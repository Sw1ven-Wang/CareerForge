from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.domain.errors import not_found


def owned[T](
    db: Session, model: type[T], owner: UUID, identifier: UUID, *, lock: bool = False
) -> T:
    entity = cast(Any, model)
    query = select(model).where(entity.id == identifier, entity.owner_id == owner)
    if hasattr(model, "revoked_at"):
        query = query.where(entity.revoked_at.is_(None))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None or (hasattr(row, "payload") and row.payload.get("_revoked")):
        raise not_found()
    return row
