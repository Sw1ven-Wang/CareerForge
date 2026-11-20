"""Thin terminal client. All authorization, policy and evidence checks stay server-side."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from typing import Annotated
from uuid import uuid4

import httpx
import typer
from pydantic import ValidationError
from rich.console import Console

from ..config import Settings
from ..contracts_conversation_actions_v1 import DialogueMessage
from ..services.dialogue_routing import explicit_actions
from .control_ext import register_cli as register_control_cli
from .integration_v166 import register_cli as register_integration_cli
from .learning_ext import register_cli as register_learning_cli
from .stream import ServerEvent, StreamProtocolError, parse_events

app = typer.Typer(no_args_is_help=True, help="DevTutor 1.6 · 有源码证据的职业学习助手")
improve = typer.Typer(help="候选、独立评测、发布和回滚")
workspace = typer.Typer(help="服务端练习工作区与独立命令审批")
app.add_typer(improve, name="improve")
app.add_typer(workspace, name="workspace")
console = Console()


def clean(text):
    # Strip both 7-bit ESC and 8-bit C1 terminal control strings, including OSC
    # hyperlinks/clipboard operations. Preserve normal Unicode, newlines and tabs.
    text = re.sub(
        r"(?:\x1b[\]P^_]|[\x90\x9d\x9e\x9f]).*?(?:\x07|\x1b\\|\x9c|$)", "", str(text), flags=re.S
    )
    text = re.sub(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\x1b[ -/]*[@-~]", "", text)
    bidi = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    return "".join(
        c
        for c in text
        if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cs"} and c not in bidi
    )


def config_path():
    return Settings.from_env().state_dir / "client.json"


def config():
    path = config_path()
    if not path.exists():
        raise typer.BadParameter("先运行 careerforge init，或建立受控的 client.json。")
    return json.loads(path.read_text(encoding="utf-8"))


def save_config(data):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # A random O_EXCL file avoids following a pre-existing client.tmp symlink.
    fd, name = tempfile.mkstemp(prefix=".client-", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def client():
    c = config()
    return httpx.Client(
        base_url=c.get("api_url", Settings.from_env().api_url),
        headers={"Authorization": "Bearer " + c["token"]},
        timeout=60,
        trust_env=False,
    )


def request(method, path, body=None):
    try:
        with client() as c:
            r = c.request(method, path, json=body)
            if not r.is_success:
                console.print(clean(r.text), markup=False)
                raise typer.Exit(1)
            return r.json()
    except httpx.HTTPError as e:
        console.print(clean(f"连接失败：{type(e).__name__}。请检查API服务。"), markup=False)
        raise typer.Exit(1) from None


def show(value):
    console.print_json(json.dumps(value, ensure_ascii=False, default=str))


@app.command("legacy-export")
def legacy_export(
    owner: str,
    output: Path,
    confirm_read_only_export: bool = False,
    database_env: str = "CAREERFORGE_LEGACY_DATABASE_URL",
):
    """只读导出原固定基线的单个owner内容；不读密码/会话/账务，不修改旧库。"""
    if not confirm_read_only_export:
        raise typer.BadParameter("必须显式 --confirm-read-only-export；导出文件包含个人资料。")
    database_url = os.getenv(database_env)
    if not database_url:
        raise typer.BadParameter("指定环境变量中缺少历史数据库连接；不要把凭据放进命令参数。")
    from ..compat_export import export_legacy, write_export

    try:
        bundle = export_legacy(database_url, owner)
        write_export(bundle, output)
    except Exception as exc:
        # Database URLs and legacy bodies must not escape through driver errors.
        console.print(
            clean(f"只读导出失败：{getattr(exc, 'code', type(exc).__name__)}。没有覆盖已有文件。"),
            markup=False,
        )
        raise typer.Exit(1) from None
    show(
        {
            "output": str(output),
            "record_count": len(bundle.records),
            "bundle_hash": bundle.bundle_hash,
            "status": "unreviewed_legacy",
            "authentication_records_exported": False,
        }
    )


@app.command("legacy-import")
def legacy_import(bundle_file: Path, confirm: str, namespace: str | None = None):
    """导入已导出的历史归档到本人私域；confirm必须为IMPORT_UNREVIEWED。"""
    from ..contracts_compat import LegacyBundle

    if bundle_file.stat().st_size > 24_000_000:
        raise typer.BadParameter("导出文件过大。")
    try:
        bundle = LegacyBundle.model_validate_json(bundle_file.read_text(encoding="utf-8"))
    except ValueError:
        raise typer.BadParameter("历史导出结构、owner或完整性哈希无效。") from None
    show(
        request(
            "POST",
            "/api/v1/compat/legacy/imports",
            {
                "namespace_id": namespace or config()["namespace_id"],
                "bundle": bundle.model_dump(mode="json"),
                "confirm": confirm,
            },
        )
    )


def run_settings(development=False):
    settings = Settings.from_env()
    if development:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        settings = replace(
            settings,
            mode="development",
            database_url="sqlite+pysqlite:///" + str(settings.state_dir / "development.db"),
        )
    return settings


@app.command()
def init(development: bool = False, name: str = "owner"):
    """建立空数据库和本机管理员；不覆盖已有账号。development明确使用SQLite且无假模型。"""
    from ..application import Application
    from ..errors import DomainError

    if config_path().exists():
        raise typer.BadParameter("client.json已存在，不覆盖已有凭证。")
    try:
        service = Application(run_settings(development))
        data = service.bootstrap(name)
    except (DomainError, ImportError) as e:
        console.print(clean(str(e)), markup=False)
        raise typer.Exit(1) from None
    save_config({**data, "api_url": service.settings.api_url, "development": development})
    console.print("已初始化。访问令牌只保存到本机 client.json；启动API和Worker后即可对话。")


@app.command()
def serve(development: bool = False, host: str = "127.0.0.1", port: int = 8000):
    """启动API。默认仅监听回环地址；生产须自行配置TLS与可信反向代理。"""
    import uvicorn

    from ..api import create_app
    from ..application import Application

    uvicorn.run(create_app(Application(run_settings(development))), host=host, port=port, ws="none")


@app.command()
def worker(development: bool = False, once: bool = False):
    """development执行本地持久队列；生产使用celery命令。"""
    from ..application import Application

    if not development:
        raise typer.BadParameter(
            "生产请运行 celery -A careerforge.worker:celery_app worker -Q careerforge"
        )
    service = Application(run_settings(True))
    while True:
        asyncio.run(service.drain())
        if once:
            return
        time.sleep(0.25)


@app.command()
def dispatch(once: bool = False):
    """生产Outbox投递与reaper，不运行模型。"""
    from ..worker import dispatch_once

    while True:
        count = dispatch_once()
        if once:
            show({"dispatched": count})
            return
        time.sleep(0.5)


@app.command("sandbox-supervisor")
def sandbox_supervisor(once: bool = False):
    """仅可信执行端运行；需要Docker权限，API/普通Worker不需要Docker socket。"""
    from ..adapters.sandbox import DockerSandbox
    from ..application import Application

    service = Application(Settings.from_env())
    while True:
        count = service.coding.process_commands(DockerSandbox(), 1)
        if once:
            show({"executed": count})
            return
        time.sleep(0.5)


@app.command()
def authorize(
    calls: int = 100,
    hours: int = 24,
    yes: bool = False,
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source", help="可重复指定允许外发的SourceSet；省略表示在其他ACL/数据类别内不限来源"
        ),
    ] = None,
):
    """授权本实例模型费用的调用次数预算；不是货币账单硬封顶。"""
    c = config()
    if not yes and not typer.confirm(
        f"授权最多 {calls} 次模型请求（{hours}小时），{len(source) if source else '不限'}个来源范围，资料可能发送给配置的模型服务？"
    ):
        raise typer.Exit()
    result = request(
        "POST",
        "/api/v1/funding",
        {
            "namespace_id": c["namespace_id"],
            "call_cap": calls,
            "hours": hours,
            "confirm": "AUTHORIZE",
            "data_classes": ["public_source", "private_source", "private_jd", "learning_state"],
            "allowed_source_ids": source,
        },
    )
    c["funding_id"] = result["funding_id"]
    save_config(c)
    show(result)


def decode_terminal_question(question: str) -> str:
    """Recover terminal bytes preserved by Python's surrogateescape."""
    try:
        question.encode("utf-8")
    except UnicodeEncodeError:
        fallback = os.getenv("CAREERFORGE_CLI_INPUT_FALLBACK")
        if not fallback:
            raise
        question = question.encode("utf-8", errors="surrogateescape").decode(fallback)
    return question


