"""Read-only sandbox inventory; no host execution or environment expansion."""

import json
import shutil
import time
from pathlib import Path

from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json

TOOLS = ("python", "python3", "uv", "node", "npm", "go", "cargo")
ALLOWED = {"python", "python3", "uv", "node", "npm"}
LIMITS = {
    "timeout_seconds": 30,
    "memory_mb": 256,
    "cpus": 1,
    "max_files": 100,
    "max_bytes": 2_000_000,
    "network": "none",
    "execution_location": "linux_container_file_copy",
}


def inventory() -> Json:
    return {
        "version": "sandbox-capabilities-1",
        "status": "ready",
        "tools": {name: bool(shutil.which(name)) for name in TOOLS},
        "allowed_commands": sorted(ALLOWED),
        "limits": LIMITS,
    }


def capabilities() -> Json:
    try:
        value = json.loads(Path("/mailbox/capabilities.json").read_text())
        if value.get("version") != "sandbox-capabilities-1" or not isinstance(
            value.get("tools"), dict
        ):
            raise ValueError("invalid inventory")
        age = time.time() - float(value.get("reported_at", 0))
        if not 0 <= age <= 10:
            raise ValueError("sandbox inventory heartbeat expired")
        return {**value, "ready": Path("/mailbox/ready").exists()}
    except (OSError, ValueError, TypeError):
        return {
            "version": "sandbox-capabilities-1",
            "status": "unknown",
            "ready": False,
            "tools": {},
            "allowed_commands": sorted(ALLOWED),
            "limits": LIMITS,
            "warning": "沙箱能力尚未上报，请启动或更新隔离服务；宿主机工具不会自动进入沙箱。",
        }


def check_command(argv: list[str], *, executing: bool = False) -> Json:
    value = capabilities()
    tool = argv[0] if argv else ""
    if tool not in ALLOWED:
        raise BusinessError(422, "COMMAND_NOT_ALLOWED", "此命令不在现有沙箱允许范围。")
    if value["status"] == "ready" and not value["tools"].get(tool):
        raise BusinessError(
            422, "SANDBOX_TOOL_UNAVAILABLE", f"当前沙箱没有 {tool}，未执行；本机安装不会自动生效。"
        )
    if executing and value["status"] != "ready":
        raise BusinessError(409, "SANDBOX_CAPABILITIES_UNKNOWN", "先等待沙箱上报能力，再确认执行。")
    if executing and not value.get("ready"):
        raise BusinessError(409, "SANDBOX_NOT_READY", "沙箱尚未就绪或正忙，请稍后重新确认执行。")
    return value


def planning(
    intent: Json, requirements: list[Json], projects: list[Json], observed: Json | None = None
) -> Json:
    value = observed if observed is not None else capabilities()
    needs_run = intent["kind"] == "build_project" or (
        intent["kind"] == "function"
        and any(
            r["depth"] != "了解" for r in requirements if r["key"] in intent["requirement_keys"]
        )
    )
    runtime = intent.get("execution_runtime")
    if needs_run and not runtime:
        text = " ".join(str(p.get("technology_stack", [])) for p in projects).lower()
        selected = [r for r in requirements if r["key"] in intent["requirement_keys"]]
        text += " " + " ".join(r["technology"].lower() for r in selected)
        candidates = [
            name
            for name, words in {
                "python": ("python",),
                "node": ("node", "javascript", "typescript"),
                "go": ("golang", " go ", "go语言"),
                "rust": ("rust",),
            }.items()
            if any(word in " " + text + " " for word in words)
        ]
        if len(candidates) == 1:
            runtime = candidates[0]
        else:
            raise BusinessError(
                422,
                "PLAN_RUNTIME_REQUIRED",
                "构建或实现任务请明确选择执行环境；阅读计划不受此限制。",
            )
    if needs_run:
        if runtime not in {"python", "node"}:
            raise BusinessError(
                422,
                "PLAN_RUNTIME_UNAVAILABLE",
                f"现有沙箱尚不支持 {runtime}；可选择阅读计划，或使用明确支持的实现环境。",
            )
        if value["status"] != "ready":
            raise BusinessError(
                409,
                "SANDBOX_CAPABILITIES_UNKNOWN",
                "沙箱能力未知，先启动隔离服务再规划执行型任务。",
            )
        if not value["tools"].get(runtime):
            raise BusinessError(
                422, "PLAN_RUNTIME_UNAVAILABLE", f"当前沙箱未安装 {runtime}，不能规划为可执行任务。"
            )
    return {
        **value,
        "execution_required": needs_run,
        "execution_runtime": runtime,
        "planning_constraint": "仅小型离线练习；不安装依赖、不执行宿主命令，不承诺整仓可构建。",
    }
