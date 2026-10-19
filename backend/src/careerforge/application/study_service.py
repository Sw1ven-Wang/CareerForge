"""Owner-scoped, versioned Agent study pipeline; runtime I/O lives in workflows."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application import coverage_service as coverage_rules
from careerforge.application import knowledge_service as knowledge
from careerforge.application import repository_service as repositories
from careerforge.application.common import check_revision, idempotency
from careerforge.application.ownership import owned
from careerforge.application.run_service import create_run
from careerforge.application.studio_learning_service import read_requirements, validate_requirements
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.models import AgentRun
from careerforge.infrastructure.db.study_models import StudyArtifact, StudyTrack
from careerforge.schemas.study import (
    CandidateInput,
    ExtractionRecovery,
    ModuleProgress,
    ReviewOutput,
    RoleInput,
    TrackInput,
    TrackSettings,
)

VERSION = "agent-study-2.2"
ROLE_TYPES = {
    "extract": "market_research",
    "discover": "project_research",
    "review": "project_research",
    "plan": "learning_path",
    "tutor": "learning_path",
    "reconcile": "learning_path",
}


def track(db: Session, owner: UUID, identifier: UUID, lock: bool = False) -> StudyTrack:
    row = owned(db, StudyTrack, owner, identifier, lock=lock)
    read_requirements(db, owner, row.payload["source_snapshot_ids"])
    return row


def artifact_row(
    db: Session, owner: UUID, track_id: UUID, identifier: UUID | str, kind: str | None = None
) -> StudyArtifact:
    row = owned(db, StudyArtifact, owner, UUID(str(identifier)))
    if row.track_id != track_id or (kind and row.kind != kind):
        raise BusinessError(404, "NOT_FOUND", "学习产物不存在。")
    return row


def put(
    db: Session, row: StudyTrack, kind: str, payload: dict[str, Any], run_id: UUID | None = None
) -> StudyArtifact:
    item = StudyArtifact(
        owner_id=row.owner_id, track_id=row.id, kind=kind, payload=payload, run_id=run_id
    )
    db.add(item)
    db.flush()
    return item


def packed(row: StudyArtifact) -> dict[str, Any]:
    payload = dict(row.payload)
    # Old immutable artifacts receive conservative display metadata; they are not rewritten.
    for field in ("highlights", "points", "focus_highlights"):
        if field in payload:
            payload[field] = [
                {**point, "verification": coverage_rules.evidence_verification()}
                for point in payload[field]
            ]
    return {
        "id": str(row.id),
        "kind": row.kind,
        "run_id": str(row.run_id) if row.run_id else None,
        "created_at": row.created_at.isoformat(),
        **payload,
    }


def current_requirements(db: Session, row: StudyTrack) -> StudyArtifact:
    identifier = row.payload.get("requirements_id")
    if not identifier:
        raise BusinessError(409, "EXTRACT_REQUIRED", "请先提取技术需求。")
    return artifact_row(db, row.owner_id, row.id, identifier, "requirements")


def current_reviews(db: Session, row: StudyTrack) -> list[StudyArtifact]:
    requirements_id = row.payload.get("requirements_id")
    rows = db.scalars(
        select(StudyArtifact)
        .where(
            StudyArtifact.track_id == row.id,
            StudyArtifact.owner_id == row.owner_id,
            StudyArtifact.kind == "review",
        )
        .order_by(StudyArtifact.created_at.desc(), StudyArtifact.id)
    ).all()
    seen, values = set(), []
    for item in rows:
        value = item.payload
        if value.get("requirements_id") != requirements_id:
            continue
        url = value["candidate"]["github_url"]
        if url in seen:
            continue
        seen.add(url)
        values.append(item)
    return values


def approved(db: Session, row: StudyTrack) -> list[StudyArtifact]:
    values = []
    for item in current_reviews(db, row):
        if not item.payload.get("admitted"):
            continue
        try:
            repositories.snapshot(db, row.owner_id, UUID(item.payload["repository_snapshot_id"]))
            for point in item.payload["highlights"]:
                repositories.citation_detail(db, row.owner_id, UUID(point["citation_id"]))
        except BusinessError:
            continue
        values.append(item)
    return values


def idle(db: Session, row: StudyTrack) -> None:
    identifier = row.payload.get("active_run_id")
    run = db.get(AgentRun, UUID(identifier)) if identifier else None
    if run and run.status in {"queued", "running", "waiting_user", "waiting_children"}:
        raise BusinessError(409, "STUDY_RUN_ACTIVE", "此方向已有 Agent 运行，请等待或取消。")


def create(db: Session, owner: UUID, body: TrackInput, key: str | None) -> dict[str, Any]:
    sources = read_requirements(db, owner, body.source_snapshot_ids)
    record = idempotency(db, owner, "study.create", key, body.model_dump(mode="json"))
    if record.response_summary:
        return detail(db, track(db, owner, UUID(record.response_summary["track_id"])))
    row = StudyTrack(
        owner_id=owner,
        name=body.name,
        payload={
            "source_snapshot_ids": [s["source_snapshot_id"] for s in sources],
            "target_count": body.target_count,
            "collection_policy": body.collection_policy
            or ("strict_count" if "target_count" in body.model_fields_set else "coverage"),
            "progress": {},
        },
    )
    db.add(row)
    db.flush()
    record.response_summary = {"track_id": str(row.id)}
    return detail(db, row)


def listing(db: Session, owner: UUID) -> list[dict[str, Any]]:
    values = []
    for row in db.scalars(
        select(StudyTrack)
        .where(StudyTrack.owner_id == owner)
        .order_by(StudyTrack.created_at.desc())
        .limit(100)
    ):
        try:
            track(db, owner, row.id)
            values.append(
                {
                    "track_id": str(row.id),
                    "name": row.name,
                    "revision": row.revision,
                    "available": True,
                }
            )
        except BusinessError:
            values.append(
                {"track_id": str(row.id), "name": "来源已撤销的学习方向", "available": False}
            )
    return values


def visible_review(
    db: Session, owner: UUID, item: StudyArtifact, accepted_ids: set[str]
) -> dict[str, Any]:
    payload = item.payload
    identifier = payload.get("repository_snapshot_id") or payload["candidate"].get(
        "repository_snapshot_id"
    )
    available = not payload.get("admitted") or str(item.id) in accepted_ids
    if identifier:
        try:
            repositories.snapshot(db, owner, UUID(identifier))
        except BusinessError:
            available = False
    if available:
        return {**packed(item), "available": True}
    return {
        "id": str(item.id),
        "candidate_id": payload["candidate_id"],
        "available": False,
        "candidate": {"full_name": "项目已撤销"},
        "verdict": "uncertain",
        "admitted": False,
        "summary": "项目依据不可用，已隐藏派生内容。",
        "technology_stack": [],
        "quality_notes": [],
        "highlights": [],
        "limitations": [],
    }


def extraction_preview(db: Session, row: StudyTrack, run: AgentRun | None) -> dict[str, Any] | None:
    """Expose usable saved material as an unpublished preview; never bypass the budget gate."""
    from pydantic import ValidationError

    from careerforge.application import study_validation as validation
    from careerforge.infrastructure.db.models import StageArtifact
    from careerforge.schemas.model_io import ModelResult
    from careerforge.schemas.study import ExtractOutput

    if (
        not run
        or run.owner_id != row.owner_id
        or run.status != "failed"
        or run.input_manifest.get("role") != "extract"
    ):
        return None
    saved = db.scalar(
        select(StageArtifact).where(
            StageArtifact.run_id == run.id,
            StageArtifact.stage_key == "extract",
            StageArtifact.stage_version == run.workflow_version,
        )
    )
    if not saved:
        return None
    try:
        result = ModelResult.model_validate(saved.payload)
        if result.finish_reason != "stop":
            return None
        original = ExtractOutput.model_validate(result.structured)
        sources = run.input_manifest["sources"]
        validate_requirements(db, row.owner_id, sources)
        normalized, repairs = validation.normalize_depths(original, sources)
        valid, unresolved, seen = [], [], set()
        for item in normalized.requirements:
            try:
                if item.key in seen:
                    raise ValueError("duplicate requirement key")
                seen.add(item.key)
                valid.extend(
                    validation.extracted(
                        ExtractOutput(requirements=[item], limitations=[]), sources
                    )
                )
            except ValueError:
                unresolved.append(
                    {
                        "key": item.key,
                        "technology": item.technology,
                        "reason": "原文摘录无法唯一定位，未作为有效技术需求发布。",
                    }
                )
        requested = run.input_manifest["max_output_tokens"]
        reported = result.usage.output_tokens
        return {
            "id": str(saved.id),
            "run_id": str(run.id),
            "synthetic": result.synthetic,
            "status": "unpublished",
            "requirements": valid,
            "unresolved": unresolved,
            "normalization_repairs": [r for r in repairs if r["key"] in {v["key"] for v in valid}],
            "limitations": [
                *original.limitations,
                "仅展示已保存结果，不是提取成功；未进入调研、审核或计划链路。",
            ],
            "budget_warning": {"requested": requested, "reported": reported}
            if reported is not None and reported > requested
            else None,
            "usage": result.usage.model_dump(),
        }
    except (ValueError, ValidationError, KeyError):
        return None


def detail(db: Session, row: StudyTrack) -> dict[str, Any]:
    # Reauthorize before returning derived private text, even on idempotent replay.
    sources = read_requirements(db, row.owner_id, row.payload["source_snapshot_ids"])
    items = list(
        db.scalars(
            select(StudyArtifact)
            .where(StudyArtifact.track_id == row.id, StudyArtifact.owner_id == row.owner_id)
            .order_by(StudyArtifact.created_at, StudyArtifact.id)
        )
    )
    reviews = current_reviews(db, row)
    accepted = approved(db, row)
    accepted_ids = {str(i.id) for i in accepted}
    current_id = row.payload.get("requirements_id")
    candidates = [
        packed(i)
        for i in items
        if i.kind == "candidate" and i.payload.get("requirements_id") == current_id
    ]
    requirements = next((packed(i) for i in items if str(i.id) == current_id), None)
    plans: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    for item in items:
        if item.kind not in {"plan", "tutor"}:
            continue
        try:
            plan = (
                item
                if item.kind == "plan"
                else artifact_row(db, row.owner_id, row.id, item.payload["plan_id"], "plan")
            )
            validate_plan_scope(db, row, plan)
        except BusinessError:
            continue
        value = packed(item)
        if item.kind == "plan":
            from careerforge.application.study_versions import metadata

            value.update(metadata(db, item, current_id))
        if item.kind == "plan" and value.get("module_outcomes"):
            from careerforge.application.studio_delivery_service import effective_outcome

            value["module_outcomes"] = {
                k: effective_outcome(db, row.owner_id, v)
                for k, v in value["module_outcomes"].items()
            }
        (plans if item.kind == "plan" else turns).append(value)
    run = (
        db.get(AgentRun, UUID(row.payload["active_run_id"]))
        if row.payload.get("active_run_id")
        else None
    )
    from careerforge.application.assistance_service import active_independent

    return {
        "track_id": str(row.id),
        "name": row.name,
        "revision": row.revision,
        "sources": [{k: v for k, v in s.items() if k != "content"} for s in sources],
        "target_count": row.payload["target_count"],
        "approved_count": len(accepted),
        "collection_policy": coverage_rules.collection_policy(row.payload),
        "coverage": coverage_rules.coverage(
            requirements["requirements"] if requirements else [], [packed(i) for i in accepted]
        ),
        "requirements": requirements,
        "extraction_preview": extraction_preview(db, row, run) if not requirements else None,
        "candidates": candidates,
        "reviews": [visible_review(db, row.owner_id, i, accepted_ids) for i in reviews],
        "approved_projects": [packed(i) for i in accepted],
        "plans": plans,
        "active_plan_id": row.payload.get("active_plan_id"),
        "turns": turns,
        "reconciliations": [packed(i) for i in items if i.kind == "reconciliation"],
        "recheck_pending": bool(row.payload.get("recheck_pending")),
        "progress": row.payload.get("progress", {}),
        "independent_attempt_active": active_independent(db, row.owner_id) is not None,
        "active_run": {
            "run_id": str(run.id),
            "status": run.status,
            "role": run.input_manifest["role"],
            "version": run.version,
        }
        if run
        else None,
    }


def configure(db: Session, owner: UUID, identifier: UUID, body: TrackSettings) -> dict[str, Any]:
    row = track(db, owner, identifier, True)
    check_revision(row.revision, body.expected_revision)
    idle(db, row)
    row.payload = {
        **row.payload,
        "target_count": body.target_count,
        "collection_policy": body.collection_policy or "strict_count",
    }
    row.revision += 1
    return detail(db, row)


def add_candidate(
    db: Session, owner: UUID, identifier: UUID, body: CandidateInput, key: str | None
) -> dict[str, Any]:
    row = track(db, owner, identifier, True)
    record = idempotency(
        db, owner, f"study.candidate:{identifier}", key, body.model_dump(mode="json")
    )
    if record.response_summary:
        return detail(db, row)
    check_revision(row.revision, body.expected_revision)
    idle(db, row)
    requirements = current_requirements(db, row)
    project = knowledge.project_detail(db, owner, body.repository_snapshot_id)
    existing = db.scalar(
        select(StudyArtifact.id)
        .where(
            StudyArtifact.track_id == row.id,
            StudyArtifact.kind == "candidate",
            StudyArtifact.payload["requirements_id"].astext == str(requirements.id),
            StudyArtifact.payload["github_url"].astext == project["github_url"],
        )
        .limit(1)
    )
    if existing:
        raise BusinessError(409, "CANDIDATE_EXISTS", "此仓库已在当前候选中，无需重复加入。")
    payload = {
        "requirements_id": str(requirements.id),
        "repository_snapshot_id": str(body.repository_snapshot_id),
        "full_name": project["github_url"].removeprefix("https://github.com/"),
        "github_url": project["github_url"],
        "commit": project["commit"],
        "name": project["name"],
        "origin": "user_import",
        "description": project.get("description", ""),
        "license": project["license_status"],
        "archived": None,
        "fork": None,
    }
    item = put(db, row, "candidate", payload)
    row.revision += 1
    record.response_summary = {"study_artifact_id": str(item.id)}
    return detail(db, row)


def validate_plan_scope(db: Session, row: StudyTrack, plan: StudyArtifact) -> None:
    from careerforge.application.study_versions import frozen_scope

    if plan.track_id != row.id or plan.owner_id != row.owner_id:
        raise BusinessError(404, "NOT_FOUND", "计划不属于当前学习方向。")
    frozen_scope(db, plan)


def start(
    db: Session, owner: UUID, identifier: UUID, body: RoleInput, key: str | None
) -> dict[str, Any]:
    row = track(db, owner, identifier, True)
    record = idempotency(db, owner, f"study.run:{identifier}", key, body.model_dump(mode="json"))
    if record.response_summary:
        run = owned(db, AgentRun, owner, UUID(record.response_summary["run_id"]))
        validate_run(db, run)
        return record.response_summary
    check_revision(row.revision, body.expected_revision)
    idle(db, row)
    settings = get_settings()
    if not settings.real_agent_enabled or not settings.openai_api_key.get_secret_value():
        raise BusinessError(409, "REAL_AGENT_DISABLED", "真实 Agent 未配置。")
    if body.role == "tutor":
        from careerforge.application.assistance_service import guard_agent

        guard_agent(db, owner)
    source_values = read_requirements(db, owner, row.payload["source_snapshot_ids"])
    from careerforge.infrastructure.runtime_capabilities import capabilities

    manifest: Json = {
        "track_id": str(row.id),
        "role": body.role,
        "runtime_capabilities": capabilities(),
        "source_snapshot_ids": [s["source_snapshot_id"] for s in source_values],
        "sources": source_values,
        "model": settings.model_name,
        "base_url": settings.openai_base_url,
        "max_output_tokens": min(body.max_output_tokens, settings.study_max_output_tokens),
        "model_call_limit": (
            max(1, body.recheck_call_budget)
            if body.role == "reconcile"
            else body.model_call_limit
            if body.role == "tutor"
            else 1 + body.auto_review_count * body.review_call_limit
            if body.role == "discover"
            else body.review_call_limit
            if body.role == "review"
            else 1
        ),
        "auto_review_count": body.auto_review_count if body.role == "discover" else 0,
        "review_call_limit": body.review_call_limit,
        "synthetic": False,
    }
    if body.role != "extract":
        requirements = current_requirements(db, row)
        manifest.update(
            requirements_id=str(requirements.id), requirements=requirements.payload["requirements"]
        )
    if body.role == "discover":
        located = coverage_rules.coverage(
            manifest["requirements"], [packed(i) for i in approved(db, row)]
        )
        keys = body.research_requirement_keys
        if keys is None:
            keys = located["missing_keys"]
            if not keys and coverage_rules.collection_policy(row.payload) == "strict_count":
                keys = [r["key"] for r in manifest["requirements"]]
        allowed = {r["key"] for r in manifest["requirements"]}
        if not keys:
            raise BusinessError(
                409, "COVERAGE_ALREADY_LOCATED", "已有位置覆盖；请选择需要补充调研的技术。"
            )
        if not set(keys) <= allowed:
            raise BusinessError(422, "INVALID_RESEARCH_SCOPE", "调研技术不属于当前需求版本。")
        manifest["research_requirement_keys"] = keys
        manifest["requirements"] = [r for r in manifest["requirements"] if r["key"] in keys]
        manifest["research_feedback"] = [
            {
                "repository": i.payload["candidate"]["github_url"],
                "verdict": i.payload["verdict"],
                "summary": i.payload["summary"][:400],
                "admitted": i.payload["admitted"],
            }
            for i in current_reviews(db, row)[:15]
            if visible_review(db, owner, i, {str(r.id) for r in approved(db, row)})["available"]
        ]
    if body.role == "review":
        candidate = (
            artifact_row(db, owner, row.id, body.candidate_id, "candidate")
            if body.candidate_id
            else None
        )
        if not candidate or candidate.payload["requirements_id"] != str(requirements.id):
            raise BusinessError(422, "CANDIDATE_REQUIRED", "请选择当前需求版本的候选项目。")
        manifest["candidate_id"] = str(candidate.id)
        manifest["candidate"] = candidate.payload
    if body.role == "plan":
        accepted = approved(db, row)
        if not body.intent:
            raise BusinessError(422, "LEARNING_INTENT_REQUIRED", "请选择学习意图、技术需求与项目。")
        selected = {str(i) for i in body.intent.project_review_ids}
        selected_requirements = set(body.intent.requirement_keys)
        if not selected <= {str(i.id) for i in accepted} or not selected_requirements <= {
            i["key"] for i in manifest["requirements"]
        }:
            raise BusinessError(422, "INVALID_PLAN_SCOPE", "所选技术或项目不属于当前审核版本。")
        manifest["intent"] = body.intent.model_dump(mode="json")
        manifest["projects"] = [packed(i) for i in accepted if str(i.id) in selected]
        from careerforge.infrastructure.runtime_capabilities import planning

        manifest["runtime_capabilities"] = planning(
            manifest["intent"], manifest["requirements"], manifest["projects"]
        )
        if body.intent.focus_chunk_id:
            from pathlib import PurePosixPath

            from careerforge.application.study_validation import CODE_SUFFIXES

            chunk = knowledge.chunk_detail(db, owner, body.intent.focus_chunk_id)
            snapshots = {p["repository_snapshot_id"] for p in manifest["projects"]}
            if (
                chunk["repository_snapshot_id"] not in snapshots
                or PurePosixPath(chunk["path"]).suffix.lower() not in CODE_SUFFIXES
            ):
                raise BusinessError(
                    422, "FOCUS_OUTSIDE_PROJECT", "请选择所选审核项目中的源码片段。"
                )
            manifest["focus_chunk"] = {
                k: chunk[k] for k in ("chunk_id", "repository_snapshot_id", "content_hash")
            }
        manifest["planning_scope"] = coverage_rules.require_plan_coverage(
            row.payload,
            manifest["requirements"],
            manifest["projects"],
            selected_requirements,
            len(accepted),
            body.acknowledge_collection_gap,
        )
    if body.role == "tutor":
        if not body.plan_id or not body.module_key or not body.question.strip():
            raise BusinessError(422, "TUTOR_INPUT_REQUIRED", "请选择计划模块并输入问题。")
        plan = artifact_row(db, owner, row.id, body.plan_id, "plan")
        validate_plan_scope(db, row, plan)
        module = next((m for m in plan.payload["modules"] if m["key"] == body.module_key), None)
        if not module:
            raise BusinessError(422, "MODULE_NOT_FOUND", "计划模块不存在。")
        reviews = [
            artifact_row(db, owner, row.id, i, "review") for i in module["project_review_ids"]
        ]
        plan_requirements = artifact_row(
            db, owner, row.id, plan.payload["requirements_id"], "requirements"
        )
        manifest.update(
            requirements_id=str(plan_requirements.id),
            requirements=plan_requirements.payload["requirements"],
            intent=plan.payload["intent"],
            reading=body.reading.model_dump(mode="json") if body.reading else {},
            plan_id=str(plan.id),
            module=module,
            question=body.question,
            review_ids=[str(i.id) for i in reviews],
            scope=[i.payload["repository_snapshot_id"] for i in reviews],
        )
        from careerforge.application.context_service import capture

        manifest["context_scope"] = capture(db, owner, manifest)
    if body.role in {"extract", "review", "discover", "reconcile"}:
        from careerforge.application.study_versions import heads

        manifest["recheck_plan_ids"] = [str(p.id) for p in heads(db, owner, row.id)]
        manifest["recheck_call_budget"] = body.recheck_call_budget
        if body.role != "reconcile":
            manifest["model_call_limit"] += body.recheck_call_budget
        elif not body.recheck_call_budget:
            raise BusinessError(422, "RECHECK_BUDGET_REQUIRED", "请明确本轮重审调用额度。")
    run = create_run(
        db,
        owner,
        ROLE_TYPES[body.role],
        "agent-study-2.3" if body.role == "tutor" else VERSION,
        manifest,
        call_budget=manifest["model_call_limit"],
    )
    row.payload = {
        **row.payload,
        "active_run_id": str(run.id),
        **(
            {"recheck_pending": manifest["recheck_plan_ids"]}
            if manifest.get("recheck_plan_ids")
            else {}
        ),
    }
    row.revision += 1
    response = {
        "run_id": str(run.id),
        "status": run.status,
        "role": body.role,
        "model_call_limit": manifest["model_call_limit"],
        "max_output_tokens": manifest["max_output_tokens"],
        "track_revision": row.revision,
    }
    record.response_summary = response
    return response


def validate_run(db: Session, run: AgentRun) -> StudyTrack:
    if run.workflow_version in {"agent-delegation-1.0", "agent-delegation-task-1.0"}:
        from careerforge.application.autonomous_service import validate_run as validate_delegation

        return validate_delegation(db, run)
    row = track(db, run.owner_id, UUID(run.input_manifest["track_id"]))
    manifest = run.input_manifest
    validate_requirements(db, run.owner_id, manifest["sources"])
    if (
        manifest.get("requirements_id")
        and manifest["role"] != "tutor"
        and row.payload.get("requirements_id") != manifest["requirements_id"]
    ):
        raise BusinessError(409, "STUDY_REQUIREMENTS_CHANGED", "需求版本已变化。")
    if manifest.get("focus_chunk"):
        chunk = knowledge.chunk_detail(db, run.owner_id, UUID(manifest["focus_chunk"]["chunk_id"]))
        if chunk["content_hash"] != manifest["focus_chunk"]["content_hash"]:
            raise BusinessError(409, "FOCUS_CHANGED", "所选实现索引已变化。")
    if manifest.get("candidate", {}).get("repository_snapshot_id"):
        repositories.snapshot(
            db, run.owner_id, UUID(manifest["candidate"]["repository_snapshot_id"])
        )
    if manifest["role"] in {"plan", "tutor"}:
        if manifest["role"] == "tutor":
            validate_plan_scope(
                db, row, artifact_row(db, run.owner_id, row.id, manifest["plan_id"], "plan")
            )
        valid = (
            {str(i.id) for i in approved(db, row)}
            if manifest["role"] == "plan"
            else set(manifest["review_ids"])
        )
        identifiers = (
            [p["id"] for p in manifest["projects"]]
            if manifest["role"] == "plan"
            else manifest["review_ids"]
        )
        if not set(identifiers) <= valid:
            raise BusinessError(409, "STUDY_PROJECT_UNAVAILABLE", "审核项目已不可用。")
    if manifest["role"] == "review_batch":
        from careerforge.application.review_batch_service import validate_parent_scope

        validate_parent_scope(db, run)
    if run.parent_run_id:
        from careerforge.application.review_batch_service import validate_child

        validate_child(db, run)
    if manifest.get("context_scope"):
        from careerforge.application.context_service import validate_catalog

        validate_catalog(db, run.owner_id, manifest)
    return row


def progress(db: Session, owner: UUID, identifier: UUID, body: ModuleProgress) -> dict[str, Any]:
    row = track(db, owner, identifier, True)
    check_revision(row.revision, body.expected_revision)
    plan = artifact_row(db, owner, row.id, body.plan_id, "plan")
    validate_plan_scope(db, row, plan)
    module = next((m for m in plan.payload["modules"] if m["key"] == body.module_key), None)
    if not module:
        raise BusinessError(422, "MODULE_NOT_FOUND", "模块不存在。")
    values = dict(row.payload.get("progress", {}))
    key = f"{plan.id}:{body.module_key}"
    if body.state == "completed":
        if any(
            values.get(f"{plan.id}:{k}", {}).get("state") != "completed"
            for k in module["prerequisite_keys"]
        ):
            raise BusinessError(409, "PREREQUISITE_PENDING", "请先完成此模块的前置模块。")
    values[key] = {
        "state": body.state,
        "note": body.note,
        "reported_by": "user",
        "competence_verified": False,
    }
    row.payload = {**row.payload, "progress": values}
    row.revision += 1
    return detail(db, row)


def recover_extraction(
    db: Session, owner: UUID, identifier: UUID, body: ExtractionRecovery, key: str | None
) -> dict[str, Any]:
    """Recover only a completed, within-budget saved response; leave the failed run untouched."""
    from careerforge.application.study_validation import partial_extraction
    from careerforge.infrastructure.db.models import ModelCallReceipt, StageArtifact
    from careerforge.schemas.model_io import ModelResult
    from careerforge.schemas.study import ExtractOutput

    row = track(db, owner, identifier, True)
    record = idempotency(
        db, owner, f"study.recover-extract:{identifier}", key, body.model_dump(mode="json")
    )
    if record.response_summary:
        return detail(db, row)
    check_revision(row.revision, body.expected_revision)
    idle(db, row)
    run = owned(db, AgentRun, owner, body.run_id)
    if (
        run.status != "failed"
        or run.workflow_version != VERSION
        or run.input_manifest.get("role") != "extract"
        or run.input_manifest.get("track_id") != str(identifier)
        or row.payload.get("active_run_id") != str(run.id)
        or row.payload.get("requirements_id")
    ):
        raise BusinessError(
            409, "EXTRACTION_RECOVERY_UNAVAILABLE", "只能恢复此方向最新未发布的提取结果。"
        )
    validate_run(db, run)
    saved = db.scalar(
        select(StageArtifact).where(
            StageArtifact.run_id == run.id,
            StageArtifact.stage_key == "extract",
            StageArtifact.stage_version == VERSION,
        )
    )
    receipts = list(db.scalars(select(ModelCallReceipt).where(ModelCallReceipt.run_id == run.id)))
    if not saved or len(receipts) != 1 or receipts[0].status != "completed":
        raise BusinessError(409, "EXTRACTION_RESULT_UNCERTAIN", "没有完整且已计量的模型结果。")
    result = ModelResult.model_validate(saved.payload)
    output = result.usage.output_tokens
    requested = run.input_manifest["max_output_tokens"]
    if (
        result.finish_reason != "stop"
        or output is None
        or output > requested
        or receipts[0].usage != result.usage.model_dump()
    ):
        raise BusinessError(
            409, "EXTRACTION_BUDGET_UNVERIFIED", "未知或超限结果只能预览，不能恢复发布。"
        )
    values = partial_extraction(
        ExtractOutput.model_validate(result.structured), run.input_manifest["sources"]
    )
    item = put(
        db,
        row,
        "requirements",
        {
            **values,
            "synthetic": result.synthetic,
            "sources": [
                {k: v for k, v in s.items() if k != "content"}
                for s in run.input_manifest["sources"]
            ],
            "recovery": {
                "method": "validated_partial_extraction",
                "original_run_id": str(run.id),
                "original_status": "failed",
                "extra_model_calls": 0,
            },
        },
        run.id,
    )
    row.payload = {**row.payload, "requirements_id": str(item.id), "active_plan_id": None}
    row.revision += 1
    from careerforge.application.run_service import event

    event(
        db,
        run,
        "study.extraction_recovered",
        {
            "artifact_id": str(item.id),
            "validated_count": len(values["requirements"]),
            "unresolved_count": len(values["unresolved"]),
            "extra_model_calls": 0,
        },
    )
    record.response_summary = {"study_artifact_id": str(item.id)}
    return detail(db, row)


def publish_review(
    db: Session,
    run: AgentRun,
    row: StudyTrack,
    manifest: dict[str, Any],
    material: dict[str, Any],
    output: ReviewOutput,
    reading: dict[str, Any],
    matches: list[dict[str, Any]],
    synthetic: bool,
    recovery: dict[str, Any] | None = None,
    evidence_validation: dict[str, Any] | None = None,
) -> StudyArtifact:
    """Publish only literal validated source matches, using the same gates for saved recovery."""
    from careerforge.schemas.knowledge import KnowledgeImport

    candidate = material["candidate"]
    license_known = candidate.get("license") not in {
        None,
        "",
        "unknown",
        "NOASSERTION",
        "OTHER",
    }
    admitted = output.verdict == "strong" and bool(matches) and license_known
    if material["origin"] != "user_import":
        admitted = (
            admitted and candidate.get("archived") is False and candidate.get("fork") is False
        )
    payload = {
        **output.model_dump(),
        "requirements_id": manifest["requirements_id"],
        "candidate_id": manifest["candidate_id"],
        "candidate": candidate,
        "admitted": admitted,
        "highlights": [],
        "coverage": reading["coverage"],
        "investigation": reading.get("investigation"),
        "counterevidence": (evidence_validation or {}).get("counterevidence", []),
        "repository_snapshot_id": None,
    }
    if not license_known:
        payload["limitations"].append("许可证未识别，未自动纳入学习项目库。")
    if material["origin"] == "user_import":
        payload["limitations"].append("人工提交副本：远端版本、活跃度与 fork 状态未独立验证。")
    if output.verdict == "strong" and not admitted:
        payload["admission_reason"] = "源码相关性较强，但自动入库质量门槛未满足。"
    if recovery:
        payload["recovery"] = recovery
    if evidence_validation:
        payload["evidence_validation"] = evidence_validation
    if admitted:
        if candidate.get("repository_snapshot_id"):
            snapshot_id = UUID(candidate["repository_snapshot_id"])
        else:
            imported = knowledge.import_project(
                db,
                run.owner_id,
                KnowledgeImport(
                    github_url=candidate["github_url"],
                    requested_ref=candidate["commit"],
                    name=candidate["full_name"],
                    description=candidate.get("description", ""),
                    tags=output.technology_stack[:20],
                    files=material["files"],
                ),
                f"{run.id}:{manifest['candidate_id']}",
                trusted_archive=True,
                archive_skipped=material["skipped"],
            )
            snapshot_id = UUID(imported["repository_snapshot_id"])
        payload["repository_snapshot_id"] = str(snapshot_id)
        for match in matches:
            citation = repositories.code_citation(
                db,
                run.owner_id,
                snapshot_id,
                match["path"],
                match["start_line"],
                match["end_line"],
            )
            payload["highlights"].append(
                {**match, "citation_id": str(citation.id), "commit": candidate["commit"]}
            )
    from careerforge.application.review_evidence import claims_for_review

    payload.update(
        claims_for_review(
            payload["highlights"] or matches,
            (evidence_validation or {}).get("counterevidence", []),
            candidate["commit"],
        )
    )
    item = put(db, row, "review", {**payload, "synthetic": synthetic}, run.id)
    row.revision += 1
    return item