@app.command()
def chat(
    once: Annotated[str | None, typer.Option(help="单次问题，适合脚本验收")] = None,
    conversation: str | None = None,
    new: bool = False,
    auto_build: bool = False,
    build_cap: int = 24,
    evidence_only: bool = False,
    guided: Annotated[bool, typer.Option(help="在当前已接受模块中讲解或追问")] = False,
    source: Annotated[list[str] | None, typer.Option(help="确认研究时使用的固定来源 ID")] = None,
    research_question: Annotated[
        str | None, typer.Option(help="确认构建时的明确公开检索问题；不能与固定来源合用")
    ] = None,
    auto_evidence_retry: Annotated[
        bool, typer.Option(help="仅为一个固定来源预留一次补证及独立复审；须有足够构建额度")
    ] = False,
    legacy: Annotated[bool, typer.Option(help="使用原有同步问答入口")] = False,
):
    """自然语言学习；自动识别项目/技术/比较，不要求选择检索模式。"""
    if (research_question is not None or auto_evidence_retry) and (
        once is not None or guided or evidence_only or legacy
    ):
        raise typer.BadParameter("Research admission options require interactive dialogue mode")
    c = config()
    cid = conversation or (None if new else c.get("conversation_id"))
    if cid is None:
        conv = request(
            "POST", "/api/v1/conversations", {"auto_build": auto_build, "build_call_cap": build_cap}
        )
        cid = conv["conversation_id"]
        c["conversation_id"] = cid
        save_config(c)
    if once is None and not (guided or evidence_only or legacy):
        dialogue_chat(
            cid,
            c,
            source or [],
            build_cap,
            research_question=research_question,
            auto_evidence_retry=auto_evidence_retry,
        )
        return
    while True:
        try:
            q = once if once is not None else typer.prompt("你")
        except (EOFError, KeyboardInterrupt):
            return
        try:
            q = decode_terminal_question(q)
        except (UnicodeError, LookupError):
            console.print("终端输入编码无效。请切换到 UTF-8 后重新输入。", markup=False)
            if once is not None:
                raise typer.Exit(2) from None
            continue
        if q.strip() in {"/exit", "exit", "quit"}:
            return
        conv = request("GET", "/api/v1/conversations/" + cid)
        route = "guided-turns" if guided else "turns"
        result = request(
            "POST",
            f"/api/v1/conversations/{cid}/{route}",
            {
                "question": q,
                "expected_revision": conv["revision"],
                "funding_id": None if evidence_only else c.get("funding_id"),
                "idempotency_key": str(uuid4()),
            },
        )
        c["last_turn_id"] = result["turn_id"]
        save_config(c)
        c["last_event_id"] = 0
        c["last_turn_status"] = "running"
        save_config(c)
        console.print(
            clean(f"turn: {result['turn_id']} · run: {result.get('run_id') or '直接证据工具'}"),
            markup=False,
        )
        stream_turn(result["turn_id"], c)
        if once is not None:
            return


