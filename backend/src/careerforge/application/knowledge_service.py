"""Owner-scoped code knowledge and verified plan-to-code associations."""

from uuid import UUID

from sqlalchemy import case, delete, func, literal_column, select

from careerforge.application import repository_service as repositories
from careerforge.application.common import digest, fingerprint, idempotency
from careerforge.application.ownership import owned
from careerforge.application.planning_service import objective_version
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.code_index.knowledge_index import (
    VERSION,
    chunks,
    query_terms,
    tokens,
)
from careerforge.infrastructure.db.models import (
    CodeCitation,
    CodeKnowledgeChunk,
    CodeKnowledgeIndex,
    CodeLearningLink,
    Repository,
    RepositoryFile,
    RepositorySnapshot,
)
from careerforge.infrastructure.repository.knowledge_reader import fetch_github, validate_submitted
from careerforge.schemas.repositories import RepositoryInput


def index_row(db, snapshot_id):
    return db.scalar(
        select(CodeKnowledgeIndex).where(CodeKnowledgeIndex.snapshot_id == snapshot_id)
    )


def search_text(chunk, metadata):
    context = " ".join(
        [metadata.get("name", ""), metadata.get("description", ""), *metadata.get("tags", [])]
    )
    return " ".join(tokens(f"{chunk.path} {chunk.symbol} " * 3 + chunk.content + " " + context))


def build_index(db, owner, snapshot_id, metadata=None):
    snapshot = repositories.snapshot(db, owner, snapshot_id)
    owned(db, RepositorySnapshot, owner, snapshot_id, lock=True)
    existing = index_row(db, snapshot_id)
    if existing and existing.payload.get("index_version") == VERSION:
        return project_detail(db, owner, snapshot_id)
    files = list(
        db.scalars(
            select(RepositoryFile)
            .where(RepositoryFile.snapshot_id == snapshot_id)
            .order_by(RepositoryFile.path)
        )
    )
    if len(files) > 1000 or sum(f.size for f in files) > 12_000_000:
        raise BusinessError(413, "KNOWLEDGE_INDEX_LIMIT", "索引最多 1000 个文件 / 12 MB。")
    repository = owned(db, Repository, owner, snapshot.repository_id)
    metadata = metadata or (
        existing.payload
        if existing
        else {"name": repository.github_url.rsplit("/", 1)[-1], "description": "", "tags": []}
    )
    db.execute(delete(CodeKnowledgeChunk).where(CodeKnowledgeChunk.snapshot_id == snapshot_id))
    count = 0
    languages = set()
    for file in files:
        if digest(file.content) != file.content_hash:
            raise BusinessError(409, "REPOSITORY_HASH_MISMATCH", "源码哈希不一致，停止索引。")
        indexed = chunks(file.path, file.content)
        if indexed:
            file.parse_status = indexed[0]["payload"]["parse_status"]
        for item in indexed:
            count += 1
            if count > 20000:
                raise BusinessError(
                    413, "KNOWLEDGE_CHUNK_LIMIT", "代码片段超过 20,000，请缩小导入范围。"
                )
            row = CodeKnowledgeChunk(
                owner_id=owner,
                snapshot_id=snapshot_id,
                ordinal=count,
                content_hash=digest(item["content"]),
                search_text="",
                **item,
            )
            row.search_text = search_text(row, metadata)
            languages.add(item["payload"]["language"])
            db.add(row)
    payload = {
        **metadata,
        "index_version": VERSION,
        "file_count": len(files),
        "chunk_count": count,
        "languages": sorted(languages),
        "semantic_enabled": False,
        "retrieval_mode": "lexical_structure",
        "executed": False,
    }
    if existing:
        existing.payload = payload
        existing.revision += 1
    else:
        db.add(
            CodeKnowledgeIndex(owner_id=owner, snapshot_id=snapshot_id, revision=1, payload=payload)
        )
    db.flush()
    return project_detail(db, owner, snapshot_id)


