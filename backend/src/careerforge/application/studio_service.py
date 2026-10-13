"""User-owned editable copies, reviewed edits, and explicitly confirmed commands."""

import json
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from careerforge.application.common import fingerprint, idempotency
from careerforge.application.ownership import owned
from careerforge.application.planning_service import objective_version
from careerforge.application.repository_service import citation_detail
from careerforge.application.run_service import create_run
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError, not_found
from careerforge.infrastructure.db.models import LearningWorkspace
from careerforge.schemas.studio import AgentReply

VERSION = "learning-studio-agent-1.0"
BLOCKED = {".git", ".env", ".aws", ".ssh", ".codex", "node_modules", ".venv", "__pycache__"}


def path(value):
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


def validate_files(files):
    if len(files) > 100 or sum(len(v.encode()) for v in files.values()) > 2_000_000:
        raise BusinessError(413, "WORKSPACE_TOO_LARGE", "工作区最多 100 个文本文件、共 2 MB。")
    for name, content in files.items():
        path(name)
        if "\x00" in content or len(content) > 100000:
            raise BusinessError(
                413, "TEXT_FILE_REQUIRED", "仅支持单份最多 100,000 字符的文本文件。"
            )


def workspace(db, owner, identifier, lock=False):
    return owned(db, LearningWorkspace, owner, identifier, lock=lock)


def detail(row, db=None):
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
            actions.append(item)
        value["actions"] = actions
    return value


def create(db, owner, body, key):
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


def revision(row, expected):
    if row.revision != expected:
        raise BusinessError(409, "WORKSPACE_CHANGED", "工作区已修改，请刷新并重新审阅。")


def save_file(db, owner, identifier, body):
    row = workspace(db, owner, identifier, True)
    revision(row, body.expected_revision)
    files = {**row.payload["files"], path(body.path): body.content}
    validate_files(files)
    row.payload = {**row.payload, "files": files}
    row.revision += 1
    return detail(row, db)


def action(row, identifier):
    item = next((a for a in row.payload["actions"] if a["id"] == str(identifier)), None)
    if not item:
        raise not_found()
    return item


def append(row, item):
    if len(row.payload["actions"]) >= 100:
        raise BusinessError(422, "ACTION_LIMIT", "此工作区已有 100 条记录，请创建新的练习工作区。")
    row.payload = {**row.payload, "actions": [*row.payload["actions"], item]}


def update_action(row, identifier, changes):
    row.payload = {
        **row.payload,
        "actions": [
            {**a, **changes} if a["id"] == str(identifier) else a for a in row.payload["actions"]
        ],
    }


def command_argv(argv):
    if not argv or argv[0] not in {"python", "python3", "uv", "node", "npm"}:
        raise BusinessError(
            422, "COMMAND_NOT_ALLOWED", "仅支持 Python、uv、Node 或 npm 参数化命令。"
        )
    if len(argv) > 30 or sum(len(x) for x in argv) > 4000 or any("\x00" in x for x in argv):
        raise BusinessError(422, "INVALID_COMMAND", "命令参数超过限制。")
    return argv


def propose_command(db, owner, identifier, body):
    row = workspace(db, owner, identifier, True)
    revision(row, body.expected_revision)
    append(
        row,
        {
            "id": str(uuid4()),
            "kind": "command",
            "status": "pending",
            "argv": command_argv(body.argv),
            "reason": body.reason,
            "revision": row.revision,
        },
    )
    return detail(row, db)


def apply(db, owner, identifier, action_id, body):
    row = workspace(db, owner, identifier, True)
    item = action(row, action_id)
    if item["kind"] != "patch" or body.confirmation != "APPLY":
        raise BusinessError(422, "PATCH_CONFIRMATION_REQUIRED", "请审阅并确认应用补丁。")
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


def start_agent(db, owner, identifier, body, key):
    if body.mode == "learn":
        from careerforge.application.studio_learning_service import start

        return start(db, owner, identifier, body, key)
    row = workspace(db, owner, identifier)
    record = idempotency(db, owner, f"studio.agent:{identifier}", key, body.model_dump(mode="json"))
    if record.response_summary:
        return record.response_summary
    revision(row, body.expected_revision)
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
    objective = (
        objective_version(db, owner, body.objective_version_id)
        if body.objective_version_id
        else None
    )
    context = {
        "files": files,
        "file_tree": sorted(row.payload["files"]),
        "references": references,
        "objective": objective.payload if objective else None,
        "recent_actions": [
            {"kind": "learning_report", "summary": item["guide"]["summary"]}
            if item["kind"] == "learning_report"
            else item
            for item in detail(row, db)["actions"][-4:]
        ],
        "instruction": body.instruction,
    }
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
        "citation_ids": [str(i) for i in body.citation_ids],
        "files_hash": fingerprint(row.payload["files"]),
        "model": settings.model_name,
        "base_url": settings.openai_base_url,
        "max_output_tokens": settings.model_max_output_tokens,
    }
    run = create_run(db, owner, "project_research", VERSION, manifest, call_budget=1)
    record.response_summary = {"run_id": str(run.id), "status": run.status}
    return record.response_summary


def validate_context(db, run):
    row = workspace(db, run.owner_id, UUID(run.input_manifest["workspace_id"]))
    if fingerprint(row.payload["files"]) != run.input_manifest["files_hash"]:
        raise BusinessError(409, "WORKSPACE_CHANGED", "工作区已变化，请重新发起 Agent 请求。")
    for i in run.input_manifest["citation_ids"]:
        citation_detail(db, run.owner_id, UUID(i))
    if run.input_manifest["objective_version_id"]:
        objective_version(db, run.owner_id, UUID(run.input_manifest["objective_version_id"]))
    return row


def save_reply(db, run, result):
    row = validate_context(db, run)
    reply = AgentReply.model_validate(result.structured)
    if not set(reply.citation_ids).issubset(run.input_manifest["citation_ids"]):
        raise ValueError("invented reference")
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
    append(
        row,
        {
            "id": str(uuid4()),
            "kind": "message",
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
                "status": "pending",
                "argv": argv,
                "reason": cmd.reason,
                "revision": row.revision,
            },
        )
    return {"workspace_id": str(row.id), "edits": len(reply.edits), "commands": len(commands)}


def execute_command(db, owner, identifier, action_id, body):
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
        if item["status"] != "pending":
            return detail(row, db)
        revision(row, body.expected_revision)
        if len(row.payload["actions"]) >= 99:
            raise BusinessError(422, "ACTION_LIMIT", "请创建新的练习工作区。")
        files = dict(row.payload["files"])
        argv = command_argv(item["argv"])
        update_action(row, action_id, {"status": "running"})
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
        update_action(row, action_id, {"status": "completed", **output})
        if output["files_captured"]:
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
                        "status": "pending",
                        "edits": edits,
                        "revision": body.expected_revision,
                    },
                )
        return detail(row, db)


def bind_reference(db, owner, identifier, body, key):
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
