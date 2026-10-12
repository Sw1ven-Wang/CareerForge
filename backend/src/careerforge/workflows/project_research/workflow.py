from typing import Any
from uuid import UUID

from careerforge.application.planning_service import criteria, objective_version
from careerforge.application.repository_service import (
    VERSION,
    capture,
    code_citation,
    snapshot,
    validate_research,
)
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.code_index.ast_index import symbols
from careerforge.infrastructure.db.models import AgentRun, ProjectRecommendation, RepositoryFile
from careerforge.workflows.shared.pure import execute_pure
from sqlalchemy import select
from sqlalchemy.orm import Session


def validate(db: Session, run: AgentRun) -> dict[str, Any]:
    validate_research(db, run.owner_id, run.input_manifest)
    return {"input_manifest_hash": run.input_manifest_hash, "mode": "offline_curated"}


def produce(db: Session, run: AgentRun) -> dict[str, Any]:
    data = run.input_manifest
    captured = [snapshot(db, run.owner_id, UUID(i)) for i in data["candidate_snapshot_ids"]]
    failures = []
    for identifier in data["candidate_repository_ids"]:
        try:
            with db.begin_nested():
                captured.append(capture(db, run.owner_id, UUID(identifier)))
        except BusinessError as error:
            failures.append({"repository_id": identifier, "error": error.code})
    ids = []
    for saved in captured:
        evidence = []
        files = list(
            db.scalars(
                select(RepositoryFile)
                .where(RepositoryFile.snapshot_id == saved.id)
                .order_by(RepositoryFile.path)
            )
        )
        for file in files:
            if not file.path.endswith(".py"):
                continue
            index = symbols(file.content)
            targets = [
                s for s in index["symbols"] if s["name"] in {"put", "get_tuple", "get", "invoke"}
            ]
            for symbol in targets[:2]:
                end = min(symbol["end_line"], symbol["start_line"] + 79)
                citation = code_citation(
                    db, run.owner_id, saved.id, file.path, symbol["start_line"], end
                )
                evidence.append(
                    {
                        "path": file.path,
                        "symbol": symbol["qualified_name"],
                        "citation_id": str(citation.id),
                        "claim": (
                            f"已解析到 {symbol['qualified_name']} 定义；适用性和运行行为尚未验证。"
                        ),
                        "match_level": "related_unconfirmed",
                        "truncated": end < symbol["end_line"],
                    }
                )
        objectives = [
            objective_version(db, run.owner_id, UUID(i)) for i in data["objective_version_ids"]
        ]
        row = ProjectRecommendation(
            owner_id=run.owner_id,
            run_id=run.id,
            goal_version_id=objectives[0].goal_version_id,
            snapshot_id=saved.id,
            payload={
                "objective_version_ids": data["objective_version_ids"],
                "criterion_version_ids": [
                    str(c.id) for o in objectives for c in criteria(db, run.owner_id, o.id)
                ],
                "repository_snapshot_id": str(saved.id),
                "commit": saved.commit_sha,
                "code_evidence": evidence,
                "match_level": "related_unconfirmed",
                "license_status": saved.payload["license_status"],
                "reading_scope": [f.path for f in files],
                "difficulty_reason": "局部实现；需理解事务与版本；阅读工时未实测。",
                "resource_constraints": ["静态阅读不安装框架；实验资源须在本人环境确认"],
                "unknowns": [
                    "未捕获 README、入口、依赖和测试，无法验证完整调用链",
                    "代码存在不证明已满足目标全部条件",
                ],
                "mode": "offline_curated",
                "executed": False,
            },
        )
        db.add(row)
        db.flush()
        ids.append(str(row.id))
    return {
        "recommendation_ids": ids,
        "failed_candidates": failures,
        "_run_status": "failed" if not ids else "partially_succeeded" if failures else "succeeded",
    }


def execute(run_id: UUID, worker: str) -> None:
    execute_pure(run_id, worker, VERSION, validate, produce)