def watch_dialogue(path, stopped):
    cursor = 0
    prefix = ""
    while not stopped.is_set():
        try:
            with client() as http:
                with http.stream(
                    "GET", path + "/activity-events", params={"after": cursor}, timeout=5
                ) as response:
                    response.raise_for_status()
                    for event in parse_events(response.iter_lines()):
                        if stopped.is_set():
                            return
                        if event.sequence <= cursor:
                            continue
                        row = event.payload
                        label = (row.get("workflow_id") or "对话")[:8]
                        answer = row.get("answer_event")
                        if answer:
                            if prefix != label:
                                console.print(clean(f"\n[{label}] Tutor"), markup=False)
                                prefix = label
                            display_event(
                                ServerEvent(answer["seq"], answer["type"], answer["payload"])
                            )
                        elif event.name.startswith(("workflow.", "run.")):
                            ref = row["reference"]
                            detail = (
                                ref.get("question") or ref.get("status") or ref.get("key") or ""
                            )
                            console.print(clean(f"[{label}] {event.name} {detail}"), markup=False)
                        cursor = event.sequence
        except (httpx.HTTPError, StreamProtocolError):
            stopped.wait(1)


def dialogue_chat(
    cid, state, sources, build_cap, *, research_question=None, auto_evidence_retry=False
):
    path = "/api/v1/conversations/" + cid
    console.print(
        "粘贴 JD 或输入学习目标。可随时问进度、暂停或继续。/language Java,Go 确认后端语言（AI/Agent 必学 Python），/workflows 查看任务，/exit 退出。"
    )
    stopped = Event()
    watcher = Thread(target=watch_dialogue, args=(path, stopped), daemon=True)
    watcher.start()
    try:
        while True:
            try:
                text = decode_terminal_question(typer.prompt("你"))
            except (EOFError, KeyboardInterrupt):
                return
            except (UnicodeError, LookupError):
                console.print("终端输入编码无效。请切换到 UTF-8 后重新输入。")
                continue
            if text.strip() in {"/exit", "exit", "quit"}:
                return
            try:
                if text == "/workflows":
                    show(request("GET", path + "/workflows"))
                    continue
                current = request("GET", path + "/workflow-status")
                body = {
                    "expected_focus_revision": current["focus_revision"],
                    "idempotency_key": str(uuid4()),
                }
                if text.startswith("/focus "):
                    result = request(
                        "POST",
                        path + "/focus",
                        {
                            **body,
                            "workflow_id": text.removeprefix("/focus ").strip(),
                        },
                    )
                else:
                    if text.startswith("/jd-file "):
                        filename = Path(text.removeprefix("/jd-file ").strip().strip('"'))
                        with filename.open(encoding="utf-8-sig", newline="") as document:
                            text = document.read(50_001)
                        body["kind"] = "jd"
                    elif text.startswith("/jd "):
                        body["kind"], text = "jd", text.removeprefix("/jd ")
                    value = DialogueMessage(
                        **body,
                        text=text,
                        authorization_ref=state.get("funding_id"),
                        build_call_cap=build_cap,
                    )
                    data = value.model_dump(mode="json")
                    if any(action == "start_build" for action, _ in explicit_actions(value) or []):
                        data["source_ids"] = sources
                        data["research_question"] = research_question
                        data["auto_evidence_retry"] = auto_evidence_retry
                        data = DialogueMessage.model_validate(data).model_dump(mode="json")
                    result = request("POST", path + "/messages", data)
                if result.get("reply"):
                    console.print(clean(result["reply"]), markup=False)
                for receipt in result["action_receipts"]:
                    if "view" in receipt:
                        show(receipt["view"])
                if result.get("turn_id"):
                    state["last_turn_id"] = result["turn_id"]
                    state["last_event_id"] = 0
                    state["last_turn_status"] = "running"
                    save_config(state)
            except (OSError, UnicodeError, ValidationError):
                console.print("输入无效。请核对文件路径、UTF-8 编码或消息长度。")
            except typer.Exit:
                # An ambiguous transport failure must never silently resubmit a paid action.
                continue
    finally:
        stopped.set()


