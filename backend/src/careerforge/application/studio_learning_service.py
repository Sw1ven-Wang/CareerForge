"""Read-only, bounded learning research with verified code evidence."""

import json
from collections.abc import Iterable
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application import knowledge_service as knowledge
from careerforge.application import repository_service as repositories
from careerforge.application.common import digest, fingerprint, idempotency
from careerforge.application.ownership import owned
from careerforge.application.planning_service import objective_version
from careerforge.application.run_service import create_run
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.models import (
    AgentRun,
    CodeKnowledgeIndex,
    LearningWorkspace,
    ModelCallReceipt,
    Repository,
    RepositorySnapshot,
)
from careerforge.schemas.studio import LearningGuide, StudioAgentInput

VERSION = "learning-studio-research-1.0"
TOOL_LIMIT = 8
SOURCE_LIMIT = 4
SOURCE_CHARS = 3500


def read_requirements(db: Session, owner: UUID, identifiers: Iterable[UUID | str]) -> list[Json]:
    from careerforge.application.source_service import get_snapshot, get_source
    from careerforge.infrastructure.db.models import SourceSnapshot

    values = []
    for identifier in dict.fromkeys(identifiers):
        row = owned(db, SourceSnapshot, owner, UUID(str(identifier)))
        source = get_source(db, owner, row.source_id)
        row = get_snapshot(db, owner, source.id, row.id)
        if digest(row.content) != row.content_hash:
            raise BusinessError(409, "REQUIREMENT_CONTENT_CHANGED", "需求正文哈希不一致。")
        if not row.content.strip() or len(row.content) > 6000:
            raise BusinessError(
                413,
                "REQUIREMENT_SCOPE_TOO_LARGE",
                "请选择非空且不超过 6000 字符的需求正文；长文可先导入需要学习的片段。",
            )
        values.append(
            {
                "source_snapshot_id": str(row.id),
                "content_hash": row.content_hash,
                "title": source.title or "需求资料",
                "source_type": source.source_type,
                "content": row.content,
            }
        )
    return values


def validate_requirements(db: Session, owner: UUID, requirements: list[Json]) -> None:
    current = read_requirements(db, owner, [r["source_snapshot_id"] for r in requirements])
    if [(r["source_snapshot_id"], r["content_hash"]) for r in current] != [
        (r["source_snapshot_id"], r["content_hash"]) for r in requirements
    ]:
        raise BusinessError(409, "REQUIREMENT_CONTENT_CHANGED", "冻结需求已变化。")


def requirement_links(
    guide: LearningGuide, requirements: list[Json], code_sources: list[Json]
) -> list[Json]:
    allowed = {r["source_snapshot_id"]: r for r in requirements}
    citations = {r["citation_id"] for r in code_sources}
    if requirements and guide.status == "grounded" and not guide.requirement_links:
        raise ValueError("grounded requirement research needs requirement-to-code links")
    links = []
    for item in guide.requirement_links:
        source = allowed.get(item.source_snapshot_id)
        if (
            source is None
            or item.citation_id not in citations
            or source["content"].count(item.quote) != 1
        ):
            raise ValueError("invented or ambiguous requirement link")
        offset = source["content"].index(item.quote)
        links.append(
            {
                **item.model_dump(),
                "source_title": source["title"],
                "content_hash": source["content_hash"],
                "quote_start": offset,
                "quote_end": offset + len(item.quote),
            }
        )
    return links


