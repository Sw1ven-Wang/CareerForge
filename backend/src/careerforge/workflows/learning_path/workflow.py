from typing import Any
from uuid import UUID

from careerforge.application import learning_service as service
from careerforge.application.ownership import owned
from careerforge.application.planning_service import criteria, objective_version
from careerforge.application.repository_service import citation_detail, recommendation
from careerforge.infrastructure.db.models import (
    AgentRun,
    LearningProgress,
    LearningUnit,
    ProjectSelection,
    TaskDraft,
)
from careerforge.workflows.shared.pure import execute_pure
from sqlalchemy.orm import Session


def validate(db: Session, run: AgentRun) -> dict[str, Any]:
    service.validate(db, run.owner_id, run.input_manifest)
    return {"mode": "offline_static", "input_manifest_hash": run.input_manifest_hash}


def produce(db: Session, run: AgentRun) -> dict[str, Any]:
    data = run.input_manifest
    selection = owned(db, ProjectSelection, run.owner_id, UUID(data["selection_id"]))
    rec = recommendation(db, run.owner_id, selection.recommendation_id)
    obj = objective_version(db, run.owner_id, UUID(data["objective_version_ids"][0]))
    steps = []
    explanations = []
    citations = []
    for n, item in enumerate(rec.payload["code_evidence"][:6]):
        quote = citation_detail(db, run.owner_id, UUID(item["citation_id"]))
        citations.append(item["citation_id"])
        steps.append(
            {
                "step_id": f"read-{n + 1}",
                "title": item["symbol"],
                "file": quote["path"],
                "start_line": quote["start_line"],
                "end_line": quote["end_line"],
                "why": "核对定义中的输入、分支及持久化边界；不据此推断完整调用链。",
                "questions": ["重复调用和失败时哪些状态可观察？", "哪些结论需要调用方或运行记录？"],
                "citation_ids": [item["citation_id"]],
            }
        )
        explanations.append(
            {
                "text": f"固定文本包含 {item['symbol']} 的定义。",
                "citation_ids": [item["citation_id"]],
                "support_scope": "定义存在；运行行为与目标适用性未知",
            }
        )
    row = LearningUnit(
        owner_id=run.owner_id,
        run_id=run.id,
        selection_id=selection.id,
        objective_version_id=obj.id,
        snapshot_id=selection.snapshot_id,
        supersedes_unit_id=UUID(data["previous_learning_unit_id"])
        if data.get("previous_learning_unit_id")
        else None,
        payload={},
    )
    db.add(row)
    db.flush()
    conditions = [str(c.id) for c in criteria(db, run.owner_id, obj.id)]
    draft = TaskDraft(
        owner_id=run.owner_id,
        unit_id=row.id,
        objective_version_id=obj.id,
        payload={
            "title": obj.payload["title"],
            "target_behavior": obj.payload["target_behavior"],
            "change_scenario": "将参考机制迁移到本人项目，说明自己的变更与边界",
            "deliverables": obj.payload["deliverables"],
            "criterion_version_ids": conditions,
            "effort_hours": obj.payload.get("effort_hours"),
        },
    )
    db.add(draft)
    db.flush()
    row.payload = {
        "objective_version_id": str(obj.id),
        "criterion_version_ids": conditions,
        "repository_snapshot_id": str(selection.snapshot_id),
        "commit": rec.payload["commit"],
        "why_this_unit": obj.payload["target_behavior"],
        "prerequisites": obj.payload["prerequisite_objective_ids"],
        "reading_steps": steps,
        "mechanism_explanations": explanations,
        "understanding_questions": [
            "指出参考实现中的事务边界，并区分已观察与推断。",
            "若输入重复或进程中断，你的实现如何检查结果？",
        ],
        "experiment": {
            "instruction": (
                "在本人隔离环境实现最小迁移；记录固定版本、环境、"
                "正常及失败输入与实际输出。由本人主动执行。"
            ),
            "platform_executes": False,
        },
        "expected_observations": ["对照完整验收条件逐项记录符合、失败或未知；预期不代表实际结果"],
        "completion_conditions": conditions,
        "task_drafts": [{"task_draft_id": str(draft.id), **draft.payload}],
        "citations": citations,
        "unknowns": [
            "缺少入口、调用方与测试；未证明完整调用关系",
            "静态阅读不证明实验运行或能力掌握",
        ],
        "mode": "offline_static",
        "question_response": {
            "question": data["question"],
            "answer": "请核对固定文本引用；超出捕获范围的机制与运行结论保持未知。",
            "citation_ids": citations,
        }
        if data.get("question")
        else None,
    }
    db.add(
        LearningProgress(
            owner_id=run.owner_id,
            unit_id=row.id,
            payload={"completed_step_ids": [], "actual_minutes": None, "note": ""},
        )
    )
    return {"learning_unit_id": str(row.id)}


def execute(run_id: UUID, worker: str) -> None:
    execute_pure(run_id, worker, service.VERSION, validate, produce)