def display_event(event: ServerEvent) -> str | None:
    value = event.payload
    if event.name == "answer.delta":
        text = value.get("text")
        if not isinstance(text, str):
            raise StreamProtocolError("answer delta text is missing")
        console.print(clean(text), end="", markup=False, highlight=False)
    elif event.name == "answer.block.validated":
        content = value.get("content")
        if not isinstance(content, dict):
            raise StreamProtocolError("validated block content is missing")
        if content.get("background_run_id"):
            console.print(clean("后台构建：" + str(content["background_run_id"])), markup=False)
        if content.get("plan_proposal"):
            show(content["plan_proposal"])
        if content.get("style") == "code_first":
            for citation in content.get("citations", [])[:2]:
                console.print(
                    clean(
                        f"{citation.get('path', '')}:{citation.get('start_line', '')} @ {citation.get('commit', '')}"
                    ),
                    markup=False,
                )
                console.print(clean(citation.get("quote", "")), markup=False, highlight=False)
    elif event.name == "answer.evidence_invalidated":
        console.print("\n引用已失效，已停止显示相关内容。")
    elif event.name == "answer.interrupted":
        show(value)
        return "interrupted"
    elif event.name == "answer.completed":
        console.print()
        return "completed"
    return None


def stream_turn(turn_id: str, state: dict, start: int = 0) -> None:
    """Reconnect GET only. EOF without a terminal event is never success."""
    cursor = start
    for attempt in range(3):
        try:
            with client() as http:
                with http.stream(
                    "GET", f"/api/v1/turns/{turn_id}/events", params={"after": cursor}, timeout=180
                ) as response:
                    if not response.is_success:
                        console.print(clean(response.read().decode(errors="replace")), markup=False)
                        raise typer.Exit(1)
                    for event in parse_events(response.iter_lines()):
                        if event.sequence <= cursor:
                            continue  # A replay never prints already acknowledged deltas twice.
                        terminal = display_event(event)
                        cursor = event.sequence
                        state.update(
                            last_turn_id=turn_id,
                            last_event_id=cursor,
                            last_turn_status=terminal or "running",
                        )
                        save_config(state)
                        if terminal == "interrupted":
                            raise typer.Exit(1)
                        if terminal == "completed":
                            return
        except httpx.HTTPError:
            pass  # Retry only the same persisted event stream, never a new turn/model call.
        except (StreamProtocolError, KeyError, TypeError) as exc:
            console.print("事件流格式无效；未确认的事件不会推进游标。可用 resume 重试。")
            raise typer.Exit(1) from exc
        if attempt < 2:
            time.sleep(0.1 * (attempt + 1))
    console.print("事件流中断，尚未确认回答完成。使用 careerforge resume 恢复，不会新建模型请求。")
    raise typer.Exit(1)


