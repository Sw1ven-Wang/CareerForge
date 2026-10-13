from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application import build_service as service
from careerforge.application.ownership import owned
from careerforge.application.planning_service import exact_ids, objective_version
from careerforge.infrastructure.db.models import (
    AgentRun,
    Assessment,
    CareerGoalVersion,
    LearningPlan,
    PlanDeltaProposal,
)
from careerforge.workflows.shared.pure import execute_pure


def validate(db: Session, run: AgentRun) -> dict[str, Any]:
    saved = service.submission(db, run.owner_id, UUID(run.input_manifest["submission_id"]))
    version = service.task_version(db, run.owner_id, saved.task_version_id)
    exact_ids(
        [c.id for c in service.frozen_criteria(db, run.owner_id, version)],
        [UUID(i) for i in run.input_manifest["criterion_version_ids"]],
    )
    return {"mode": "offline_material_inventory", "input_manifest_hash": run.input_manifest_hash}


def produce(db: Session, run: AgentRun) -> dict[str, Any]:
    saved = service.submission(db, run.owner_id, UUID(run.input_manifest["submission_id"]))
    version = service.task_version(db, run.owner_id, saved.task_version_id)
    full = service.frozen_criteria(db, run.owner_id, version)
    items = [
        {
            "criterion_version_id": str(c.id),
            "verdict": "unknown",
            "method": "model_review",
            "evidence_refs": saved.payload["attachment_ids"],
            "rationale": "离线材料盘点无法验证行为或本人理解；需分项自验。",
            "limitations": [
                "未调用模型、未执行代码",
                *[
                    f"缺少材料：{kind}"
                    for kind in c.payload["required_artifact_types"]
                    if kind not in service.materials(saved, c.id)
                ],
            ],
        }
        for c in full
    ]
    proposal = Assessment(
        owner_id=run.owner_id,
        submission_id=saved.id,
        run_id=run.id,
        payload={
            "criterion_version_ids": [str(c.id) for c in full],
            "proposal_version": 1,
            "items": items,
            "suggested_task_outcome": "needs_revision",
            "missing_evidence": ["对照完整条件核查运行、解释和迁移材料"],
            "artifact_evidence": saved.payload["attachment_ids"],
            "explanation_evidence": saved.payload["explanation_answers"],
            "transfer_evidence": saved.payload.get("transfer_notes", []),
            "mode": "offline_material_inventory",
        },
    )
    db.add(proposal)
    db.flush()
    objective = objective_version(db, run.owner_id, version.objective_version_id)
    plan = db.scalar(
        select(LearningPlan).where(
            LearningPlan.owner_id == run.owner_id,
            LearningPlan.goal_id
            == owned(db, CareerGoalVersion, run.owner_id, objective.goal_version_id).goal_id,
        )
    )
    delta = PlanDeltaProposal(
        owner_id=run.owner_id,
        assessment_id=proposal.id,
        payload={
            "base_plan_version_id": str(plan.active_version_id)
            if plan and plan.active_version_id
            else None,
            "assessment_id": str(proposal.id),
            "retain": [],
            "adjust": [
                {"objective_version_id": str(objective.id), "reason": "全部条件尚待实际证据核查"}
            ],
            "defer": [],
            "add": [],
            "automatic_adoption": False,
        },
    )
    db.add(delta)
    db.flush()
    proposal.payload = {**proposal.payload, "plan_delta_proposal_id": str(delta.id)}
    return {"assessment_id": str(proposal.id)}


def execute(run_id: UUID, worker: str) -> None:
    execute_pure(run_id, worker, service.VERSION, validate, produce)
