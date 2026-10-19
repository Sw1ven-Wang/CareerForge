"""User-owned editable copies, reviewed edits, and explicitly confirmed commands."""

import json
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from careerforge.application import assistance_service as assistance
from careerforge.application.common import fingerprint, idempotency
from careerforge.application.ownership import owned
from careerforge.application.planning_service import objective_version
from careerforge.application.repository_service import citation_detail
from careerforge.application.run_service import create_run, db_now
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError, not_found
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.models import AgentRun, LearningWorkspace
from careerforge.schemas.model_io import ModelResult
from careerforge.schemas.studio import (
    AgentReply,
    CommandInput,
    ConfirmInput,
    FileInput,
    ReferenceLinkInput,
    StudioAgentInput,
    WorkspaceInput,
    WorkspaceSnapshotInput,
)

LEGACY_VERSION = "learning-studio-agent-1.0"
VERSION = "learning-studio-pi-1.0"
BLOCKED = {".git", ".env", ".aws", ".ssh", ".codex", "node_modules", ".venv", "__pycache__"}


def path(value: str) -> str:
    p = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or ":" in value
        or p.is_absolute()
        or any(
            x in {".", "..", ""} or x in BLOCKED or x.startswith(".env.") for x in value.split("/")
        )
        or any(ord(c) < 32 for c in value)
        or len(value) > 240
    ):
        raise BusinessError(422, "INVALID_WORKSPACE_PATH", "文件路径无效或包含私密/依赖目录。")
    return value


def validate_files(files: dict[str, str]) -> None:
    if len(files) > 100 or sum(len(v.encode()) for v in files.values()) > 2_000_000:
        raise BusinessError(413, "WORKSPACE_TOO_LARGE", "工作区最多 100 个文本文件、共 2 MB。")
    for name, content in files.items():
        path(name)
        if "\x00" in content or len(content) > 100000:
            raise BusinessError(
                413, "TEXT_FILE_REQUIRED", "仅支持单份最多 100,000 字符的文本文件。"
            )


def workspace(db: Session, owner: UUID, identifier: UUID, lock: bool = False) -> LearningWorkspace:
    return owned(db, LearningWorkspace, owner, identifier, lock=lock)


def detail(row: LearningWorkspace, db: Session | None = None) -> Json:
    value = {"workspace_id": str(row.id), "name": row.name, "revision": row.revision, **row.payload}
    if db is not None:
        from careerforge.application.studio_learning_service import visible_action
        from careerforge.infrastructure.db.models import AgentRun

        actions = []
        for item in row.payload["actions"]:
            if item["kind"] == "learning_report":
                item = visible_action(db, row.owner_id, item)
            elif item["kind"] == "learning_run":
                run = db.get(AgentRun, UUID(item["run_id"]))
                item = {
                    **item,
                    "status": run.status if run and run.owner_id == row.owner_id else "unavailable",
                }
            if item["kind"] == "practice" or item.get("practice_id"):
                from careerforge.application.studio_practice_service import visible

                item = visible(db, row.owner_id, row, item)
            actions.append(item)
        value["actions"] = actions
        active = active_workspace_run(db, row.owner_id, row.id)
        value["active_run"] = (
            {"run_id": str(active.id), "status": active.status, "version": active.version}
            if active
            else None
        )
    return value


def active_workspace_run(db: Session, owner: UUID, identifier: UUID) -> "AgentRun | None":
    from sqlalchemy import select

    from careerforge.infrastructure.db.models import AgentRun

    return db.scalar(
        select(AgentRun)
        .where(
            AgentRun.owner_id == owner,
            AgentRun.input_manifest["workspace_id"].astext == str(identifier),
            AgentRun.status.in_(["queued", "running", "waiting_user", "waiting_children"]),
        )
        .order_by(AgentRun.created_at)
        .limit(1)
    )


def guard_workspace_run(db: Session, owner: UUID, row: LearningWorkspace) -> None:
    active = active_workspace_run(db, owner, row.id)
    if active:
        raise BusinessError(
            409,
            "WORKSPACE_AGENT_ACTIVE",
            "工作区已有任务 " + str(active.id) + "（" + active.status + "），请等待或取消。",
        )
    if len(row.payload["actions"]) > 94:
        raise BusinessError(422, "ACTION_LIMIT", "记录容量不足，未调用模型；请整理或创建新工作区。")


