"""Preserve old personal artifacts without reusing old approval, spending or session authority."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..adapters.github import GitHubProvider
from ..contracts import ImportSourceInput
from ..contracts_compat import LegacyImportInput, LegacySourceInput
from ..errors import DomainError
from ..models_compat import LegacyImportBatch, LegacyImportRecord
from ..util import digest, fingerprint, validate_files
from .privacy import assert_personal_access
from .security import namespace_access

if TYPE_CHECKING:
    from ..application import Application


class LegacyImportService:
    def __init__(self, app: Application):
        self.app, self.db = app, app.db

    @staticmethod
    def _owned(db: Session, actor: str, identifier: str) -> LegacyImportBatch:
        row = db.get(LegacyImportBatch, identifier)
        if row is None or row.principal_id != actor:
            raise DomainError("NOT_FOUND", "迁移归档不存在。", 404)
        namespace_access(db, actor, row.namespace_id)
        if row.status != "unreviewed_legacy":
            raise DomainError("LEGACY_ARCHIVE_UNAVAILABLE", "历史归档已删除或不可使用。", 409)
        return row

    @staticmethod
    def _summary(row: LegacyImportBatch, count: int) -> dict[str, Any]:
        return {
            "batch_id": row.id,
            "namespace_id": row.namespace_id,
            "baseline_sha": row.baseline_sha,
            "bundle_hash": row.bundle_hash,
            "status": row.status,
            "record_count": count,
            "published_knowledge_created": 0,
            "billing_authority_imported": False,
            "old_sessions_imported": False,
        }

    def import_bundle(self, actor: str, value: LegacyImportInput) -> dict[str, Any]:
        # Callers of the service, not only HTTP/Pydantic parsing, get full validation.
        value = LegacyImportInput.model_validate(value.model_dump(mode="json"))
        try:
            with self.db.transaction() as db:
                assert_personal_access(db, actor)
                ns = namespace_access(db, actor, value.namespace_id, True)
                if ns.kind != "private" or ns.owner_id != actor:
                    raise DomainError("LEGACY_PRIVATE_ONLY", "历史资料只能导入本人私有归档。", 403)
                old = db.scalar(
                    select(LegacyImportBatch).where(
                        LegacyImportBatch.principal_id == actor,
                        LegacyImportBatch.bundle_hash == value.bundle.bundle_hash,
                    )
                )
                if old is not None:
                    self._owned(db, actor, old.id)
                    if old.namespace_id != ns.id:
                        raise DomainError(
                            "LEGACY_REPLAY_CHANGED", "相同归档不能静默改换目标知识域。", 409
                        )
                    return self._summary(old, len(value.bundle.records))
                batch = LegacyImportBatch(
                    principal_id=actor,
                    namespace_id=ns.id,
                    baseline_sha=value.bundle.baseline_sha,
                    legacy_owner_id=value.bundle.owner_id,
                    bundle_hash=value.bundle.bundle_hash,
                )
                db.add(batch)
                db.flush()
                for record in value.bundle.records:
                    db.add(
                        LegacyImportRecord(
                            batch_id=batch.id,
                            table_name=record.table_name,
                            legacy_id=record.legacy_id,
                            payload=record.data,
                            content_hash=record.content_hash,
                        )
                    )
                db.flush()
                return self._summary(batch, len(value.bundle.records))
        except IntegrityError:
            # Unique owner+manifest fences concurrent repeats; never retry a different error.
            with self.db.read() as db:
                old = db.scalar(
                    select(LegacyImportBatch).where(
                        LegacyImportBatch.principal_id == actor,
                        LegacyImportBatch.bundle_hash == value.bundle.bundle_hash,
                    )
                )
                if old is None or old.namespace_id != value.namespace_id:
                    raise
                self._owned(db, actor, old.id)
                return self._summary(old, len(value.bundle.records))

    def records(self, actor: str, batch_id: str, after: str | None = None, limit: int = 50):
        if not 1 <= limit <= 100:
            raise DomainError("PAGE_SIZE", "每页必须为1–100条。", 422)
        with self.db.read() as db:
            self._owned(db, actor, batch_id)
            query = select(LegacyImportRecord).where(LegacyImportRecord.batch_id == batch_id)
            if after:
                cursor = db.get(LegacyImportRecord, after)
                if cursor is None or cursor.batch_id != batch_id:
                    raise DomainError("CURSOR_SCOPE", "游标不属于此归档。", 422)
                query = query.where(LegacyImportRecord.id > after)
            rows = list(db.scalars(query.order_by(LegacyImportRecord.id).limit(limit + 1)))
            data = []
            for row in rows[:limit]:
                if fingerprint(row.payload) != row.content_hash:
                    raise DomainError("LEGACY_HASH", "历史归档内容校验失败。", 409)
                data.append(
                    {
                        "record_id": row.id,
                        "legacy_table": row.table_name,
                        "legacy_id": row.legacy_id,
                        "data": row.payload,
                        "content_hash": row.content_hash,
                        "source_id": row.mapped_source_id,
                        "knowledge_status": "unreviewed_legacy",
                    }
                )
            return {
                "records": data,
                "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
            }

    def import_source(self, actor: str, batch_id: str, value: LegacySourceInput):
        with self.db.transaction() as db:
            batch = self._owned(db, actor, batch_id)
            record = db.scalar(
                select(LegacyImportRecord)
                .where(LegacyImportRecord.id == value.snapshot_record_id)
                .with_for_update()
            )
            if (
                record is None
                or record.batch_id != batch.id
                or record.table_name != "repository_snapshots"
            ):
                raise DomainError("LEGACY_SOURCE_SCOPE", "必须选择该归档中的固定仓库快照。", 422)
            if fingerprint(record.payload) != record.content_hash:
                raise DomainError("LEGACY_HASH", "历史快照已改变。", 409)
            if record.mapped_source_id:
                previous = self.app.sources.get(db, actor, record.mapped_source_id)
                if (
                    previous.license_id != value.license_id
                    or previous.metadata_json.get("rights_confirmed") != value.rights_confirmed
                ):
                    raise DomainError(
                        "LEGACY_REPLAY_CHANGED", "来源用途授权已改变，不能静默复用旧导入。", 409
                    )
                return {
                    "source_id": record.mapped_source_id,
                    "knowledge_status": "requires_new_analysis_and_review",
                }
            related = list(
                db.scalars(
                    select(LegacyImportRecord).where(
                        LegacyImportRecord.batch_id == batch.id,
                        LegacyImportRecord.table_name.in_(["repositories", "repository_files"]),
                    )
                )
            )
            files: dict[str, str] = {}
            repository = None
            for item in related:
                if fingerprint(item.payload) != item.content_hash:
                    raise DomainError("LEGACY_HASH", "历史来源关联内容已改变。", 409)
                if item.table_name == "repositories" and item.legacy_id == record.payload.get(
                    "repository_id"
                ):
                    repository = item.payload
                if (
                    item.table_name == "repository_files"
                    and item.payload.get("snapshot_id") == record.legacy_id
                ):
                    path, content = item.payload.get("path"), item.payload.get("content")
                    if not isinstance(path, str) or not isinstance(content, str) or path in files:
                        raise DomainError("LEGACY_FILE", "历史文件身份或正文无效。", 422)
                    if item.payload.get("content_hash") != digest(content):
                        raise DomainError("LEGACY_FILE_HASH", "历史文件字节与原哈希不一致。", 409)
                    files[path] = content
            if repository is None or repository.get("revoked_at") is not None or not files:
                raise DomainError("LEGACY_SOURCE_SCOPE", "历史仓库已撤销或缺少可核验文件。", 409)
            url = repository.get("github_url")
            commit = record.payload.get("commit_sha")
            if not isinstance(url, str) or not isinstance(commit, str):
                raise DomainError("LEGACY_SOURCE_SCOPE", "历史来源没有固定仓库与提交身份。", 422)
            validate_files(files)
            request = ImportSourceInput(
                namespace_id=batch.namespace_id,
                repository_key=GitHubProvider.name(url),
                commit_sha=commit,
                license_id=value.license_id,
                files=files,
                rights_confirmed=value.rights_confirmed,
                source_metadata={
                    "legacy_baseline": batch.baseline_sha,
                    "legacy_bundle_hash": batch.bundle_hash,
                    "legacy_snapshot_id": record.legacy_id,
                    "legacy_declared_manifest_hash": record.payload.get("manifest_hash"),
                    "migration_scope": "verified_exported_files_only",
                    "legacy_semantic_approval_imported": False,
                },
            )
            # Same transaction retains the privacy lock through the new immutable bytes.
            source_id = self.app.sources.import_source_in(db, actor, request)
            record.mapped_source_id = source_id
            return {"source_id": source_id, "knowledge_status": "requires_new_analysis_and_review"}
