"""Freeze selected repositories and dispatch bounded independent review contexts."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application import delegation_service as delegation
from careerforge.application import repository_service as repositories
from careerforge.application import study_service as study
from careerforge.application.common import check_revision, fingerprint, idempotency
from careerforge.application.run_service import create_run, db_now, owned_run
from careerforge.application.studio_learning_service import read_requirements
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.models import AgentRun
from careerforge.infrastructure.db.study_models import StudyArtifact
from careerforge.schemas.subtasks import ReviewBatchInput, ReviewTaskContract

READ_TOOLS = [
    "list_tree",
    "find_symbol",
    "search_text",
    "read_span",
    "references_of",
    "find_tests",
    "finish_investigation",
]


def frozen_candidate(db: Session, owner: UUID, track_id: UUID, identifier: UUID) -> Json:
    item = study.artifact_row(db, owner, track_id, identifier, "candidate")
    candidate = dict(item.payload)
    snapshot_id = candidate.get("repository_snapshot_id")
    if not snapshot_id:
        reviews = db.scalars(
            select(StudyArtifact)
            .where(
                StudyArtifact.track_id == track_id,
                StudyArtifact.kind == "review",
                StudyArtifact.payload["candidate_id"].astext == str(identifier),
            )
            .order_by(StudyArtifact.created_at.desc())
        ).all()
        previous = next((r for r in reviews if r.payload.get("repository_snapshot_id")), None)
        if previous:
            candidate = {
                **previous.payload["candidate"],
                "repository_snapshot_id": previous.payload["repository_snapshot_id"],
            }
            snapshot_id = candidate["repository_snapshot_id"]
    if not snapshot_id:
        raise BusinessError(
            422, "FIXED_SNAPSHOT_REQUIRED", "批次只审核已导入或已审核留存的固定版本，请先导入项目。"
        )
    snapshot = repositories.snapshot(db, owner, UUID(snapshot_id))
    candidate["commit"] = snapshot.commit_sha
    return {
        "candidate_id": str(identifier),
        "candidate": candidate,
        "source_hash": fingerprint(item.payload),
        "snapshot_hash": snapshot.manifest_hash,
    }


def start(
    db: Session, owner: UUID, track_id: UUID, body: ReviewBatchInput, key: str | None
) -> Json:
    record = idempotency(
        db, owner, f"study.review_batch:{track_id}", key, body.model_dump(mode="json")
    )
    if record.response_summary:
        run = owned_run(db, owner, UUID(record.response_summary["run_id"]))
        study.validate_run(db, run)
        return record.response_summary
    track = study.track(db, owner, track_id, True)
    check_revision(track.revision, body.expected_revision)
    study.idle(db, track)
    settings = get_settings()
    if not settings.real_agent_enabled or not settings.openai_api_key.get_secret_value():
        raise BusinessError(409, "REAL_AGENT_DISABLED", "真实 Agent 未配置。")
    requirement = study.current_requirements(db, track)
    candidates = [frozen_candidate(db, owner, track_id, i) for i in body.candidate_ids]
    if any(c["candidate"]["requirements_id"] != str(requirement.id) for c in candidates):
        raise BusinessError(409, "STUDY_REQUIREMENTS_CHANGED", "候选不属于当前需求版本。")
    manifest = {
        "track_id": str(track.id),
        "role": "review_batch",
        "requirements_id": str(requirement.id),
        "requirements": requirement.payload["requirements"],
        "sources": read_requirements(db, owner, track.payload["source_snapshot_ids"]),
        "candidates": candidates,
        "model": settings.model_name,
        "base_url": settings.openai_base_url,
        "max_output_tokens": min(body.max_output_tokens, settings.study_max_output_tokens),
        "model_call_limit": body.call_budget,
        "per_child_call_limit": body.per_child_call_limit,
        "max_parallel_children": 2,
        "automatic_retry": False,
    }
    run = create_run(
        db,
        owner,
        "project_research",
        delegation.BATCH_VERSION,
        manifest,
        call_budget=body.call_budget,
        deadline_at=db_now(db) + timedelta(minutes=15),
    )
    track.payload = {**track.payload, "active_run_id": str(run.id)}
    track.revision += 1
    result = {
        "run_id": str(run.id),
        "status": run.status,
        "role": "review_batch",
        "model_call_limit": run.call_budget,
        "max_output_tokens": manifest["max_output_tokens"],
        "child_count": len(candidates),
        "track_revision": track.revision,
    }
    record.response_summary = result
    return result


def validate_parent_scope(db: Session, run: AgentRun) -> None:
    manifest = run.input_manifest
    if run.deadline_at and run.deadline_at <= db_now(db):
        raise BusinessError(409, "TASK_DEADLINE_EXCEEDED", "审核批次已到截止时间。")
    for frozen in manifest["candidates"]:
        item = study.artifact_row(
            db, run.owner_id, UUID(manifest["track_id"]), frozen["candidate_id"], "candidate"
        )
        if fingerprint(item.payload) != frozen["source_hash"]:
            raise BusinessError(409, "CHILD_SCOPE_CHANGED", "候选版本已变化。")
        candidate = frozen["candidate"]
        snap = repositories.snapshot(db, run.owner_id, UUID(candidate["repository_snapshot_id"]))
        if snap.manifest_hash != frozen["snapshot_hash"] or snap.commit_sha != candidate["commit"]:
            raise BusinessError(409, "CHILD_SCOPE_CHANGED", "固定源码版本已变化。")


def child_manifest(parent: AgentRun, frozen: Json) -> Json:
    source = parent.input_manifest
    if not parent.deadline_at:
        raise ValueError("Delegation requires a deadline")
    contract = ReviewTaskContract(
        parent_run_id=parent.id,
        requirements_id=source["requirements_id"],
        candidate_id=frozen["candidate_id"],
        candidate_hash=fingerprint(frozen["candidate"]),
        requirement_keys=[r["key"] for r in source["requirements"]],
        source_snapshot_ids=[s["source_snapshot_id"] for s in source["sources"]],
        context_refs=[source["requirements_id"], frozen["candidate_id"]],
        allowed_tools=READ_TOOLS,
        call_limit=source["per_child_call_limit"],
        max_output_tokens=source["max_output_tokens"],
        deadline=parent.deadline_at.isoformat(),
        stop_conditions=[
            "parent_cancelled",
            "source_changed",
            "deadline",
            "budget_exhausted",
            "no_new_evidence",
        ],
    ).model_dump(mode="json")
    return {
        k: source[k]
        for k in (
            "track_id",
            "requirements_id",
            "requirements",
            "sources",
            "model",
            "base_url",
            "max_output_tokens",
        )
    } | {
        "role": "review",
        "candidate_id": frozen["candidate_id"],
        "candidate": frozen["candidate"],
        "review_call_limit": source["per_child_call_limit"],
        "model_call_limit": source["per_child_call_limit"],
        "delegated_review": contract,
    }


def validate_child(db: Session, run: AgentRun) -> None:
    parent = delegation.parent_guard(db, run)
    study.validate_run(db, parent)
    frozen = next(
        (
            c
            for c in parent.input_manifest["candidates"]
            if c["candidate_id"] == run.input_manifest["candidate_id"]
        ),
        None,
    )
    if not frozen or fingerprint(child_manifest(parent, frozen)) != run.input_manifest_hash:
        raise BusinessError(409, "CHILD_SCOPE_CHANGED", "子任务不能改变父任务范围、模型或预算。")
    ReviewTaskContract.model_validate(run.input_manifest["delegated_review"])
