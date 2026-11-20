"""Container-only PID 1 supervisor. Importing this module never executes a job."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

ROOT = Path("/workspace")
BLOCKED_PARTS = {
    ".git",
    ".env",
    ".ssh",
    ".aws",
    ".azure",
    ".kube",
    ".codex",
    ".agents",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "vendor",
}


def safe_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and len(name) <= 1000
        and not PurePosixPath(name).is_absolute()
        and "\\" not in name
        and ":" not in name
        and not any(ord(c) < 32 for c in name)
        and all(
            p not in {"", ".", ".."}
            and p.lower() not in BLOCKED_PARTS
            and not p.lower().startswith(".env.")
            for p in name.split("/")
        )
    )


def valid_text(body: object) -> bool:
    return (
        isinstance(body, str)
        and "\x00" not in body
        and len(body.encode("utf-8")) <= 100_000
        and all(len(line.encode("utf-8")) <= 25_000 for line in body.splitlines())
    )


def validate_job(job: object) -> dict:
    if not isinstance(job, dict) or set(job) != {"files", "argv"}:
        raise ValueError("invalid job schema")
    files, argv = job["files"], job["argv"]
    if (
        not isinstance(files, dict)
        or len(files) > 100
        or not all(safe_name(k) and valid_text(v) for k, v in files.items())
        or sum(len(v.encode("utf-8")) for v in files.values()) > 2_000_000
    ):
        raise ValueError("invalid workspace")
    if (
        not isinstance(argv, list)
        or not 1 <= len(argv) <= 30
        or not all(isinstance(a, str) and len(a) <= 1000 and "\x00" not in a for a in argv)
        or argv[0] not in {"python", "python3"}
    ):
        raise ValueError("invalid argv")
    names = set(files)
    if any(str(parent) in names for name in names for parent in PurePosixPath(name).parents):
        raise ValueError("file/directory conflict")
    return job


def collect_files(root: Path) -> dict[str, str]:
    result, total = {}, 0
    for path, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            d
            for d in dirs
            if not (Path(path) / d).is_symlink()
            and safe_name(str((Path(path) / d).relative_to(root)))
        )
        for name in sorted(files):
            candidate = Path(path) / name
            relative = str(candidate.relative_to(root))
            if not safe_name(relative) or candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                if candidate.stat().st_size > 100_000:
                    continue
                # Bound the read even if this helper is used outside the stopped-child phase.
                with candidate.open("rb") as handle:
                    data = handle.read(100_001)
                body = data.decode("utf-8")
                if not valid_text(body):
                    continue
            except (OSError, UnicodeError):
                continue
            if len(result) >= 100 or total + len(data) > 2_000_000:
                return result
            result[relative] = body
            total += len(data)
    return result


def drop_privileges() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
    os.umask(0o077)


def kill_children() -> None:
    # kill(-1) is safe ONLY in our dedicated container PID namespace, never on the host.
    if os.getpid() != 1 or os.geteuid() != 0:
        raise RuntimeError("sandbox supervisor must be root PID 1 inside its container")
    for sig in (signal.SIGSTOP, signal.SIGKILL):
        try:
            os.kill(-1, sig)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid:
            continue
        if time.monotonic() >= deadline:
            raise RuntimeError("sandbox children did not stop before receipt collection")
        time.sleep(0.01)


def tail(handle) -> str:
    handle.seek(0, 2)
    handle.seek(max(0, handle.tell() - 16_000))
    return handle.read(16_000).decode("utf-8", "replace")


def main() -> int:
    if os.getpid() != 1 or os.geteuid() != 0:
        raise RuntimeError("refusing to run sandbox entry outside a dedicated container as PID 1")
    with Path("/input/job.json").open("rb") as handle:
        payload = handle.read(3_000_001)
    if len(payload) > 3_000_000:
        raise ValueError("job too large")
    job = validate_job(json.loads(payload))
    for name, body in job["files"].items():
        target = ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        target.chmod(0o666)
    for path, _, _ in os.walk(ROOT):
        Path(path).chmod(0o777)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        timed_out, exit_code = False, 127
        try:
            process = subprocess.Popen(
                job["argv"],
                cwd=ROOT,
                stdout=out,
                stderr=err,
                preexec_fn=drop_privileges,
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": "/tmp",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            try:
                exit_code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_children()
                process.wait(timeout=3)
                exit_code = 124
        except OSError:
            pass
        finally:
            kill_children()
        result = {
            "exit_code": exit_code,
            "stdout": tail(out),
            "stderr": tail(err),
            "files": collect_files(ROOT),
            "timed_out": timed_out,
        }
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