@app.command()
def resume(turn: str | None = None, from_start: bool = False):
    """恢复最后回答的已保存事件；只重放GET，不新建问题或重复调用模型。"""
    state = config()
    identifier = turn or state.get("last_turn_id")
    if not identifier:
        raise typer.BadParameter("没有可恢复回答；可用 --turn 指定。")
    same = identifier == state.get("last_turn_id")
    if same and not from_start and state.get("last_turn_status") == "completed":
        console.print(
            "该回答已显示完成。使用 sources 检查当前引用，或 resume --from-start 重新显示。"
        )
        return
    if same and not from_start and state.get("last_turn_status") == "interrupted":
        console.print("该回答已中断。使用 tasks 查看原因，或 resume --from-start 重放已保存事件。")
        raise typer.Exit(1)
    cursor = state.get("last_event_id", 0) if same and not from_start else 0
    if not isinstance(cursor, int) or cursor < 0:
        raise typer.BadParameter("本地事件游标无效；使用 --from-start 重新显示。")
    stream_turn(identifier, state, cursor)


@app.command()
def build(
    question: str,
    repo: str | None = None,
    source: str | None = None,
    jd_file: Path | None = None,
    topics: str = "",
    calls: int = 24,
    max_projects: int = 3,
    max_units_per_project: int = 2,
    duration_minutes: int = 30,
    planning_call_seconds: int = 120,
    yes: bool = False,
    auto_evidence_retry: bool = False,
    coverage_guided_analysis: bool = False,
    mechanism_targets_file: Path | None = None,
    discover_mechanisms: bool = False,
):
    """明确授权知识构建批次。日常学习仍使用chat自动路由。"""
    c = config()
    if not c.get("funding_id"):
        raise typer.BadParameter("先运行 authorize 授权调用额度。")
    if not yes and not typer.confirm("按上述来源和预算进行研究、分析、独立审核？"):
        raise typer.Exit()
    jd = jd_file.read_text(encoding="utf-8") if jd_file else None
    mechanism_targets = {}
    if mechanism_targets_file:
        from pydantic import TypeAdapter, ValidationError

        from ..contracts import MechanismEdgeTarget

        try:
            targets = TypeAdapter(list[MechanismEdgeTarget]).validate_json(
                mechanism_targets_file.read_bytes()
            )
        except (OSError, ValidationError) as exc:
            raise typer.BadParameter("机制目标文件需要符合合同的 JSON 数组。") from exc
        mechanism_targets = {
            "mechanism_edge_targets": [target.model_dump(mode="json") for target in targets]
        }
    show(
        request(
            "POST",
            "/api/v1/knowledge/builds",
            {
                "namespace_id": c["namespace_id"],
                "funding_id": c["funding_id"],
                "question": question,
                "repository_url": repo,
                "source_id": source,
                "jd_text": jd,
                "topic_keys": [x.strip() for x in topics.split(",") if x.strip()],
                "call_cap": calls,
                "max_projects": max_projects,
                "max_units_per_project": max_units_per_project,
                "duration_minutes": duration_minutes,
                "planning_call_seconds": planning_call_seconds,
                **({"auto_evidence_retry": True} if auto_evidence_retry else {}),
                **({"coverage_guided_analysis": True} if coverage_guided_analysis else {}),
                **mechanism_targets,
                **({"discover_mechanisms": True} if discover_mechanisms else {}),
                "idempotency_key": str(uuid4()),
            },
        )
    )


