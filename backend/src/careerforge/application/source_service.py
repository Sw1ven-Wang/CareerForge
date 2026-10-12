from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from careerforge.application.attachment_service import attachment_text, available, bind
from careerforge.application.common import check_revision, digest, fingerprint, idempotency
from careerforge.application.import_service import materialize_job
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError, not_found
from careerforge.infrastructure.db.models import Citation, Deletion, Source, SourceSnapshot
from careerforge.schemas.resources import CitationInput, SnapshotInput, SourceImport


def get_source(db: Session, owner: UUID, identifier: UUID, *, lock: bool = False) -> Source:
    query = select(Source).where(
        Source.id == identifier, Source.owner_id == owner, Source.revoked_at.is_(None)
    )
    if lock:
        query = query.with_for_update()
    result = db.scalars(query).first()
    if result is None:
        raise not_found()
    return result


def get_snapshot(db: Session, owner: UUID, source_id: UUID, snapshot_id: UUID) -> SourceSnapshot:
    get_source(db, owner, source_id)
    result = db.scalars(
        select(SourceSnapshot).where(
            SourceSnapshot.id == snapshot_id,
            SourceSnapshot.source_id == source_id,
            SourceSnapshot.owner_id == owner,
        )
    ).first()
    if result is None:
        raise not_found()
    if result.attachment_id is not None:
        available(db, owner, result.attachment_id)
    return result


def normalize(content: str) -> str:
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if len(content) > get_settings().max_source_chars:
        raise BusinessError(413, "SOURCE_TOO_LARGE", "正文超过限制，请拆分后导入。")
    return content


def latest(db: Session, source: Source) -> SourceSnapshot | None:
    return db.scalars(
        select(SourceSnapshot)
        .where(SourceSnapshot.source_id == source.id, SourceSnapshot.owner_id == source.owner_id)
        .order_by(SourceSnapshot.version_no.desc())
    ).first()


def snapshot_result(
    source: Source, snapshot: SourceSnapshot | None, duplicate: UUID | None = None
) -> dict[str, Any]:
    return {
        "source_id": str(source.id),
        "source_snapshot_id": str(snapshot.id) if snapshot else None,
        "content_hash": snapshot.content_hash if snapshot else None,
        "read_status": "available" if snapshot else "unread",
        "version": source.revision,
        "duplicate_of_snapshot_id": str(duplicate) if duplicate else None,
    }


def import_source(db: Session, owner: UUID, value: SourceImport, key: str | None) -> dict[str, Any]:
    record = idempotency(db, owner, "sources.import", key, value.model_dump(mode="json"))
    if record.response_summary is not None:
        source_id = UUID(record.response_summary["source_id"])
        get_source(db, owner, source_id)
        if record.response_summary.get("source_snapshot_id"):
            get_snapshot(db, owner, source_id, UUID(record.response_summary["source_snapshot_id"]))
        return record.response_summary
    raw = attachment_text(db, owner, value.attachment_id) if value.attachment_id else value.content
    content = normalize(raw) if raw is not None else None
    metadata = value.model_dump(mode="json", exclude={"content", "import_mode", "source_url"})
    identity_hash = digest(
        value.source_url or (str(uuid4()) if value.source_type == "job_posting" else content or "")
    )
    # Serialize natural source identity across different idempotency keys.
    lock_key = int(digest(str(owner) + identity_hash)[:15], 16)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    source = db.scalars(
        select(Source)
        .where(
            Source.owner_id == owner,
            Source.identity_hash == identity_hash,
            Source.revoked_at.is_(None),
        )
        .with_for_update()
    ).first()
    existing = latest(db, source) if source is not None else None
    if source is None:
        source = Source(
            owner_id=owner,
            identity_hash=identity_hash,
            source_url=value.source_url,
            import_mode=value.import_mode,
            source_type=value.source_type,
            title=value.title,
        )
        db.add(source)
        db.flush()
    if value.attachment_id is not None:
        bind(db, owner, value.attachment_id, source.id)
    if content is None:
        result = snapshot_result(source, None)
    elif (
        existing
        and existing.content_hash == digest(content)
        and existing.metadata_hash == fingerprint(metadata)
    ):
        result = snapshot_result(source, existing, existing.id)
    else:
        if existing:
            source.revision += 1
        source.source_type = value.source_type
        source.title = value.title
        snapshot = SourceSnapshot(
            owner_id=owner,
            source_id=source.id,
            version_no=(existing.version_no + 1) if existing else 1,
            content=content,
            attachment_id=value.attachment_id,
            content_hash=digest(content),
            metadata_hash=fingerprint(metadata),
            metadata_values=metadata,
        )
        db.add(snapshot)
        db.flush()
        materialize_job(db, snapshot)
        if get_settings().job_contract_write and value.source_type == "job_posting":
            from careerforge.application.job_contract_service import from_legacy

            from_legacy(db, owner, source, snapshot, raw=raw)
        result = snapshot_result(source, snapshot)
    record.response_summary = result
    return result