def create(db: Session, owner: UUID, body: WorkspaceInput, key: str | None) -> Json:
    record = idempotency(db, owner, "studio.create", key, body.model_dump(mode="json"))
    if record.response_summary:
        return detail(workspace(db, owner, UUID(record.response_summary["workspace_id"])), db)
    validate_files(body.files)
    row = LearningWorkspace(
        owner_id=owner, name=body.name, payload={"files": body.files, "actions": []}
    )
    db.add(row)
    db.flush()
    record.response_summary = {"workspace_id": str(row.id)}
    return detail(row, db)


def revision(row: LearningWorkspace, expected: int) -> None:
    if row.revision != expected:
        raise BusinessError(409, "WORKSPACE_CHANGED", "工作区已修改，请刷新并重新审阅。")


def save_file(db: Session, owner: UUID, identifier: UUID, body: FileInput) -> Json:
    row = workspace(db, owner, identifier, True)
    revision(row, body.expected_revision)
    files = {**row.payload["files"], path(body.path): body.content}
    validate_files(files)
    row.payload = {**row.payload, "files": files}
    row.revision += 1
    return detail(row, db)


def sync_snapshot(
    db: Session, owner: UUID, identifier: UUID, body: WorkspaceSnapshotInput, key: str | None
) -> Json:
    row = workspace(db, owner, identifier, True)
    record = idempotency(
        db, owner, f"studio.snapshot:{identifier}", key, body.model_dump(mode="json")
    )
    if record.response_summary:
        return detail(row, db)
    revision(row, body.expected_revision)
    from sqlalchemy import select

    from careerforge.infrastructure.db.models import AgentRun

    active = db.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.owner_id == owner,
            AgentRun.input_manifest["workspace_id"].astext == str(identifier),
            AgentRun.status.in_(["queued", "running", "waiting_user", "waiting_children"]),
        )
        .limit(1)
    )
    if active or any(
        a["kind"] == "command" and a["status"] == "running" for a in row.payload["actions"]
    ):
        raise BusinessError(409, "WORKSPACE_BUSY", "任务执行中，不能替换输入快照。")
    validate_files(body.files)
    row.payload = {**row.payload, "files": body.files, "selected_files_only": True}
    row.revision += 1
    record.response_summary = {"revision": row.revision}
    return detail(row, db)


def action(row: LearningWorkspace, identifier: UUID | str) -> Json:
    item: Json | None = next(
        (a for a in row.payload["actions"] if a["id"] == str(identifier)), None
    )
    if not item:
        raise not_found()
    return item


def append(row: LearningWorkspace, item: Json) -> None:
    if len(row.payload["actions"]) >= 100:
        raise BusinessError(422, "ACTION_LIMIT", "此工作区已有 100 条记录，请创建新的练习工作区。")
    row.payload = {**row.payload, "actions": [*row.payload["actions"], item]}


def update_action(row: LearningWorkspace, identifier: UUID | str, changes: Json) -> None:
    row.payload = {
        **row.payload,
        "actions": [
            {**a, **changes} if a["id"] == str(identifier) else a for a in row.payload["actions"]
        ],
    }


def command_argv(argv: list[str]) -> list[str]:
    if not argv or argv[0] not in {"python", "python3", "uv", "node", "npm"}:
        raise BusinessError(
            422, "COMMAND_NOT_ALLOWED", "仅支持 Python、uv、Node 或 npm 参数化命令。"
        )
    if len(argv) > 30 or sum(len(x) for x in argv) > 4000 or any("\x00" in x for x in argv):
        raise BusinessError(422, "INVALID_COMMAND", "命令参数超过限制。")
    return argv


def require_practice(db: Session, owner: UUID, row: LearningWorkspace, item: Json) -> None:
    if item.get("practice_id"):
        from careerforge.application.studio_practice_service import validate

        validate(db, owner, row, item["practice_id"])


def propose_command(db: Session, owner: UUID, identifier: UUID, body: CommandInput) -> Json:
    row = workspace(db, owner, identifier, True)
    revision(row, body.expected_revision)
    practice_id = assistance.bound_practice(row, body.practice_id)
    require_practice(db, owner, row, {"practice_id": practice_id})
    from careerforge.infrastructure.runtime_capabilities import check_command

    capability = check_command(command_argv(body.argv))
    append(
        row,
        {
            "runtime_capabilities": capability,
            "id": str(uuid4()),
            "kind": "command",
            "status": "pending",
            "argv": command_argv(body.argv),
            "reason": body.reason,
            "practice_id": str(practice_id) if practice_id else None,
            "revision": row.revision,
        },
    )
    return detail(row, db)