def import_project(db, owner, body, key):
    record = idempotency(db, owner, "knowledge.import", key, body.model_dump(mode="json"))
    if record.response_summary:
        return project_detail(db, owner, UUID(record.response_summary["repository_snapshot_id"]))
    if body.files is None:
        files, skipped = fetch_github(body.github_url, body.requested_ref)
        origin = "github_commit_archive"
    else:
        files, skipped = validate_submitted(body.files), {}
        origin = "user_submitted_text"
    # Registration also serializes same repository/version imports, without another request key.
    registration = RepositoryInput(github_url=body.github_url, requested_ref=body.requested_ref)
    from sqlalchemy.dialects.postgresql import insert

    db.execute(
        insert(Repository)
        .values(owner_id=owner, **registration.model_dump())
        .on_conflict_do_nothing()
    )
    repository = db.scalar(
        select(Repository)
        .where(
            Repository.owner_id == owner,
            Repository.github_url == body.github_url,
            Repository.requested_ref == body.requested_ref,
        )
        .with_for_update()
    )
    if repository.revoked_at:
        raise BusinessError(409, "REPOSITORY_REVOKED", "此仓库版本已撤销。")
    manifest = [
        {"path": name, "sha256": digest(text), "bytes": len(text.encode())}
        for name, text in sorted(files.items())
    ]
    hashed = fingerprint({"commit": body.requested_ref, "manifest": manifest, "provenance": origin})
    snapshot = db.scalar(
        select(RepositorySnapshot).where(
            RepositorySnapshot.repository_id == repository.id,
            RepositorySnapshot.manifest_hash == hashed,
        )
    )
    if snapshot is None:
        license_text = next(
            (
                text
                for name, text in files.items()
                if name.lower() in {"license", "licence", "license.md", "license.txt"}
            ),
            "",
        )
        snapshot = RepositorySnapshot(
            owner_id=owner,
            repository_id=repository.id,
            commit_sha=body.requested_ref,
            manifest_hash=hashed,
            payload={
                "manifest": manifest,
                "retrieval_mode": origin,
                "commit_verified": origin == "github_commit_archive",
                "coverage": {"included_files": len(files), "skipped": skipped},
                "truncated": bool(skipped) or origin == "user_submitted_text",
                "license_status": "MIT" if "MIT License" in license_text else "unknown",
                "executed": False,
                "skipped": ["本地目录的 commit 为用户声明，未与远端比对"]
                if origin == "user_submitted_text"
                else [],
            },
        )
        db.add(snapshot)
        db.flush()
        for name, text in sorted(files.items()):
            db.add(
                RepositoryFile(
                    owner_id=owner,
                    snapshot_id=snapshot.id,
                    path=name,
                    content=text,
                    content_hash=digest(text),
                    size=len(text.encode()),
                    parse_status="not_indexed",
                )
            )
        db.flush()
    result = build_index(
        db,
        owner,
        snapshot.id,
        {"name": body.name, "description": body.description, "tags": body.tags},
    )
    record.response_summary = {"repository_snapshot_id": str(snapshot.id)}
    return result


def project_detail(db, owner, snapshot_id):
    snapshot = repositories.snapshot(db, owner, snapshot_id)
    repository = owned(db, Repository, owner, snapshot.repository_id)
    index = index_row(db, snapshot_id)
    return {
        "repository_snapshot_id": str(snapshot.id),
        "repository_id": str(repository.id),
        "github_url": repository.github_url,
        "commit": snapshot.commit_sha,
        "provenance": snapshot.payload.get("retrieval_mode"),
        "commit_verified": snapshot.payload.get("commit_verified", False),
        "coverage": snapshot.payload.get("coverage"),
        "truncated": snapshot.payload.get("truncated", True),
        "license_status": snapshot.payload.get("license_status", "unknown"),
        "indexed": index is not None,
        "revision": index.revision if index else 0,
        "name": repository.github_url.rsplit("/", 1)[-1],
        **(index.payload if index else {}),
    }


def projects(db, owner):
    rows = db.scalars(
        select(RepositorySnapshot)
        .join(Repository, Repository.id == RepositorySnapshot.repository_id)
        .where(RepositorySnapshot.owner_id == owner, Repository.revoked_at.is_(None))
        .order_by(RepositorySnapshot.created_at.desc())
        .limit(200)
    )
    return [project_detail(db, owner, row.id) for row in rows]


