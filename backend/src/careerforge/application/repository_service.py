import ast
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from careerforge.application.common import digest, fingerprint, idempotency
from careerforge.application.ownership import owned
from careerforge.application.planning_service import current_goal, objective_version
from careerforge.application.run_service import create_run
from careerforge.domain.errors import BusinessError, not_found
from careerforge.infrastructure.db.models import (
    ArtifactFeedback,
    CareerGoalVersion,
    CodeCitation,
    ProjectRecommendation,
    ProjectSelection,
    Repository,
    RepositoryFile,
    RepositorySnapshot,
)
from careerforge.infrastructure.repository.snapshot_reader import read_curated, safe_path
from careerforge.schemas.repositories import (
    ProjectResearchInput,
    RecommendationFeedback,
    RepositoryInput,
    SelectionInput,
)

VERSION = "projects-offline-v1"


def register(db: Session, owner: UUID, body: RepositoryInput, key: str | None) -> dict[str, Any]:
    record = idempotency(db, owner, "repositories.create", key, body.model_dump(mode="json"))
    if record.response_summary:
        owned(db, Repository, owner, UUID(record.response_summary["repository_id"]))
        return record.response_summary
    db.execute(
        insert(Repository)
        .values(owner_id=owner, github_url=body.github_url, requested_ref=body.requested_ref)
        .on_conflict_do_nothing(index_elements=["owner_id", "github_url", "requested_ref"])
    )
    row = db.scalar(
        select(Repository).where(
            Repository.owner_id == owner,
            Repository.github_url == body.github_url,
            Repository.requested_ref == body.requested_ref,
            Repository.revoked_at.is_(None),
        )
    )
    if row is None:
        raise BusinessError(409, "REPOSITORY_REVOKED", "此候选已撤销，请显式重新登记新的版本身份。")
    record.response_summary = {
        "repository_id": str(row.id),
        "github_url": row.github_url,
        "requested_ref": row.requested_ref,
        "read_status": "unread",
        "version": row.revision,
    }
    return record.response_summary


def snapshot(db: Session, owner: UUID, identifier: UUID) -> RepositorySnapshot:
    row = owned(db, RepositorySnapshot, owner, identifier)
    owned(db, Repository, owner, row.repository_id)
    return row


def capture(db: Session, owner: UUID, identifier: UUID) -> RepositorySnapshot:
    repository = owned(db, Repository, owner, identifier, lock=True)
    manifest, files = read_curated(repository.github_url, repository.requested_ref)
    hashed = fingerprint(manifest)
    existing = db.scalar(
        select(RepositorySnapshot).where(
            RepositorySnapshot.repository_id == repository.id,
            RepositorySnapshot.commit_sha == manifest["requested_ref"],
            RepositorySnapshot.manifest_hash == hashed,
        )
    )
    if existing:
        return existing
    licence = next((body for path, body, _ in files if path == "LICENSE"), "")
    row = RepositorySnapshot(
        owner_id=owner,
        repository_id=repository.id,
        commit_sha=manifest["requested_ref"],
        manifest_hash=hashed,
        payload={
            "manifest": manifest["files"],
            "coverage": manifest["coverage"],
            "truncated": True,
            "license_status": "MIT" if "MIT License" in licence else "unknown",
            "retrieval_mode": "offline_saved_snapshot",
            "skipped": ["未捕获完整 tree、README、依赖或测试；不声称完整仓库审计"],
            "executed": False,
        },
    )
    db.add(row)
    db.flush()
    for path, body, sha in files:
        status = "not_python"
        if path.endswith(".py"):
            try:
                ast.parse(body)
                status = "parsed"
            except SyntaxError:
                status = "parse_failed"
        db.add(
            RepositoryFile(
                owner_id=owner,
                snapshot_id=row.id,
                path=path,
                content=body,
                content_hash=sha,
                size=len(body.encode()),
                parse_status=status,
            )
        )
    db.flush()
    return row


def file(db: Session, owner: UUID, identifier: UUID, path: str) -> RepositoryFile:
    snapshot(db, owner, identifier)
    normalized = safe_path(path)
    row = db.scalar(
        select(RepositoryFile).where(
            RepositoryFile.snapshot_id == identifier,
            RepositoryFile.path == normalized,
            RepositoryFile.owner_id == owner,
        )
    )
    if row is None:
        raise not_found()
    if digest(row.content) != row.content_hash:
        raise BusinessError(409, "REPOSITORY_HASH_MISMATCH", "代码文本与清单哈希不一致。")
    return row


def read_lines(
    db: Session, owner: UUID, identifier: UUID, path: str, start: int, end: int
) -> dict[str, Any]:
    row = file(db, owner, identifier, path)
    lines = row.content.splitlines(keepends=True)
    if not 1 <= start <= end <= len(lines) or end - start >= 200:
        raise BusinessError(422, "INVALID_CODE_RANGE", "代码行区间无效或超过 200 行。")
    text = "".join(lines[start - 1 : end])
    if len(text.encode()) > 30000:
        raise BusinessError(413, "CODE_CONTEXT_TOO_LARGE", "该区间超过正文读取预算，请缩小行范围。")
    snap = snapshot(db, owner, identifier)
    return {
        "repository_snapshot_id": str(snap.id),
        "commit": snap.commit_sha,
        "path": row.path,
        "start_line": start,
        "end_line": end,
        "content": text,
        "content_hash": digest(text),
        "file_hash": row.content_hash,
        "truncated": False,
    }