@app.command("import-source")
def import_source(directory: Path, repository: str, commit: str, license_id: str = "unknown"):
    """只读取选定根目录内的文本，禁止符号链接和私密路径；不执行仓库。"""
    from ..util import MAX_SOURCE_FILE_BYTES, safe_path, validate_files

    root = directory.resolve(strict=True)
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        try:
            safe_path(rel)
        except Exception:
            continue
        if path.stat().st_size > MAX_SOURCE_FILE_BYTES:
            continue
        try:
            body = path.read_bytes().decode("utf-8")
        except UnicodeError:
            continue
        if "\x00" not in body:
            files[rel] = body
        if len(files) > 1000:
            raise typer.BadParameter("文件超过1000，请选择明确子目录。")
    validate_files(files)
    c = config()
    show(
        request(
            "POST",
            "/api/v1/sources",
            {
                "namespace_id": c["namespace_id"],
                "repository_key": repository,
                "commit_sha": commit,
                "license_id": license_id,
                "files": files,
                "source_metadata": {"import_mode": "user_selected_directory"},
            },
        )
    )


@app.command()
def tasks(run: str | None = None, cancel: bool = False):
    if run:
        detail = request("GET", "/api/v1/runs/" + run)
        if cancel:
            if typer.confirm("取消该任务及仍活跃的子任务？"):
                show(
                    request(
                        "POST",
                        f"/api/v1/runs/{run}/cancel",
                        {"expected_revision": detail["revision"]},
                    )
                )
        else:
            show(detail)
    else:
        show(request("GET", "/api/v1/runs"))


@app.command()
def sources(turn: str | None = None):
    turn = turn or config().get("last_turn_id")
    if not turn:
        raise typer.BadParameter("没有当前回答；可用 --turn 指定。")
    packet = request("GET", "/api/v1/turns/" + turn)
    citations = []
    for e in packet["events"]:
        if e["type"] == "answer.block.validated":
            citations += e["payload"]["content"].get("citations", [])
    show({"status": packet["status"], "citations": citations})


@app.command()
def feedback(text: str, turn: str | None = None, correction: bool = False, consent: bool = False):
    turn = turn or config().get("last_turn_id")
    if not turn:
        raise typer.BadParameter("需要具体turn。")
    show(
        request(
            "POST",
            "/api/v1/feedback",
            {
                "turn_id": turn,
                "signal": "correction" if correction else "explicit_feedback",
                "content": text,
                "consent": consent,
            },
        )
    )


@improve.command("status")
def improve_status():
    show(
        {
            "policies": request("GET", "/api/v1/policies"),
            "candidates": request("GET", "/api/v1/evolution/candidates"),
        }
    )


@improve.command("history")
def improve_history():
    show(request("GET", "/api/v1/evolution/history"))


@improve.command("propose")
def improve_propose(experiences: str, target: str = "teaching_skill"):
    c = config()
    show(
        request(
            "POST",
            "/api/v1/evolution/candidates",
            {
                "namespace_id": c["namespace_id"],
                "funding_id": c.get("funding_id"),
                "target": target,
                "experience_ids": [x.strip() for x in experiences.split(",")],
                "idempotency_key": str(uuid4()),
            },
        )
    )


@improve.command("dataset")
def improve_dataset(file: Path):
    data = json.loads(file.read_text(encoding="utf-8"))
    data["namespace_id"] = config()["namespace_id"]
    show(request("POST", "/api/v1/evolution/datasets", data))


