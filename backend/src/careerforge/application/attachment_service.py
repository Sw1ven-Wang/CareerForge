import ast
import hashlib
import json
from datetime import timedelta
from pathlib import PureWindowsPath
from typing import Any
from uuid import UUID

from defusedxml import ElementTree  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application.common import check_revision, idempotency
from careerforge.application.run_service import db_now
from careerforge.domain.errors import BusinessError, not_found
from careerforge.infrastructure.db.models import Attachment, Deletion, Source, SourceSnapshot

MAX_BYTES = 1024 * 1024
MIMES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".log": "text/plain",
    ".json": "application/json",
    ".py": "text/x-python",
    ".xml": "application/xml",
}


def validate_file(name: str, body: bytes) -> tuple[str, str]:
    name = PureWindowsPath(name).name
    suffix = PureWindowsPath(name).suffix.lower()
    if suffix not in MIMES or not name or len(name) > 200:
        raise BusinessError(
            422,
            "ATTACHMENT_TYPE_REJECTED",
            "仅支持文本、Markdown、JSON、日志、Python 与 JUnit XML。",
        )
    if len(body) > MAX_BYTES:
        raise BusinessError(413, "ATTACHMENT_TOO_LARGE", "附件不能超过 1 MiB。")
    try:
        text = body.decode("utf-8-sig")
        if not text.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            raise ValueError("Binary or empty text")
        if suffix == ".json":

            def reject_constant(value: str) -> None:
                raise ValueError(value)

            json.loads(text, parse_constant=reject_constant)
        if suffix == ".py":
            ast.parse(text)
        if suffix == ".xml":
            root = ElementTree.fromstring(
                text, forbid_dtd=True, forbid_entities=True, forbid_external=True
            )
            if root.tag not in {"testsuite", "testsuites"}:
                raise ValueError("Only JUnit XML")
    except Exception as error:
        raise BusinessError(
            422, "ATTACHMENT_CONTENT_REJECTED", "附件不是支持的有效 UTF-8 文本格式。"
        ) from error
    return name, MIMES[suffix]


def available(db: Session, owner: UUID, attachment_id: UUID, *, lock: bool = False) -> Attachment:
    stmt = select(Attachment).where(
        Attachment.id == attachment_id,
        Attachment.owner_id == owner,
        Attachment.revoked_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    row = db.scalar(stmt)
    if row is None or row.body is None:
        raise not_found()
    if row.evidence_id is not None:
        from careerforge.infrastructure.db.models import PersonalEvidence

        root_evidence = db.get(PersonalEvidence, row.evidence_id)
        if (
            root_evidence is None
            or root_evidence.owner_id != owner
            or root_evidence.revoked_at is not None
        ):
            raise not_found()
    elif row.submission_id is not None:
        pass  # Immutable submission binding remains available until explicit revocation.
    elif row.source_id is None:
        if row.expires_at <= db_now(db):
            raise not_found()
    else:
        root = db.get(Source, row.source_id)
        if root is None or root.owner_id != owner or root.revoked_at is not None:
            raise not_found()
    return row


def attachment_text(db: Session, owner: UUID, identifier: UUID) -> str:
    return (available(db, owner, identifier).body or b"").decode("utf-8-sig")


def bind(db: Session, owner: UUID, identifier: UUID, source_id: UUID) -> None:
    row = available(db, owner, identifier, lock=True)
    if row.purpose != "source":
        raise BusinessError(422, "ATTACHMENT_PURPOSE_CONFLICT", "该附件用途不能绑定岗位来源。")
    if row.source_id is not None and row.source_id != source_id:
        raise BusinessError(409, "ATTACHMENT_ROOT_CONFLICT", "附件已归属于另一个来源，请重新上传。")
    row.source_id = source_id


def metadata(row: Attachment) -> dict[str, Any]:
    return {
        "attachment_id": str(row.id),
        "name": row.name,
        "mime": row.mime,
        "size": row.size,
        "content_hash": row.content_hash,
        "read_status": "available",
        "version": row.revision,
        "purpose": row.purpose,
    }


def upload(
    db: Session, owner: UUID, filename: str, body: bytes, purpose: str, key: str | None
) -> dict[str, Any]:
    name, mime = validate_file(filename, body)
    hashed = hashlib.sha256(body).hexdigest()
    record = idempotency(
        db,
        owner,
        "attachments.upload",
        key,
        {"name": name, "content_hash": hashed, "purpose": purpose},
    )
    if record.response_summary is not None:
        return metadata(available(db, owner, UUID(record.response_summary["attachment_id"])))
    row = Attachment(
        owner_id=owner,
        name=name,
        mime=mime,
        purpose=purpose,
        body=body,
        size=len(body),
        content_hash=hashed,
        expires_at=db_now(db) + timedelta(hours=24),
    )
    db.add(row)
    db.flush()
    record.response_summary = {"attachment_id": str(row.id)}
    return metadata(row)


def content(db: Session, owner: UUID, identifier: UUID, snapshot_id: UUID | None) -> Attachment:
    row = available(db, owner, identifier)
    if snapshot_id is not None:
        snapshot = db.get(SourceSnapshot, snapshot_id)
        if (
            snapshot is None
            or snapshot.owner_id != owner
            or snapshot.source_id != row.source_id
            or snapshot.attachment_id != row.id
        ):
            raise not_found()
    return row


def delete(
    db: Session, owner: UUID, identifier: UUID, version: int, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"attachments.{identifier}.delete", key, {"expected_version": version}
    )
    if record.response_summary is not None:
        return record.response_summary
    row = available(db, owner, identifier, lock=True)
    check_revision(row.revision, version)
    row.revoked_at = db_now(db)
    row.revision += 1
    job = Deletion(owner_id=owner, attachment_id=row.id, status="revoked")
    db.add(job)
    db.flush()
    record.response_summary = {
        "deletion_id": str(job.id),
        "status": "revoked",
        "version": row.revision,
    }
    return record.response_summary