def start(
    db: Session, owner: UUID, workspace_id: UUID, body: StudioAgentInput, key: str | None
) -> Json:
    workspace = owned(db, LearningWorkspace, owner, workspace_id)
    if not body.objective_version_id and not body.source_snapshot_ids:
        raise BusinessError(
            422,
            "LEARNING_OBJECTIVE_REQUIRED",
            "请选择需求资料或已有学习目标，再让 Agent 查找对应实现。",
        )
    objective = (
        objective_version(db, owner, body.objective_version_id)
        if body.objective_version_id
        else None
    )
    requirements = read_requirements(db, owner, body.source_snapshot_ids)
    record = idempotency(
        db, owner, f"studio.learn:{workspace_id}", key, body.model_dump(mode="json")
    )
    if record.response_summary:
        return record.response_summary
    workspace = owned(db, LearningWorkspace, owner, workspace_id, lock=True)
    active = db.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.owner_id == owner,
            AgentRun.workflow_version == VERSION,
            AgentRun.input_manifest["workspace_id"].astext == str(workspace_id),
            AgentRun.status.in_(["queued", "running", "waiting_user"]),
        )
        .limit(1)
    )
    if active:
        raise BusinessError(
            409, "LEARNING_RUN_ACTIVE", "此工作区已有学习说明任务，请等待或取消后再发起。"
        )
    if workspace.revision != body.expected_revision:
        raise BusinessError(409, "WORKSPACE_CHANGED", "工作区已变化，请刷新。")
    from careerforge.application.assistance_service import guard_agent

    guard_agent(db, owner, workspace)
    settings = get_settings()
    if not settings.real_agent_enabled or not settings.openai_api_key.get_secret_value():
        raise BusinessError(409, "REAL_AGENT_DISABLED", "真实 Agent 未配置。")
    if body.knowledge_snapshot_ids:
        ids = list(dict.fromkeys(body.knowledge_snapshot_ids))
    else:
        ids = list(
            db.scalars(
                select(CodeKnowledgeIndex.snapshot_id)
                .join(RepositorySnapshot, RepositorySnapshot.id == CodeKnowledgeIndex.snapshot_id)
                .join(Repository, Repository.id == RepositorySnapshot.repository_id)
                .where(CodeKnowledgeIndex.owner_id == owner, Repository.revoked_at.is_(None))
                .order_by(CodeKnowledgeIndex.created_at.desc())
                .limit(11)
            )
        )
    if not ids or len(ids) > 10:
        raise BusinessError(
            422, "KNOWLEDGE_SCOPE_REQUIRED", "请选择 1–10 个已索引项目版本；没有资料时请先导入。"
        )
    scope = []
    for identifier in ids:
        snapshot = repositories.snapshot(db, owner, identifier)
        index = knowledge.index_row(db, identifier)
        if index is None:
            raise BusinessError(422, "KNOWLEDGE_NOT_INDEXED", "所选项目尚未建立索引。")
        scope.append(
            {
                "snapshot_id": str(identifier),
                "manifest_hash": snapshot.manifest_hash,
                "commit": snapshot.commit_sha,
                "name": index.payload.get("name", ""),
                "description": index.payload.get("description", "")[:300],
                "tags": index.payload.get("tags", [])[:10],
            }
        )
    context: Json = {
        "objective": (
            {
                k: objective.payload.get(k)
                for k in ("title", "target_behavior", "scope", "deliverables")
            }
            if objective
            else {
                "title": " / ".join(r["title"] for r in requirements)[:150],
                "target_behavior": "分析需求并找到开源项目中对应的实现",
                "scope": "本轮选择的需求正文与开源版本",
            }
        ),
        "requirements_untrusted": requirements,
        "instruction": body.instruction,
    }
    if len(json.dumps(context, ensure_ascii=False)) > 24000:
        raise BusinessError(
            413, "LEARNING_CONTEXT_TOO_LARGE", "需求、目标与请求超过 24,000 字符，请缩小学习范围。"
        )
    pending_id = str(uuid4())
    manifest = {
        "pending_action_id": pending_id,
        "workspace_id": str(workspace_id),
        "objective_version_id": str(objective.id) if objective else None,
        "objective_hash": fingerprint(objective.payload) if objective else None,
        "scope": scope,
        "context": context,
        "model": settings.model_name,
        "base_url": settings.openai_base_url,
        "max_output_tokens": settings.model_max_output_tokens,
        "model_call_limit": body.model_call_limit,
        "tool_limit": TOOL_LIMIT,
        "source_limit": SOURCE_LIMIT,
        "read_only": True,
    }
    run = create_run(
        db,
        owner,
        "project_research",
        VERSION,
        manifest,
        call_budget=body.model_call_limit,
        goal_version_id=objective.goal_version_id if objective else None,
    )
    from careerforge.application.studio_service import append

    append(
        workspace,
        {
            "id": pending_id,
            "kind": "learning_run",
            "status": "queued",
            "run_id": str(run.id),
            "objective_version_id": str(objective.id) if objective else None,
            "objective_title": context["objective"].get("title", ""),
            "instruction": body.instruction,
        },
    )
    record.response_summary = {"run_id": str(run.id), "status": run.status, "mode": "learn"}
    return record.response_summary