def code_citation(
    db: Session, owner: UUID, identifier: UUID, path: str, start: int, end: int
) -> CodeCitation:
    result = read_lines(db, owner, identifier, path, start, end)
    row = CodeCitation(
        owner_id=owner,
        snapshot_id=identifier,
        path=path,
        start_line=start,
        end_line=end,
        content_hash=result["content_hash"],
    )
    db.add(row)
    db.flush()
    return row


def citation_detail(db: Session, owner: UUID, identifier: UUID) -> dict[str, Any]:
    row = owned(db, CodeCitation, owner, identifier)
    value = read_lines(db, owner, row.snapshot_id, row.path, row.start_line, row.end_line)
    if value["content_hash"] != row.content_hash:
        raise BusinessError(409, "CITATION_HASH_MISMATCH", "引用与保存文本不一致。")
    return {
        **value,
        "citation_id": str(row.id),
        "quote": value["content"],
        "availability": "available",
        "support_scope": "静态文本；未经运行验证",
    }


def validate_research(db: Session, owner: UUID, data: dict[str, Any]) -> UUID:
    objectives = [objective_version(db, owner, UUID(i)) for i in data["objective_version_ids"]]
    if (
        len({v.id for v in objectives}) != len(objectives)
        or len({v.goal_version_id for v in objectives}) != 1
    ):
        raise BusinessError(422, "OBJECTIVE_SCOPE_MISMATCH", "目标须不重复且属于同一职业目标版本。")
    repos = [owned(db, Repository, owner, UUID(i)) for i in data["candidate_repository_ids"]]
    snaps = [snapshot(db, owner, UUID(i)) for i in data["candidate_snapshot_ids"]]
    if len(set(data["candidate_repository_ids"])) != len(repos) or len(
        set(data["candidate_snapshot_ids"])
    ) != len(snaps):
        raise BusinessError(422, "DUPLICATE_CANDIDATE", "候选不可重复。")
    if {r.id for r in repos}.intersection(s.repository_id for s in snaps):
        raise BusinessError(422, "AMBIGUOUS_CANDIDATE", "同一候选不可同时提交仓库与快照。")
    for i in data["feedback_ids"]:
        feedback = owned(db, ArtifactFeedback, owner, UUID(i))
        if feedback.artifact_kind != "recommendation":
            raise BusinessError(422, "FEEDBACK_TYPE_MISMATCH", "请选择项目推荐反馈。")
        recommendation(db, owner, feedback.artifact_id)
    return objectives[0].goal_version_id


def start_research(
    db: Session, owner: UUID, body: ProjectResearchInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(db, owner, "project_research.create", key, body.model_dump(mode="json"))
    target = validate_research(db, owner, body.model_dump(mode="json"))
    if record.response_summary:
        return record.response_summary
    run = create_run(
        db,
        owner,
        "project_research",
        VERSION,
        body.model_dump(mode="json"),
        call_budget=0,
        goal_version_id=target,
    )
    record.response_summary = {"run_id": str(run.id), "version": run.version, "status": run.status}
    return record.response_summary


def recommendation(db: Session, owner: UUID, identifier: UUID) -> ProjectRecommendation:
    row = owned(db, ProjectRecommendation, owner, identifier)
    snapshot(db, owner, row.snapshot_id)
    for objective in row.payload["objective_version_ids"]:
        objective_version(db, owner, UUID(objective))
    return row


def select_project(
    db: Session, owner: UUID, body: SelectionInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(db, owner, "project_selections.create", key, body.model_dump(mode="json"))
    candidate = recommendation(db, owner, body.recommendation_id)
    target = owned(db, CareerGoalVersion, owner, body.expected_goal_version_id)
    if candidate.goal_version_id != target.id:
        raise BusinessError(409, "SELECTION_GOAL_MISMATCH", "推荐与当前职业目标版本不一致。")
    if len(set(body.objective_version_ids)) != len(body.objective_version_ids) or not set(
        map(str, body.objective_version_ids)
    ).issubset(candidate.payload["objective_version_ids"]):
        raise BusinessError(422, "SELECTION_OBJECTIVE_MISMATCH", "选中目标不属于此推荐。")
    if record.response_summary:
        return record.response_summary
    current_goal(db, owner, target)
    row = ProjectSelection(
        owner_id=owner,
        recommendation_id=candidate.id,
        snapshot_id=candidate.snapshot_id,
        payload={
            "objective_version_ids": [str(i) for i in body.objective_version_ids],
            "goal_version_id": str(target.id),
        },
    )
    db.add(row)
    db.flush()
    record.response_summary = {
        "selection_id": str(row.id),
        "repository_snapshot_id": str(row.snapshot_id),
        **row.payload,
        "selected_at": row.created_at.isoformat(),
    }
    return record.response_summary


def feedback(
    db: Session, owner: UUID, identifier: UUID, body: RecommendationFeedback, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"recommendation.{identifier}.feedback", key, body.model_dump(mode="json")
    )
    recommendation(db, owner, identifier)
    if record.response_summary:
        return record.response_summary
    row = ArtifactFeedback(
        owner_id=owner,
        artifact_kind="recommendation",
        artifact_id=identifier,
        payload=body.model_dump(mode="json"),
    )
    db.add(row)
    db.flush()
    record.response_summary = {
        "feedback_id": str(row.id),
        "next_action": "explicit_project_research_run",
    }
    return record.response_summary
