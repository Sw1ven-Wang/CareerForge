"""Bounded JSONL bridge: Pi controls the loop, Python owns model and tool authority.

No project is mounted in the child process, no provider key is inherited, and no
shell is involved. This controller is separate from the command sandbox.
"""

import asyncio
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json

MAX_FRAME = 1_000_000


def runtime_paths() -> tuple[str, Path]:
    settings = get_settings()
    node = shutil.which(settings.pi_node_executable)
    runner = settings.pi_runtime_dir.resolve() / "runner.mjs"
    package = runner.parent / "node_modules/@earendil-works/pi-coding-agent/package.json"
    if not node or not runner.is_file() or not package.is_file():
        raise BusinessError(
            503, "PI_RUNTIME_UNAVAILABLE", "Pi SDK 未安装，请安装 coding-agent 依赖并重建 Worker。"
        )
    if json.loads(package.read_text(encoding="utf-8"))["version"] != "1.3.0":
        raise BusinessError(503, "PI_RUNTIME_VERSION", "Pi SDK 版本与冻结合同不符，请使用 npm ci。")
    return node, runner


async def run_session(
    init: Json,
    dispatch: Callable[[Json], Awaitable[Json]],
    tick: Callable[[], None],
) -> Json:
    node, runner = runtime_paths()
    # Do not inherit .env, provider secrets, NODE_OPTIONS, proxies, home, or SDK config.
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "PATH") if key in os.environ}
    with TemporaryDirectory(prefix="careerforge-pi-") as temporary:
        env.update(
            {
                "HOME": temporary,
                "USERPROFILE": temporary,
                "TMPDIR": temporary,
                "TEMP": temporary,
                "TMP": temporary,
            }
        )
        process = await asyncio.create_subprocess_exec(
            node,
            str(runner),
            cwd=temporary,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_FRAME + 1,
        )
        writer, reader = process.stdin, process.stdout
        assert writer is not None and reader is not None

        async def send(value: Json) -> None:
            frame = json.dumps(value, ensure_ascii=False).encode() + b"\n"
            if len(frame) > MAX_FRAME:
                raise BusinessError(413, "PI_CONTEXT_LIMIT", "Pi 会话数据超过限制，请缩小上下文。")
            writer.write(frame)
            await writer.drain()

        try:
            await send(init)
            deadline = (
                asyncio.get_running_loop().time()
                + 30
                + init["call_limit"] * (get_settings().real_agent_timeout_seconds + 20)
            )
            for _ in range(80):
                while True:
                    tick()  # Lease and cancellation apply even while waiting for SDK startup.
                    if asyncio.get_running_loop().time() > deadline:
                        raise BusinessError(504, "PI_RUNTIME_TIMEOUT", "Pi 会话超时，已停止。")
                    try:
                        line = await asyncio.wait_for(reader.readline(), timeout=2)
                        break
                    except TimeoutError:
                        continue
                if not line or len(line) > MAX_FRAME:
                    raise BusinessError(
                        502, "PI_RUNTIME_ERROR", "Pi SDK 未返回完整结果；不会自动重试模型调用。"
                    )
                packet = json.loads(line)
                if packet["type"] == "ready":
                    if set(packet["tools"]) != {tool["name"] for tool in init["tools"]}:
                        raise BusinessError(502, "PI_TOOL_SCOPE", "Pi 工具集合不符合受控合同。")
                elif packet["type"] == "done":
                    return dict(packet)
                elif packet["type"] in {"model", "tool"}:
                    response = await dispatch(packet)
                    await send({"id": packet["id"], "result": response})
                else:
                    raise BusinessError(
                        502, "PI_RUNTIME_ERROR", "Pi SDK 执行失败；不会自动重试模型调用。"
                    )
            raise BusinessError(422, "PI_STEP_LIMIT", "Pi 工具步数达到上限，已停止。")
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()