def validate_inputs(db: Session, run: AgentRun) -> LearningWorkspace:
    data = run.input_manifest
    workspace = owned(db, LearningWorkspace, run.owner_id, UUID(data["workspace_id"]))
    if data["objective_version_id"]:
        objective = objective_version(db, run.owner_id, UUID(data["objective_version_id"]))
        if fingerprint(objective.payload) != data["objective_hash"]:
            raise BusinessError(409, "LEARNING_OBJECTIVE_CHANGED", "冻结学习目标已变化。")
    validate_requirements(db, run.owner_id, data["context"].get("requirements_untrusted", []))
    for item in data["scope"]:
        snapshot = repositories.snapshot(db, run.owner_id, UUID(item["snapshot_id"]))
        if snapshot.manifest_hash != item["manifest_hash"]:
            raise BusinessError(409, "KNOWLEDGE_SCOPE_CHANGED", "冻结项目版本已变化。")
    return workspace


def brief(item: Json) -> Json:
    return {
        key: item[key]
        for key in (
            "chunk_id",
            "repository_snapshot_id",
            "commit",
            "path",
            "symbol",
            "kind",
            "start_line",
            "end_line",
        )
    }


def read_source(
    db: Session, owner: UUID, item: Json, start: int | None = None, end: int | None = None
) -> Json:
    start = start if start is not None else item["start_line"]
    end = end if end is not None else item["end_line"]
    if not item["start_line"] <= start <= end <= item["end_line"]:
        raise BusinessError(422, "INVALID_CODE_RANGE", "读取范围必须位于已发现片段内。")
    lines = item["content"].splitlines(keepends=True)[
        start - item["start_line"] : end - item["start_line"] + 1
    ]
    selected, size = [], 0
    for line in lines:
        if size + len(line) > SOURCE_CHARS:
            break
        selected.append(line)
        size += len(line)
    if not selected:
        raise BusinessError(
            413, "LEARNING_SOURCE_TOO_LARGE", "此代码行超过上下文预算，请选择更小片段。"
        )
    actual_end = start + len(selected) - 1
    # Create an exact, bounded citation. Existing chunk text was already hash-checked.
    from careerforge.infrastructure.db.models import CodeCitation

    citation = db.scalar(
        select(CodeCitation).where(
            CodeCitation.owner_id == owner,
            CodeCitation.snapshot_id == UUID(item["repository_snapshot_id"]),
            CodeCitation.path == item["path"],
            CodeCitation.start_line == start,
            CodeCitation.end_line == actual_end,
        )
    )
    if citation is None:
        citation = repositories.code_citation(
            db, owner, UUID(item["repository_snapshot_id"]), item["path"], start, actual_end
        )
    return {
        **repositories.citation_detail(db, owner, citation.id),
        "chunk_id": item["chunk_id"],
        "symbol": item["symbol"],
        "context_truncated": actual_end < end,
    }


def parse_guide(structured: Any) -> tuple[LearningGuide, list[Json]]:
    """Only repair missing presentation labels; never alter facts or quotations."""
    from careerforge.schemas.studio import LearningGuide

    value = deepcopy(structured)
    repairs = []
    if isinstance(value, dict) and isinstance(value.get("points"), list):
        for index, point in enumerate(value["points"]):
            if isinstance(point, dict) and "title" not in point:
                point["title"] = f"学习点 {index + 1}"
                repairs.append(
                    {"field": f"points.{index}.title", "action": "generated_display_label"}
                )
    return LearningGuide.model_validate(value), repairs


