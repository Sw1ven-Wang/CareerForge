import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.models import IdempotencyRecord


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fingerprint(payload: dict[str, Any]) -> str:
    return digest(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def idempotency(
    db: Session, owner: UUID, operation: str, key: str | None, payload: dict[str, Any]
) -> IdempotencyRecord:
    if not key or len(key) > 128:
        raise BusinessError(400, "IDEMPOTENCY_KEY_REQUIRED", "请提供有效 Idempotency-Key。")
    hashed = fingerprint(payload)
    db.execute(
        insert(IdempotencyRecord)
        .values(owner_id=owner, operation=operation, key=key, request_hash=hashed)
        .on_conflict_do_nothing()
    )
    row = db.scalars(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.owner_id == owner,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.key == key,
        )
        .with_for_update()
    ).one()
    if row.request_hash != hashed:
        raise BusinessError(409, "IDEMPOTENCY_CONFLICT", "同一幂等键不能使用不同内容。")
    if row.response_summary and row.response_summary.get("_revoked"):
        raise BusinessError(404, "NOT_FOUND", "原请求引用的材料已撤销。")
    if row.response_summary and not operation.endswith(".delete"):
        replay_guard(db, owner, row.response_summary)
    return row


def replay_guard(db: Session, owner: UUID, summary: dict[str, Any]) -> None:
    """Reauthorize persisted response identities before any service can replay private text."""
    from careerforge.application import (
        build_service,
        learning_service,
        planning_service,
        repository_service,
    )

    if summary.get("contract_artifact_id"):
        from careerforge.application.job_contract_service import read
        read(db, owner, UUID(summary["contract_artifact_id"]))
    if summary.get("evidence_version_id"):
        planning_service.evidence_version(db, owner, UUID(summary["evidence_version_id"]))
    if summary.get("current_task_version_id"):
        build_service.task_version(db, owner, UUID(summary["current_task_version_id"]))
    if summary.get("submission_id"):
        build_service.submission(db, owner, UUID(summary["submission_id"]))
    if summary.get("assessment_id"):
        build_service.assessment(db, owner, UUID(summary["assessment_id"]))
    if summary.get("learning_unit_id"):
        learning_service.unit(db, owner, UUID(summary["learning_unit_id"]))
    if summary.get("recommendation_id"):
        repository_service.recommendation(db, owner, UUID(summary["recommendation_id"]))
    if summary.get("run_id"):
        from careerforge.application.maintenance_service import authorized
        from careerforge.application.run_service import owned_run

        authorized(db, owned_run(db, owner, UUID(summary["run_id"])))


def check_revision(actual: int, expected: int) -> None:
    if actual != expected:
        raise BusinessError(
            409, "VERSION_CONFLICT", "对象已有更新，请重新读取。", current_version=actual
        )