def update_metadata(db, owner, snapshot_id, body):
    repositories.snapshot(db, owner, snapshot_id)
    owned(db, RepositorySnapshot, owner, snapshot_id, lock=True)
    index = index_row(db, snapshot_id)
    if not index or index.revision != body.expected_revision:
        raise BusinessError(409, "KNOWLEDGE_REVISION_CONFLICT", "索引资料已变化，请刷新。")
    metadata = body.model_dump(exclude={"expected_revision"})
    index.payload = {**index.payload, **metadata}
    index.revision += 1
    for row in db.scalars(
        select(CodeKnowledgeChunk).where(CodeKnowledgeChunk.snapshot_id == snapshot_id)
    ):
        row.search_text = search_text(row, metadata)
    db.flush()
    return project_detail(db, owner, snapshot_id)


def chunk_detail(db, owner, identifier):
    row = owned(db, CodeKnowledgeChunk, owner, identifier)
    snapshot = repositories.snapshot(db, owner, row.snapshot_id)
    file = repositories.file(db, owner, row.snapshot_id, row.path)
    text = "".join(file.content.splitlines(keepends=True)[row.start_line - 1 : row.end_line])
    if digest(text) != row.content_hash or text != row.content:
        raise BusinessError(409, "KNOWLEDGE_HASH_MISMATCH", "索引与原始代码不一致，请重建索引。")
    return {
        "chunk_id": str(row.id),
        "repository_snapshot_id": str(row.snapshot_id),
        "commit": snapshot.commit_sha,
        "path": row.path,
        "symbol": row.symbol,
        "kind": row.kind,
        "start_line": row.start_line,
        "end_line": row.end_line,
        "content": row.content,
        "content_hash": row.content_hash,
        **row.payload,
    }


def search(db, owner, query, snapshot_id=None, limit=20, scope_ids=None):
    terms, aliases = query_terms(query)
    if scope_ids is not None:
        for identifier in scope_ids:
            repositories.snapshot(db, owner, identifier)
    if snapshot_id:
        repositories.snapshot(db, owner, snapshot_id)
    if not terms:
        return {
            "results": [],
            "retrieval_mode": "lexical_structure",
            "semantic_enabled": False,
            "expanded_topics": aliases,
        }
    # Terms come exclusively from the tokenizer; no tsquery syntax from user input.
    config = literal_column("'simple'::regconfig")
    vector = func.to_tsvector(config, CodeKnowledgeChunk.search_text)
    tsquery = func.to_tsquery(config, " | ".join(terms))
    rank = func.ts_rank_cd(vector, tsquery)
    statement = (
        select(CodeKnowledgeChunk, rank.label("rank"))
        .join(RepositorySnapshot, RepositorySnapshot.id == CodeKnowledgeChunk.snapshot_id)
        .join(Repository, Repository.id == RepositorySnapshot.repository_id)
        .where(
            CodeKnowledgeChunk.owner_id == owner,
            Repository.revoked_at.is_(None),
            vector.op("@@")(tsquery),
        )
    )
    if snapshot_id:
        statement = statement.where(CodeKnowledgeChunk.snapshot_id == snapshot_id)
    if scope_ids is not None:
        statement = statement.where(CodeKnowledgeChunk.snapshot_id.in_(scope_ids))
    identifier = query.strip().lower()
    exact = (func.lower(CodeKnowledgeChunk.symbol) == identifier) | func.lower(
        CodeKnowledgeChunk.symbol
    ).endswith("." + identifier, autoescape=True)
    candidates = db.execute(
        statement.order_by(
            case((exact, 1), else_=0).desc(), rank.desc(), CodeKnowledgeChunk.id
        ).limit(200)
    ).all()
    scored = []
    for row, base in candidates:
        names = set(tokens(row.symbol + " " + row.path))
        body = set(tokens(row.content))
        hits = [t for t in terms if t in names or t in body]
        score = float(base) + sum(2 if t in names else 0.25 for t in hits)
        if row.symbol.lower() == identifier or row.symbol.lower().endswith("." + identifier):
            score += 50
        if row.kind != "file_window":
            score += 0.2
        scored.append((score, row, hits))
    scored.sort(key=lambda item: (-item[0], item[1].path, item[1].start_line))
    selected = []
    seen = set()
    for score, row, hits in scored:
        identity = (row.snapshot_id, row.path, row.start_line, row.end_line)
        if identity in seen:
            continue
        seen.add(identity)
        selected.append(
            {
                **chunk_detail(db, owner, row.id),
                "score": round(score, 3),
                "matched_terms": hits,
                "match_reason": "符号/路径/原文关键词" if hits else "项目资料与主题标签",
                "project_name": index_row(db, row.snapshot_id).payload.get("name", ""),
            }
        )
        if len(selected) >= limit:
            break
    return {
        "results": selected,
        "retrieval_mode": "lexical_structure",
        "semantic_enabled": False,
        "expanded_topics": aliases,
        "candidate_limit": 200,
        "limitations": [
            "中文主题使用显式别名扩展；未启用向量与模型重排序",
            "非 Python 文件采用文本窗口索引",
        ],
    }