def validate_guide(
    db: Session, owner: UUID, guide: LearningGuide, sources: list[Json]
) -> tuple[list[Json], list[Json]]:
    allowed = {item["citation_id"]: item for item in sources}
    if guide.status == "grounded" and not guide.points:
        raise ValueError("grounded report needs quoted evidence")
    if guide.status == "insufficient_evidence" and (guide.points or guide.reading_order):
        raise ValueError("insufficient report cannot assert evidence")
    if not set(guide.reading_order).issubset(allowed):
        raise ValueError("unread reading reference")
    points = []
    for point in guide.points:
        if point.citation_id not in allowed or not point.quote.strip():
            raise ValueError("unread code reference")
        source = repositories.citation_detail(db, owner, UUID(point.citation_id))
        if source["content_hash"] != allowed[point.citation_id]["content_hash"]:
            raise ValueError("changed code reference")
        offset = source["quote"].find(point.quote)
        if offset < 0:
            raise ValueError("quote not found verbatim")
        first = source["start_line"] + source["quote"][:offset].count("\n")
        last = first + point.quote.rstrip("\n").count("\n")
        points.append(
            {
                **point.model_dump(),
                "path": source["path"],
                "commit": source["commit"],
                "quote_start_line": first,
                "quote_end_line": last,
                "quote_char_start": offset,
                "quote_char_end": offset + len(point.quote),
            }
        )
    used = set(guide.reading_order) | {p.citation_id for p in guide.points}
    return points, [allowed[i] for i in allowed if i in used]


def usage(db: Session, run_id: UUID) -> Json:
    receipts = list(db.scalars(select(ModelCallReceipt).where(ModelCallReceipt.run_id == run_id)))
    known = all(
        r.status == "completed" and r.usage and r.usage.get("status") == "known" for r in receipts
    )
    return {
        "calls": len(receipts),
        "input_tokens": sum(r.usage["input_tokens"] for r in receipts if r.usage is not None)
        if known
        else None,
        "output_tokens": sum(r.usage["output_tokens"] for r in receipts if r.usage is not None)
        if known
        else None,
        "status": "known" if known else "unknown",
        "cost_status": "not_priced",
    }


def save(
    db: Session,
    run: AgentRun,
    guide: LearningGuide,
    sources: list[Json],
    trace: list[Json],
    synthetic: bool = False,
    generated_by: str = "model",
    repairs: list[Json] | None = None,
) -> Json:
    validate_inputs(db, run)
    points, used = validate_guide(db, run.owner_id, guide, sources)
    requirements = run.input_manifest["context"].get("requirements_untrusted", [])
    links = requirement_links(guide, requirements, used)
    workspace = owned(
        db, LearningWorkspace, run.owner_id, UUID(run.input_manifest["workspace_id"]), lock=True
    )
    from careerforge.application.studio_service import update_action

    identifier = run.input_manifest["pending_action_id"]
    update_action(
        workspace,
        identifier,
        {
            "id": identifier,
            "kind": "learning_report",
            "status": "completed",
            "objective_version_id": run.input_manifest["objective_version_id"],
            "objective_title": run.input_manifest["context"]["objective"].get("title", ""),
            "instruction": run.input_manifest["context"]["instruction"],
            "guide": {**guide.model_dump(), "points": points, "requirement_links": links},
            "requirements": [{k: v for k, v in r.items() if k != "content"} for r in requirements],
            "sources": used,
            "trace": trace,
            "run_id": str(run.id),
            "model": run.input_manifest["model"],
            "usage": usage(db, run.id),
            "synthetic": synthetic,
            "generated_by": generated_by,
            "compatibility_repairs": repairs or [],
            "review_required": True,
            "executed": False,
        },
    )
    return {
        "workspace_id": str(workspace.id),
        "learning_report_id": identifier,
        "citation_ids": [i["citation_id"] for i in used],
        "points": len(points),
        "evidence_status": guide.status,
        "executed": False,
    }


def visible_action(db: Session, owner: UUID, item: Json) -> Json:
    try:
        if item.get("objective_version_id"):
            objective_version(db, owner, UUID(item["objective_version_id"]))
        validate_requirements(db, owner, item.get("requirements", []))
        for source in item.get("sources", []):
            repositories.citation_detail(db, owner, UUID(source["citation_id"]))
    except (BusinessError, ValueError):
        return {
            "id": item["id"],
            "kind": "learning_report",
            "status": "unavailable",
            "guide": {
                "status": "insufficient_evidence",
                "summary": "学习目标或参考代码已不可用，历史说明已隐藏。",
                "points": [],
                "reading_order": [],
                "practice": [],
                "questions": [],
                "limitations": [],
            },
            "sources": [],
            "trace": [],
        }
    return item
