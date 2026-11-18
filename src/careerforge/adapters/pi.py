"""Fail-closed Pi JSONL controller, without project files or provider credentials.

The SDK runs the tool loop; the Python service owns the model transcript, funding,
tools and proposals. A child packet is a request, never an authorization.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from ..errors import DomainError
from ..util import canonical

MAX_FRAME_BYTES = 1_000_000
SDK_VERSION = "1.3.0"
PROTOCOL_VERSION = 1


def _json_packet(line: bytes) -> dict:
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    def reject_constant(_):
        raise ValueError("non-finite number")

    try:
        value = json.loads(line, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DomainError("PI_PROTOCOL", "Pi返回了无效JSON对象。", 502) from exc


async def run_pi(runtime_dir: Path, init: dict, dispatch, heartbeat):
    node = shutil.which("node")
    runner = runtime_dir / "runner.mjs"
    if not node or not runner.is_file():
        raise DomainError("PI_RUNTIME_UNAVAILABLE", "Pi SDK未安装；不会替换编码Agent。", 503)
    for name in ("pi-coding-agent", "pi-ai"):
        package = runtime_dir / f"node_modules/@earendil-works/{name}/package.json"
        try:
            installed = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DomainError("PI_RUNTIME_UNAVAILABLE", "Pi SDK未完整安装。", 503) from exc
        if not isinstance(installed, dict) or installed.get("version") != SDK_VERSION:
            raise DomainError("PI_VERSION", "Pi SDK版本与冻结合同不符。", 503)
    call_limit = init.get("call_limit")
    if type(call_limit) is not int or not 1 <= call_limit <= 4:
        raise DomainError("PI_BUDGET", "Pi模型调用额度无效。", 422)
    allowed_tools = {tool["name"] for tool in init["tools"]}
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR") if key in os.environ}
    with TemporaryDirectory(prefix="careerforge-pi-") as temp:
        env.update(HOME=temp, USERPROFILE=temp, TMPDIR=temp, TEMP=temp, TMP=temp)
        proc = await asyncio.create_subprocess_exec(
            node,
            str(runner.resolve()),
            cwd=temp,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_FRAME_BYTES + 1,
        )

        stdin, stdout = proc.stdin, proc.stdout

        async def send(value):
            if stdin is None:
                raise DomainError("PI_PROTOCOL", "Pi未建立受控输入管道。", 502)
            frame = canonical(value).encode() + b"\n"
            if len(frame) > MAX_FRAME_BYTES:
                raise DomainError("PI_FRAME", "Pi会话帧超限。", 413)
            try:
                stdin.write(frame)
                await asyncio.wait_for(stdin.drain(), timeout=5)
            except (BrokenPipeError, ConnectionError, TimeoutError) as exc:
                raise DomainError("PI_PROTOCOL", "Pi写入中断，不重试已发模型调用。", 502) from exc

        ready = False
        last_id = model_count = tool_count = 0
        submitted = False
        try:
            if stdout is None:
                raise DomainError("PI_PROTOCOL", "Pi未建立受控输出管道。", 502)
            await send({**init, "protocol": PROTOCOL_VERSION})
            deadline = asyncio.get_running_loop().time() + 30 + call_limit * 150
            for _ in range(80):
                while True:
                    heartbeat()
                    if asyncio.get_running_loop().time() >= deadline:
                        raise DomainError("PI_TIMEOUT", "Pi会话超时。", 504)
                    try:
                        line = await asyncio.wait_for(stdout.readline(), timeout=2)
                        break
                    except TimeoutError:
                        continue
                    except (ValueError, asyncio.LimitOverrunError) as exc:
                        raise DomainError("PI_FRAME", "Pi消息越界。", 413) from exc
                if not line or len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                    raise DomainError(
                        "PI_PROTOCOL", "Pi连接中断或消息越界，不重试已发模型调用。", 502
                    )
                packet = _json_packet(line)
                kind = packet.get("type")
                if not ready:
                    if (
                        kind != "ready"
                        or set(packet) != {"type", "sdk", "protocol", "tools"}
                        or packet.get("sdk") != f"pi-coding-agent-{SDK_VERSION}"
                        or type(packet.get("protocol")) is not int
                        or packet["protocol"] != PROTOCOL_VERSION
                    ):
                        raise DomainError("PI_PROTOCOL", "Pi没有完成受限会话握手。", 502)
                    names = packet.get("tools")
                    if (
                        not isinstance(names, list)
                        or any(not isinstance(name, str) for name in names)
                        or len(names) != len(allowed_tools)
                        or set(names) != allowed_tools
                    ):
                        raise DomainError("PI_TOOL_SCOPE", "SDK加载了未经授权的工具。", 403)
                    ready = True
                    continue
                if kind == "done":
                    if (
                        set(packet) != {"type", "proposed", "calls", "failed"}
                        or type(packet.get("calls")) is not int
                        or packet["calls"] != model_count
                        or type(packet.get("proposed")) is not bool
                        or packet["proposed"] != submitted
                        or type(packet.get("failed")) is not bool
                        or packet["failed"]
                    ):
                        raise DomainError("PI_PROTOCOL", "Pi结束状态与服务端观察不一致。", 502)
                    return packet
                if submitted or kind not in {"model", "tool"}:
                    raise DomainError("PI_PROTOCOL", "Pi返回未知消息或在提交后继续调用。", 502)
                expected = (
                    {"type", "id"}
                    if kind == "model"
                    else {"type", "id", "name", "arguments", "call_id"}
                )
                if (
                    set(packet) != expected
                    or type(packet.get("id")) is not int
                    or packet["id"] != last_id + 1
                ):
                    raise DomainError("PI_PROTOCOL", "Pi请求字段或顺序无效。", 502)
                last_id = packet["id"]
                if kind == "model":
                    model_count += 1
                    if model_count > call_limit:
                        raise DomainError("PI_BUDGET", "Pi模型调用额度已用尽。")
                else:
                    if (
                        not isinstance(packet["name"], str)
                        or packet["name"] not in allowed_tools
                        or not isinstance(packet["arguments"], dict)
                        or not isinstance(packet["call_id"], str)
                        or not 1 <= len(packet["call_id"]) <= 200
                    ):
                        raise DomainError("PI_TOOL_SCOPE", "Pi工具或参数未获授权。", 403)
                    tool_count += 1
                    if tool_count > 12:
                        raise DomainError("PI_TOOLS", "Pi工具调用额度已用尽。")
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    response = await asyncio.wait_for(dispatch(packet), timeout=max(0, remaining))
                except TimeoutError as exc:
                    raise DomainError("PI_TIMEOUT", "Pi会话调用超时。", 504) from exc
                if kind == "tool" and packet["name"] == "submit_reply":
                    submitted = bool(
                        isinstance(response, dict)
                        and response.get("proposal_id")
                        and not response.get("is_error")
                    )
                await send({"id": packet["id"], "result": response})
            raise DomainError("PI_MESSAGES", "Pi消息次数超过限制。")
        finally:
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
            with suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