def apply(db: Session, owner: UUID, identifier: UUID, action_id: UUID, body: ConfirmInput) -> Json:
    row = workspace(db, owner, identifier, True)
    item = action(row, action_id)
    if item["kind"] != "patch" or body.confirmation != "APPLY":
        raise BusinessError(422, "PATCH_CONFIRMATION_REQUIRED", "请审阅并确认应用补丁。")
    require_practice(db, owner, row, item)
    assistance.guard_patch(row, item)
    if item["status"] == "applied":
        return detail(row, db)
    revision(row, body.expected_revision)
    files = dict(row.payload["files"])
    for edit in item["edits"]:
        name = path(edit["path"])
        if files.get(name) != edit["before"]:
            raise BusinessError(409, "PATCH_BASE_CHANGED", "补丁依据的文件已变化，请重新生成。")
        if edit["after"] is None:
            files.pop(name, None)
        else:
            files[name] = edit["after"]
    validate_files(files)
    row.payload = {**row.payload, "files": files}
    row.revision += 1
    update_action(row, action_id, {"status": "applied"})
    return detail(row, db)


def start_agent(
    db: Session, owner: UUID, identifier: UUID, body: StudioAgentInput, key: str | None
) -> Json:
    if body.mode == "learn":
        if body.practice_id:
            raise BusinessError(422, "PRACTICE_MODE_REQUIRED", "请用代码补丁助手继续已有练习。")
        from careerforge.application.studio_learning_service import start

        return start(db, owner, identifier, body, key)
    row = workspace(db, owner, identifier, True)
    record = idempotency(db, owner, f"studio.agent:{identifier}", key, body.model_dump(mode="json"))
    if record.response_summary:
        return record.response_summary
    revision(row, body.expected_revision)
    guard_workspace_run(db, owner, row)
    assistance.guard_agent(db, owner, row)
    bound = assistance.bound_practice(row, body.practice_id)
    if bound:
        practice_item = action(row, bound)
        body = body.model_copy(
            update={
                "practice_id": bound,
                "objective_version_id": body.objective_version_id
                or UUID(practice_item["objective_version_id"]),
            }
        )
    settings = get_settings()
    if not settings.real_agent_enabled or not settings.openai_api_key.get_secret_value():
        raise BusinessError(409, "REAL_AGENT_DISABLED", "真实 Agent 未配置。")
    files = {}
    for name in body.paths:
        name = path(name)
        if name not in row.payload["files"]:
            raise not_found()
        files[name] = row.payload["files"][name]
    references = [citation_detail(db, owner, i) for i in body.citation_ids]
    practice = None
    practice_context = None
    if body.practice_id:
        from careerforge.application.studio_practice_service import context as practice_inputs

        practice, references, practice_context = practice_inputs(db, owner, row, body.practice_id)
        if str(body.objective_version_id) != practice["objective_version_id"]:
            raise BusinessError(422, "PRACTICE_OBJECTIVE_MISMATCH", "请选择此练习对应的学习目标。")
    objective = (
        objective_version(db, owner, body.objective_version_id)
        if body.objective_version_id
        else None
    )
    from careerforge.infrastructure.runtime_capabilities import capabilities as runtime_capabilities

    context = {
        "files": files,
        "file_tree": sorted(row.payload["files"]),
        "references": references,
        "objective": objective.payload if objective else None,
        "recent_actions": [
            {"kind": "learning_report", "summary": item["guide"]["summary"]}
            if item["kind"] == "learning_report"
            else {
                "kind": "practice",
                "exercise": item.get("exercise", ""),
                "note": item.get("note", ""),
                "status": item["status"],
            }
            if item["kind"] == "practice"
            else item
            for item in detail(row, db)["actions"][-4:]
        ],
        "instruction": body.instruction,
        "practice": practice_context,
        "runtime_capabilities": runtime_capabilities(),
    }
    if row.payload.get("selected_files_only"):
        context["recent_actions"] = []
    if len(json.dumps(context, ensure_ascii=False)) > 24000:
        raise BusinessError(
            413, "STUDIO_CONTEXT_TOO_LARGE", "上下文超过 24,000 字符，请选择更少文件。"
        )
    manifest = {
        "workspace_id": str(row.id),
        "revision": row.revision,
        "context": context,
        "objective_version_id": str(body.objective_version_id)
        if body.objective_version_id
        else None,
        "citation_ids": [i["citation_id"] for i in references],
        "practice_id": practice["id"] if practice else None,
        "practice_source_hash": practice["source_hash"] if practice else None,
        "files_hash": fingerprint(row.payload["files"]),
        "model": settings.model_name,
        "base_url": settings.openai_base_url,
        "max_output_tokens": settings.model_max_output_tokens,
        "coding_engine": "pi-sdk-1.3.0",
        "coding_call_limit": body.coding_call_limit,
        "reasoning_effort": "low",
    }
    run = create_run(
        db, owner, "project_research", VERSION, manifest, call_budget=body.coding_call_limit
    )
    record.response_summary = {"run_id": str(run.id), "status": run.status}
    return record.response_summary


