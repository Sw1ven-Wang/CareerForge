"""Mutable library organization, independent of frozen source evidence."""

import re
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from careerforge.application.common import check_revision, digest, idempotency
from careerforge.domain.errors import BusinessError, not_found
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.models import Source, SourceLibraryCategory, SourceSnapshot


def lock_library(db: Session, owner: UUID) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int(digest("source-library:" + str(owner))[:15], 16)},
    )


def category(db: Session, owner: UUID, identifier: UUID) -> SourceLibraryCategory:
    row = db.scalar(
        select(SourceLibraryCategory).where(
            SourceLibraryCategory.id == identifier, SourceLibraryCategory.owner_id == owner
        )
    )
    if row is None:
        raise not_found()
    return row


def category_result(row: SourceLibraryCategory) -> Json:
    return {
        "id": str(row.id),
        "parent_id": str(row.parent_id) if row.parent_id else None,
        "name": row.name,
        "version": row.revision,
    }


def categories(db: Session, owner: UUID) -> list[Json]:
    return [
        category_result(row)
        for row in db.scalars(
            select(SourceLibraryCategory)
            .where(SourceLibraryCategory.owner_id == owner)
            .order_by(SourceLibraryCategory.created_at, SourceLibraryCategory.id)
        )
    ]


def ensure_category(
    db: Session, owner: UUID, name: str, parent_id: UUID | None = None
) -> SourceLibraryCategory:
    name = name.strip()
    if not name or len(name) > 100:
        raise BusinessError(422, "INVALID_CATEGORY", "分类名称需要为1至100个字符。")
    row = db.scalar(
        select(SourceLibraryCategory).where(
            SourceLibraryCategory.owner_id == owner,
            SourceLibraryCategory.parent_id == parent_id,
            SourceLibraryCategory.name == name,
        )
    )
    if row is None:
        row = SourceLibraryCategory(owner_id=owner, parent_id=parent_id, name=name)
        db.add(row)
        db.flush()
    return row


def inferred_job(title: str | None) -> str:
    for pattern, name in [
        ("agent|智能体|llm|大模型应用|rag", "Agent 开发"),
        ("后端|backend|back.end|java|golang", "后端开发"),
        ("前端|frontend|front.end", "前端开发"),
        ("算法|机器学习|深度学习|algorithm", "算法工程"),
        ("数据|data", "数据工程"),
        ("测试|test|qa", "测试开发"),
        ("运维|devops|sre|平台工程", "运维 / 平台工程"),
        ("产品|设计|product|design", "产品 / 设计"),
    ]:
        if re.search(pattern, title or "", re.I):
            return name
    return "未分类岗位"


def inferred_source(url: str | None) -> str:
    if not url:
        return "手动导入"
    host = (urlparse(url).hostname or "").removeprefix("www.")
    for domain, name in {
        "zhipin.com": "BOSS直聘",
        "liepin.com": "猎聘",
        "zhaopin.com": "智联招聘",
        "51job.com": "前程无忧",
        "nowcoder.com": "牛客",
        "linkedin.com": "LinkedIn",
        "lagou.com": "拉勾",
    }.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host[:100] or "未分类来源"


def assign(
    db: Session,
    owner: UUID,
    source: Source,
    job_name: str | None = None,
    source_name: str | None = None,
) -> SourceLibraryCategory:
    if source.library_category_id and not job_name and not source_name:
        return category(db, owner, source.library_category_id)
    root = ensure_category(db, owner, (job_name or "").strip() or inferred_job(source.title))
    leaf = ensure_category(
        db, owner, (source_name or "").strip() or inferred_source(source.source_url), root.id
    )
    source.library_category_id = leaf.id
    return leaf


def initialize(db: Session, owner: UUID) -> list[Json]:
    lock_library(db, owner)
    for source in db.scalars(
        select(Source)
        .where(
            Source.owner_id == owner,
            Source.revoked_at.is_(None),
            Source.library_category_id.is_(None),
        )
        .with_for_update()
    ):
        snapshot = db.scalar(
            select(SourceSnapshot)
            .where(SourceSnapshot.owner_id == owner, SourceSnapshot.source_id == source.id)
            .order_by(SourceSnapshot.version_no.desc())
            .limit(1)
        )
        metadata = snapshot.metadata_values if snapshot else {}
        assign(db, owner, source, metadata.get("job_category"), metadata.get("source_category"))
    db.flush()
    return categories(db, owner)


def save_category(
    db: Session, owner: UUID, value: Any, key: str | None, identifier: UUID | None = None
) -> Json:
    record = idempotency(
        db,
        owner,
        "source-library.category." + (str(identifier) if identifier else "create"),
        key,
        value.model_dump(mode="json"),
    )
    lock_library(db, owner)
    if record.response_summary:
        category(db, owner, UUID(record.response_summary["id"]))
        return record.response_summary
    row = category(db, owner, identifier) if identifier else None
    if row:
        check_revision(row.revision, value.expected_version)
        if (row.parent_id is None) != (value.parent_id is None):
            raise BusinessError(422, "INVALID_CATEGORY_LEVEL", "岗位分类和来源分类不能互换层级。")
    if value.parent_id:
        parent = category(db, owner, value.parent_id)
        if parent.parent_id is not None:
            raise BusinessError(422, "INVALID_CATEGORY_PARENT", "来源分类必须放在一级岗位分类下。")
    conflict = db.scalar(
        select(SourceLibraryCategory).where(
            SourceLibraryCategory.owner_id == owner,
            SourceLibraryCategory.parent_id == value.parent_id,
            SourceLibraryCategory.name == value.name,
        )
    )
    if conflict and (row is None or conflict.id != row.id):
        raise BusinessError(409, "CATEGORY_EXISTS", "该层级已有同名分类，请使用其他名称。")
    if row:
        row.name = value.name
        row.parent_id = value.parent_id
        row.revision += 1
    else:
        row = ensure_category(db, owner, value.name, value.parent_id)
    db.flush()
    record.response_summary = category_result(row)
    return record.response_summary


def edit_source(db: Session, owner: UUID, identifier: UUID, value: Any, key: str | None) -> Json:
    from careerforge.application.source_service import get_source

    record = idempotency(
        db, owner, f"source-library.source.{identifier}", key, value.model_dump(mode="json")
    )
    lock_library(db, owner)
    source = get_source(db, owner, identifier, lock=True)
    if record.response_summary:
        return record.response_summary
    check_revision(source.revision, value.expected_version)
    leaf = category(db, owner, value.category_id)
    if leaf.parent_id is None:
        raise BusinessError(422, "INVALID_CATEGORY_LEVEL", "具体岗位必须放在二级来源分类下。")
    source.title = value.title
    source.source_url = value.source_url
    source.source_type = value.source_type
    source.library_category_id = leaf.id
    source.revision += 1
    db.flush()
    record.response_summary = {"source_id": str(source.id), "version": source.revision}
    return record.response_summary