@improve.command("evaluate")
def improve_evaluate(candidate: str, dataset: str):
    show(
        request(
            "POST", f"/api/v1/evolution/candidates/{candidate}/evaluate", {"dataset_id": dataset}
        )
    )


@improve.command("promote")
def improve_promote(candidate: str, evaluation: str, revision: int):
    if not typer.confirm("将已通过独立评测的精确候选用于后续任务？"):
        raise typer.Exit()
    show(
        request(
            "POST",
            f"/api/v1/evolution/candidates/{candidate}/promote",
            {"evaluation_id": evaluation, "expected_revision": revision, "confirm": "PROMOTE"},
        )
    )


@improve.command("rollback")
def improve_rollback(target: str, revision: int):
    if not typer.confirm("恢复上一有效策略，保持在途任务原版本？"):
        raise typer.Exit()
    show(
        request(
            "POST",
            "/api/v1/evolution/rollback",
            {
                "namespace_id": config()["namespace_id"],
                "target": target,
                "expected_revision": revision,
                "confirm": "ROLLBACK",
            },
        )
    )


@workspace.command("new")
def workspace_new(publications: str = "", independent: bool = False):
    show(
        request(
            "POST",
            "/api/v1/workspaces",
            {
                "publication_ids": [x for x in publications.split(",") if x],
                "assistance_mode": "independent" if independent else "assisted",
            },
        )
    )


@workspace.command("show")
def workspace_show(identifier: str):
    show(request("GET", "/api/v1/workspaces/" + identifier))


@workspace.command("save")
def workspace_save(identifier: str, path: str, file: Path):
    ws = request("GET", "/api/v1/workspaces/" + identifier)
    show(
        request(
            "PUT",
            f"/api/v1/workspaces/{identifier}/file",
            {
                "expected_revision": ws["revision"],
                "path": path,
                "content": file.read_text(encoding="utf-8"),
            },
        )
    )


@workspace.command("code")
def workspace_code(identifier: str, question: str, files: str = "", calls: int = 2):
    ws = request("GET", "/api/v1/workspaces/" + identifier)
    show(
        request(
            "POST",
            f"/api/v1/workspaces/{identifier}/coding",
            {
                "expected_revision": ws["revision"],
                "selected_files": [x for x in files.split(",") if x],
                "question": question,
                "funding_id": config().get("funding_id"),
                "call_cap": calls,
                "idempotency_key": str(uuid4()),
            },
        )
    )


@workspace.command("apply")
def workspace_apply(identifier: str, proposal: str):
    details = request("GET", f"/api/v1/workspaces/{identifier}/patches/{proposal}")
    show(details)
    if not typer.confirm("审阅以上before/after差异后应用该补丁？"):
        raise typer.Exit()
    ws = request("GET", "/api/v1/workspaces/" + identifier)
    show(
        request(
            "POST",
            f"/api/v1/workspaces/{identifier}/patches/{proposal}/apply",
            {"expected_revision": ws["revision"], "confirm": "APPLY"},
        )
    )


@workspace.command("run")
def workspace_run(identifier: str, argv_json: str):
    argv = json.loads(argv_json)
    ws = request("GET", "/api/v1/workspaces/" + identifier)
    proposal = request(
        "POST",
        f"/api/v1/workspaces/{identifier}/commands",
        {"expected_revision": ws["revision"], "argv": argv},
    )
    show({k: v for k, v in proposal.items() if k != "approval_nonce"})
    if not typer.confirm("在显示的代码版本与隔离环境中执行这一条命令？"):
        raise typer.Exit()
    show(
        request(
            "POST",
            f"/api/v1/commands/{proposal['command_id']}/execute",
            {"approval_nonce": proposal["approval_nonce"], "confirm": "RUN"},
        )
    )


@workspace.command("close")
def workspace_close(identifier: str):
    show(request("POST", f"/api/v1/workspaces/{identifier}/close"))


@improve.command("canary")
def canary(candidate: str, evaluation: str, revision: int, traffic: int = 10):
    if not typer.confirm("批准以此流量对后续任务进行有界灰度？事实门禁保持不变。"):
        raise typer.Exit()
    show(
        request(
            "POST",
            f"/api/v1/evolution/candidates/{candidate}/canary",
            {
                "evaluation_id": evaluation,
                "expected_revision": revision,
                "traffic_percent": traffic,
                "confirm": "CANARY",
            },
        )
    )