def validate_context(db: Session, run: AgentRun) -> LearningWorkspace:
    row = workspace(db, run.owner_id, UUID(run.input_manifest["workspace_id"]), True)
    if fingerprint(row.payload["files"]) != run.input_manifest["files_hash"]:
        raise BusinessError(409, "WORKSPACE_CHANGED", "工作区已变化，请重新发起 Agent 请求。")
    if run.input_manifest.get("practice_id"):
        from careerforge.application.studio_practice_service import validate

        practice, _ = validate(db, run.owner_id, row, run.input_manifest["practice_id"])
        if practice["source_hash"] != run.input_manifest["practice_source_hash"]:
            raise BusinessError(409, "PRACTICE_CHANGED", "练习依据已变化。")
    for i in run.input_manifest["citation_ids"]:
        citation_detail(db, run.owner_id, UUID(i))
    if run.input_manifest["objective_version_id"]:
        objective_version(db, run.owner_id, UUID(run.input_manifest["objective_version_id"]))
    return row


def validate_reply(
    db: Session, run: AgentRun, result: ModelResult
) -> tuple[LearningWorkspace, AgentReply, list[list[str]]]:
    row = validate_context(db, run)
    reply = AgentReply.model_validate(result.structured)
    if not set(reply.citation_ids).issubset(run.input_manifest["citation_ids"]):
        raise ValueError("invented reference")
    if run.input_manifest.get("practice_id") and not reply.citation_ids:
        raise ValueError("practice reply requires a source reference")
    seen = set()
    for edit in reply.edits:
        name = path(edit.path)
        if name in seen or edit.before != run.input_manifest["context"]["files"].get(name):
            raise ValueError("invalid edit base")
        # Existing files may only be edited when actually included in model context.
        if name in row.payload["files"] and name not in run.input_manifest["context"]["files"]:
            raise ValueError("unread file edit")
        seen.add(name)
    candidate_files = dict(row.payload["files"])
    for edit in reply.edits:
        if edit.after is None:
            candidate_files.pop(edit.path, None)
        else:
            candidate_files[edit.path] = edit.after
    validate_files(candidate_files)
    commands = [command_argv(c.argv) for c in reply.commands]
    return row, reply, commands


def save_reply(db: Session, run: AgentRun, result: ModelResult) -> Json:
    row, reply, commands = validate_reply(db, run, result)
    append(
        row,
        {
            "id": str(uuid4()),
            "kind": "message",
            "practice_id": run.input_manifest.get("practice_id"),
            "status": "completed",
            "instruction": run.input_manifest["context"]["instruction"],
            "answer": reply.answer,
            "citation_ids": reply.citation_ids,
            "run_id": str(run.id),
            "model": run.input_manifest["model"],
            "usage": result.usage.model_dump(),
            "review_required": True,
        },
    )
    if reply.edits:
        append(
            row,
            {
                "id": str(uuid4()),
                "kind": "patch",
                "origin": "agent",
                "run_id": str(run.id),
                "practice_id": run.input_manifest.get("practice_id"),
                "status": "pending",
                "edits": [e.model_dump() for e in reply.edits],
                "revision": row.revision,
            },
        )
    for cmd, argv in zip(reply.commands, commands, strict=True):
        append(
            row,
            {
                "id": str(uuid4()),
                "kind": "command",
                "practice_id": run.input_manifest.get("practice_id"),
                "status": "pending",
                "argv": argv,
                "reason": cmd.reason,
                "revision": row.revision,
            },
        )
    return {"workspace_id": str(row.id), "edits": len(reply.edits), "commands": len(commands)}


