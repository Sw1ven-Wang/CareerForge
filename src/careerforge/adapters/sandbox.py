"""Trusted Linux Docker supervisor. No host-command or reduced-isolation fallback."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from ..errors import DomainError
from ..util import canonical, validate_files

MAX_RECEIPT_BYTES = 3_000_000
MAX_STREAM_BYTES = 48_000  # 16KB tail can expand when invalid UTF-8 is replaced.


def _run_bounded(command: list[str], *, timeout: float, limit: int) -> subprocess.CompletedProcess:
    """Drain both pipes without unbounded capture_output memory or pipe deadlocks."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    stdout, stderr = process.stdout, process.stderr
    try:
        if stdout is None or stderr is None:
            raise DomainError("SANDBOX_UNAVAILABLE", "执行器未建立受控输出管道。", 503)
        # Python 3.12 supports nonblocking anonymous pipes on both Windows
        # and POSIX. Windows select() only accepts sockets.
        pipes = {"stdout": stdout.fileno(), "stderr": stderr.fileno()}
        for fd in pipes.values():
            os.set_blocking(fd, False)
        while pipes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            made_progress = False
            for name, fd in tuple(pipes.items()):
                try:
                    chunk = os.read(fd, 65_536)
                except BlockingIOError:
                    continue
                made_progress = True
                if not chunk:
                    del pipes[name]
                    continue
                stream = buffers[name]
                ceiling = limit if name == "stdout" else 64_000
                if len(stream) + len(chunk) > ceiling:
                    raise DomainError("SANDBOX_OUTPUT", "执行回执或诊断输出超限。", 502)
                stream.extend(chunk)
            if not made_progress:
                time.sleep(min(remaining, 0.01))
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return subprocess.CompletedProcess(
            command, code, bytes(buffers["stdout"]), bytes(buffers["stderr"])
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if stdout is not None:
            stdout.close()
        if stderr is not None:
            stderr.close()


def _files(files: object) -> None:
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        raise DomainError("SANDBOX_RECEIPT", "沙箱文件必须是文本映射。", 502)
    if files:
        validate_files(files, max_files=100, max_bytes=2_000_000)


def validate_receipt(result: object) -> dict:
    """Reject booleans masquerading as exit codes and every malformed nested field."""
    if not isinstance(result, dict) or set(result) != {
        "exit_code",
        "stdout",
        "stderr",
        "files",
        "timed_out",
    }:
        raise DomainError("SANDBOX_RECEIPT", "沙箱回执Schema不符合合同。", 502)
    if (
        type(result["exit_code"]) is not int
        or not -255 <= result["exit_code"] <= 255
        or type(result["timed_out"]) is not bool
        or (result["timed_out"] and result["exit_code"] != 124)
    ):
        raise DomainError("SANDBOX_RECEIPT", "沙箱退出状态无效。", 502)
    try:
        for key in ("stdout", "stderr"):
            if (
                not isinstance(result[key], str)
                or len(result[key].encode("utf-8")) > MAX_STREAM_BYTES
            ):
                raise DomainError("SANDBOX_RECEIPT", "沙箱输出字段无效。", 502)
    except UnicodeError as exc:
        raise DomainError("SANDBOX_RECEIPT", "沙箱输出不是有效UTF-8文本。", 502) from exc
    try:
        _files(result["files"])
    except (DomainError, UnicodeError) as exc:
        raise DomainError("SANDBOX_RECEIPT", "沙箱返回的文件超出文本合同。", 502) from exc
    return result


class DockerSandbox:
    synthetic = False

    def execute(self, execution: dict, argv: list[str]) -> dict:
        docker = shutil.which("docker")
        if not docker:
            raise DomainError("SANDBOX_UNAVAILABLE", "可信执行端没有Docker；拒绝宿主执行。", 503)
        image = execution.get("image_digest", "")
        if not isinstance(image, str) or not re.fullmatch(
            r"(?:[A-Za-z0-9_./:-]+@)?sha256:[a-f0-9]{64}", image
        ):
            raise DomainError("IMAGE_DIGEST", "运行环境必须固定到不可变镜像摘要。", 422)
        if (
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 30
            or not all(isinstance(x, str) and len(x) <= 1000 and "\x00" not in x for x in argv)
            or argv[0] not in {"python", "python3"}
        ):
            raise DomainError(
                "COMMAND_SCOPE", "默认环境仅允许明确的Python argv，无宿主Shell。", 422
            )
        _files(execution.get("files"))
        try:
            info = _run_bounded(
                [docker, "info", "--format", "{{json .}}"], timeout=10, limit=256_000
            )
            if info.returncode:
                raise DomainError("SANDBOX_UNAVAILABLE", "Docker守护进程不可用。", 503)
            capabilities = json.loads(info.stdout)
            if not isinstance(capabilities, dict):
                raise DomainError("SANDBOX_ISOLATION", "Docker隔离能力响应无效。", 503)
            security = capabilities.get("SecurityOptions")
            if (
                not isinstance(security, list)
                or capabilities.get("OSType") != "linux"
                or not all(
                    capabilities.get(k) is True for k in ("MemoryLimit", "PidsLimit", "CpuCfsQuota")
                )
                or not any(
                    x in {"name=seccomp,profile=builtin", "name=seccomp,profile=default"}
                    for x in security
                    if isinstance(x, str)
                )
            ):
                raise DomainError(
                    "SANDBOX_ISOLATION",
                    "Docker缺少所需内存/进程/CPU或seccomp隔离；拒绝降级执行。",
                    503,
                )
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            raise DomainError("SANDBOX_UNAVAILABLE", "无法验证Docker隔离能力。", 503) from exc
        name = "careerforge-" + str(uuid4())
        with tempfile.TemporaryDirectory(prefix="cf-sandbox-input-") as tmp:
            job = Path(tmp) / "job.json"
            job.write_text(canonical({"files": execution["files"], "argv": argv}), encoding="utf-8")
            job.chmod(0o444)
            command = [
                docker,
                "run",
                "--rm",
                "--pull",
                "never",
                "--name",
                name,
                "--network",
                "none",
                "--ipc",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "SETUID",
                "--cap-add",
                "SETGID",
                "--cap-add",
                "KILL",
                # PID 1 must read job-owned 0600 files / traverse 0700 output dirs.
                # setuid(65534) clears these capabilities before untrusted execution.
                "--cap-add",
                "DAC_READ_SEARCH",
                "--security-opt",
                "no-new-privileges=true",
                "--pids-limit",
                "64",
                "--memory",
                "256m",
                "--cpus",
                "1",
                "--ulimit",
                "nofile=256:256",
                "--log-driver",
                "none",
                "--stop-timeout",
                "1",
                "--tmpfs",
                "/workspace:rw,size=64m,mode=1777",
                "--tmpfs",
                "/tmp:rw,size=16m,mode=1777",
                "--mount",
                f"type=bind,src={job.resolve()},dst=/input/job.json,readonly",
                image,
            ]
            try:
                completed = _run_bounded(command, timeout=45, limit=MAX_RECEIPT_BYTES)
                if completed.returncode:
                    raise DomainError("SANDBOX_CONTAINER", "隔离容器未成功返回可信回执。", 502)
                result = validate_receipt(json.loads(completed.stdout))
                return {
                    **result,
                    "image_digest": image,
                    "synthetic": False,
                    "runtime": "docker-network-none",
                }
            except subprocess.TimeoutExpired as exc:
                raise DomainError(
                    "SANDBOX_TIMEOUT", "容器响应超时；状态不明，不自动重试。", 504
                ) from exc
            except (ValueError, OSError) as exc:
                raise DomainError("SANDBOX_UNAVAILABLE", "隔离执行服务不可用。", 503) from exc
            finally:
                # Cleanup must neither mask the original error nor convert a valid receipt into
                # a false success when a container cannot be confirmed removed.
                active_error = sys.exc_info()[0] is not None
                try:
                    cleanup = _run_bounded([docker, "rm", "-f", name], timeout=10, limit=4096)
                    if cleanup.returncode and b"No such container" not in cleanup.stderr:
                        raise DomainError(
                            "SANDBOX_CLEANUP", "无法确认容器已移除；不自动重试。", 503
                        )
                except (OSError, subprocess.TimeoutExpired, DomainError) as exc:
                    if not active_error:
                        raise DomainError(
                            "SANDBOX_CLEANUP", "无法确认容器已移除；不自动重试。", 503
                        ) from exc