@improve.command("canary-status")
def canary_status(identifier: str):
    show(request("GET", f"/api/v1/evolution/canaries/{identifier}"))


@improve.command("canary-stop")
def canary_stop(identifier: str):
    show(request("POST", f"/api/v1/evolution/canaries/{identifier}/stop"))


@improve.command("forget")
def forget_feedback(identifier: str):
    if typer.confirm("撤回该反馈的优化授权、删除经验正文并停止相关灰度？"):
        show(request("DELETE", f"/api/v1/feedback/{identifier}/consent"))


@app.command("gaps")
def knowledge_gaps(identifier: str | None = None, auto_reanswer: bool = False):
    if not identifier:
        show(request("GET", "/api/v1/knowledge/gaps"))
        return
    message = (
        "授权本缺口最多24次补证调用，并在同一授权额度内最多8次原题复答调用？"
        if auto_reanswer
        else "对该缺口启动新的已授权知识构建，最多24次调用？"
    )
    if not typer.confirm(message):
        raise typer.Exit()
    show(
        request(
            "POST",
            f"/api/v1/knowledge/gaps/{identifier}/schedule",
            {
                "funding_id": config().get("funding_id"),
                "call_cap": 24,
                "idempotency_key": str(uuid4()),
                "confirm": "BUILD",
                **({"auto_reanswer": True} if auto_reanswer else {}),
            },
        )
    )


@app.command("gap-reanswer")
def gap_reanswer(identifier: str, yes: bool = False):
    if yes or typer.confirm("按原问题和新发布的证据复答，最多 8 次已授权调用？"):
        show(
            request(
                "POST",
                f"/api/v1/knowledge/gaps/{identifier}/reanswer",
                {
                    "funding_id": config().get("funding_id"),
                    "confirm": "REANSWER",
                },
            )
        )


@app.command("gap-result")
def gap_result(identifier: str):
    show(
        request(
            "POST",
            f"/api/v1/knowledge/gaps/{identifier}/reconcile",
            {
                "confirm": "RECONCILE",
            },
        )
    )


@app.command("disputes")
def disputes(target: str | None = None, reason: str = ""):
    if target:
        show(request("POST", "/api/v1/knowledge/disputes", {"target_id": target, "reason": reason}))
    else:
        show(request("GET", "/api/v1/knowledge/disputes"))


@app.command("plan")
def plan(identifier: str, accept_version: str | None = None):
    details = request("GET", "/api/v1/plans/" + identifier)
    show(details)
    if accept_version and typer.confirm("采用选定版本的学习计划？"):
        show(
            request(
                "POST",
                f"/api/v1/plans/{identifier}/accept",
                {"version_id": accept_version, "expected_revision": details["revision"]},
            )
        )


@workspace.command("result")
def workspace_result(command: str):
    show(request("GET", f"/api/v1/commands/{command}"))


@workspace.command("submit")
def workspace_submit(
    identifier: str, explanation: Path, transfer: Path, external_help: str = "unknown"
):
    ws = request("GET", "/api/v1/workspaces/" + identifier)
    show(
        request(
            "POST",
            f"/api/v1/workspaces/{identifier}/submissions",
            {
                "expected_revision": ws["revision"],
                "explanation": explanation.read_text(encoding="utf-8"),
                "transfer_notes": transfer.read_text(encoding="utf-8"),
                "external_help": external_help,
                "idempotency_key": str(uuid4()),
            },
        )
    )


@workspace.command("review")
def workspace_review(identifier: str):
    checks = {
        k: typer.confirm(q)
        for k, q in [
            ("artifact_and_runtime", "已核对当前代码与运行观察？"),
            ("explanation", "已完成本人机制解释？"),
            ("transfer", "已说明变体与迁移差异？"),
        ]
    }
    show(
        request(
            "POST",
            f"/api/v1/submissions/{identifier}/review",
            {"checks": checks, "confirm": "REVIEW"},
        )
    )


register_control_cli(app, request, show, config, save_config)
register_learning_cli(app, request, show)
register_integration_cli(app, request, show, lambda: console, clean)

if __name__ == "__main__":
    app()