def execute_command(
    db: Session, owner: UUID, identifier: UUID, action_id: UUID, body: ConfirmInput
) -> Json:
    if body.confirmation != "RUN":
        raise BusinessError(422, "COMMAND_CONFIRMATION_REQUIRED", "必须明确确认此命令。")
    from sqlalchemy.orm import sessionmaker

    from careerforge.infrastructure.db.session import get_engine
    from careerforge.infrastructure.workspace_runner import run

    factory = sessionmaker(get_engine(), expire_on_commit=False)
    with factory.begin() as stage:
        row = workspace(stage, owner, identifier, True)
        item = action(row, action_id)
        if item["kind"] != "command":
            raise not_found()
        require_practice(stage, owner, row, item)
        if item["status"] != "pending":
            return detail(row, db)
        revision(row, body.expected_revision)
        if len(row.payload["actions"]) >= 99:
            raise BusinessError(422, "ACTION_LIMIT", "请创建新的练习工作区。")
        files = dict(row.payload["files"])
        argv = command_argv(item["argv"])
        from careerforge.infrastructure.runtime_capabilities import check_command

        check_command(argv, executing=True)
        update_action(
            row,
            action_id,
            {
                "status": "running",
                "execution_input_hash": fingerprint(files),
                "execution_started_at": db_now(stage).isoformat(),
                "execution_environment": {
                    "runner_contract": "careerforge-workspace-sandbox-v1",
                    "network_policy": "none",
                    "timeout_seconds": 30,
                },
            },
        )
    try:
        output = run(files, argv)
    except BusinessError as error:
        with factory.begin() as stage:
            row = workspace(stage, owner, identifier, True)
            update_action(row, action_id, {"status": "failed", "error": error.code})
        raise
    with factory.begin() as stage:
        row = workspace(stage, owner, identifier, True)
        result_files = output.pop("result_files")
        update_action(
            row,
            action_id,
            {"status": "completed", **output, "execution_finished_at": db_now(stage).isoformat()},
        )
        try:
            require_practice(stage, owner, row, item)
            source_available = True
        except BusinessError:
            source_available = False
        if output["files_captured"] and source_available:
            edits = []
            for name in sorted(set(files) | set(result_files)):
                try:
                    path(name)
                except BusinessError:
                    continue
                if files.get(name) != result_files.get(name):
                    edits.append(
                        {
                            "path": name,
                            "before": files.get(name),
                            "after": result_files.get(name),
                            "reason": "隔离命令生成的变更",
                        }
                    )
            if edits:
                append(
                    row,
                    {
                        "id": str(uuid4()),
                        "kind": "patch",
                        "practice_id": item.get("practice_id"),
                        "command_id": str(action_id),
                        "status": "pending",
                        "edits": edits,
                        "revision": body.expected_revision,
                    },
                )
        return detail(row, db)


def bind_reference(
    db: Session, owner: UUID, identifier: UUID, body: ReferenceLinkInput, key: str | None
) -> Json:
    row = workspace(db, owner, identifier, True)
    objective_version(db, owner, body.objective_version_id)
    record = idempotency(db, owner, f"studio.link:{identifier}", key, body.model_dump(mode="json"))
    if record.response_summary:
        return detail(row, db)
    from careerforge.application.repository_service import code_citation

    citation = code_citation(
        db, owner, body.repository_snapshot_id, body.path, body.start_line, body.end_line
    )
    links = row.payload.get("links", [])
    if len(links) >= 100:
        raise BusinessError(422, "REFERENCE_LIMIT", "每个工作区最多保存 100 个代码定位。")
    row.payload = {
        **row.payload,
        "links": [
            *links,
            {
                "objective_version_id": str(body.objective_version_id),
                "citation_id": str(citation.id),
                "path": body.path,
                "start_line": body.start_line,
                "end_line": body.end_line,
            },
        ],
    }
    record.response_summary = {"citation_id": str(citation.id)}
    return detail(row, db)