def new_snapshot(
    db: Session, owner: UUID, source_id: UUID, value: SnapshotInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"sources.{source_id}.snapshots", key, value.model_dump(mode="json")
    )
    source = get_source(db, owner, source_id, lock=True)
    if record.response_summary is not None:
        get_snapshot(db, owner, source_id, UUID(record.response_summary["source_snapshot_id"]))
        return record.response_summary
    check_revision(source.revision, value.expected_version)
    previous = latest(db, source)
    base = (
        get_snapshot(db, owner, source_id, value.base_snapshot_id)
        if value.base_snapshot_id
        else previous
    )
    content = normalize(
        attachment_text(db, owner, value.attachment_id)
        if value.attachment_id
        else value.content
        if value.content is not None
        else base.content
        if base
        else ""
    )
    metadata = (
        dict(base.metadata_values)
        if base
        else {
            "source_type": source.source_type,
            "title": source.title,
            "published_at": None,
            "published_at_precision": "unknown",
            "declared_provenance": None,
        }
    )
    metadata.update(value.metadata_overrides.model_dump(mode="json", exclude_unset=True))
    if metadata.get("source_type") is None:
        raise BusinessError(422, "INVALID_METADATA", "材料类型不能为空。")
    if metadata.get("published_at") is None and metadata.get("published_at_precision") != "unknown":
        raise BusinessError(422, "INVALID_METADATA", "缺失日期的精度应为 unknown。")
    attachment_id = value.attachment_id or (
        base.attachment_id if value.base_snapshot_id and base else None
    )
    if attachment_id:
        bind(db, owner, attachment_id, source_id)
    snapshot = SourceSnapshot(
        attachment_id=attachment_id,
        owner_id=owner,
        source_id=source_id,
        version_no=previous.version_no + 1 if previous else 1,
        content=content,
        content_hash=digest(content),
        metadata_hash=fingerprint(metadata),
        metadata_values=metadata,
        change_reason=value.change_reason,
    )
    db.add(snapshot)
    source.revision += 1
    source.source_type = str(metadata["source_type"])
    source.title = metadata.get("title")
    db.flush()
    materialize_job(db, snapshot)
    if get_settings().job_contract_write and metadata.get("source_type") == "job_posting":
        from careerforge.application.job_contract_service import from_legacy

        supplied = (
            attachment_text(db, owner, value.attachment_id)
            if value.attachment_id
            else value.content
        )
        from_legacy(db, owner, source, snapshot, raw=supplied)
    result = snapshot_result(source, snapshot)
    record.response_summary = result
    return result


def delete_source(
    db: Session, owner: UUID, source_id: UUID, version: int, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"sources.{source_id}.delete", key, {"expected_version": version}
    )
    if record.response_summary is not None:
        job = db.scalars(
            select(Deletion).where(
                Deletion.id == UUID(record.response_summary["deletion_id"]),
                Deletion.owner_id == owner,
            )
        ).one()
        return {**record.response_summary, "status": job.status}
    source = get_source(db, owner, source_id, lock=True)
    check_revision(source.revision, version)
    source.revoked_at = db.scalar(select(func.now()))
    source.revision += 1
    job = Deletion(owner_id=owner, source_id=source_id, status="revoked")
    db.add(job)
    db.flush()
    result = {"deletion_id": str(job.id), "status": job.status, "version": source.revision}
    record.response_summary = result
    return result


def create_citation(
    db: Session, owner: UUID, value: CitationInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(db, owner, "citations.create", key, value.model_dump(mode="json"))
    snapshot = db.scalars(
        select(SourceSnapshot).where(
            SourceSnapshot.id == value.source_snapshot_id, SourceSnapshot.owner_id == owner
        )
    ).first()
    if snapshot is None:
        raise not_found()
    get_snapshot(db, owner, snapshot.source_id, snapshot.id)
    if record.response_summary is not None:
        return record.response_summary
    if not 0 <= value.char_start < value.char_end <= len(snapshot.content):
        raise BusinessError(422, "INVALID_CITATION", "引用范围不合法。")
    quote = snapshot.content[value.char_start : value.char_end]
    if digest(quote) != value.text_hash:
        raise BusinessError(422, "INVALID_CITATION", "引用哈希与原文不一致。")
    citation = Citation(
        owner_id=owner,
        source_id=snapshot.source_id,
        snapshot_id=snapshot.id,
        char_start=value.char_start,
        char_end=value.char_end,
        text_hash=value.text_hash,
    )
    db.add(citation)
    db.flush()
    result = {"citation_id": str(citation.id)}
    record.response_summary = result
    return result
