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
    return row


def check_revision(actual: int, expected: int) -> None:
    if actual != expected:
        raise BusinessError(
            409, "VERSION_CONFLICT", "对象已有更新，请重新读取。", current_version=actual
        )