def related(db, owner, identifier):
    item = chunk_detail(db, owner, identifier)
    names = {name.split(".")[-1] for name in item["references"]}
    row = owned(db, CodeKnowledgeChunk, owner, identifier)
    # Names are static candidates, deliberately not presented as proven call edges.
    rows = db.scalars(
        select(CodeKnowledgeChunk)
        .where(
            CodeKnowledgeChunk.snapshot_id == row.snapshot_id,
            CodeKnowledgeChunk.kind != "file_window",
            CodeKnowledgeChunk.id != row.id,
        )
        .order_by(CodeKnowledgeChunk.ordinal)
    )
    result = []
    for target in rows:
        if target.symbol.rsplit(".", 1)[-1] in names or (
            target.path == row.path and target.symbol.startswith(row.symbol + ".")
        ):
            result.append(
                {
                    **chunk_detail(db, owner, target.id),
                    "relation": "contains"
                    if target.path == row.path and target.symbol.startswith(row.symbol + ".")
                    else "reference_candidate",
                }
            )
        if len(result) >= 20:
            break
    return {
        "results": result,
        "support_scope": "静态包含或同名引用候选；不证明动态调用，可能存在歧义",
    }


def citation(db, owner, chunk_id):
    item = chunk_detail(db, owner, chunk_id)
    row = db.scalar(
        select(CodeCitation).where(
            CodeCitation.owner_id == owner,
            CodeCitation.snapshot_id == UUID(item["repository_snapshot_id"]),
            CodeCitation.path == item["path"],
            CodeCitation.start_line == item["start_line"],
            CodeCitation.end_line == item["end_line"],
            CodeCitation.content_hash == item["content_hash"],
        )
    )
    if row is None:
        row = repositories.code_citation(
            db,
            owner,
            UUID(item["repository_snapshot_id"]),
            item["path"],
            item["start_line"],
            item["end_line"],
        )
    return repositories.citation_detail(db, owner, row.id)


def bind(db, owner, body, key):
    objective_version(db, owner, body.objective_version_id)
    record = idempotency(db, owner, "knowledge.bind", key, body.model_dump(mode="json"))
    if record.response_summary:
        repositories.citation_detail(db, owner, UUID(record.response_summary["citation_id"]))
        return record.response_summary
    from sqlalchemy.dialects.postgresql import insert

    item = citation(db, owner, body.chunk_id)
    db.execute(
        insert(CodeLearningLink)
        .values(
            owner_id=owner,
            objective_version_id=body.objective_version_id,
            citation_id=UUID(item["citation_id"]),
            note=body.note,
        )
        .on_conflict_do_nothing()
    )
    record.response_summary = {
        "objective_version_id": str(body.objective_version_id),
        "citation_id": item["citation_id"],
    }
    return record.response_summary


def links(db, owner, objective_id):
    objective_version(db, owner, objective_id)
    rows = db.scalars(
        select(CodeLearningLink)
        .where(
            CodeLearningLink.owner_id == owner,
            CodeLearningLink.objective_version_id == objective_id,
        )
        .order_by(CodeLearningLink.created_at)
        .limit(100)
    )
    result = []
    for row in rows:
        try:
            item = repositories.citation_detail(db, owner, row.citation_id)
            result.append({**item, "link_id": str(row.id), "note": row.note})
        except BusinessError as error:
            if error.status != 404:
                raise
    return result
