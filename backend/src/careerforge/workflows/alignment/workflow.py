from typing import Any, Literal, cast
from uuid import UUID

from careerforge.application.goal_service import get_goal
from careerforge.application.market_report_service import read_report
from careerforge.application.ownership import owned
from careerforge.application.planning_service import (
    VERSION,
    new_plan_version,
    persist_objective,
    validate_alignment,
)
from careerforge.infrastructure.db.models import (
    AgentRun,
    AlignmentReport,
    ArtifactDependency,
    CareerGoalVersion,
    LearningObjective,
    LearningObjectiveVersion,
    LearningPlan,
)
from careerforge.schemas.planning import CriterionInput, ObjectiveInput
from careerforge.workflows.shared.pure import execute_pure
from sqlalchemy import select
from sqlalchemy.orm import Session


def validate(db: Session, run: AgentRun) -> dict[str, Any]:
    validate_alignment(db, run.owner_id, run.input_manifest)
    return {"input_manifest_hash": run.input_manifest_hash, "mode": "offline_conservative_rules"}


def produce(db: Session, run: AgentRun) -> dict[str, Any]:
    data, owner = run.input_manifest, run.owner_id
    target = owned(db, CareerGoalVersion, owner, UUID(data["goal_version_id"]))
    get_goal(db, owner, target.goal_id, lock=True)
    market = read_report(db, owner, UUID(data["market_report_id"]))
    eligible = set(market.payload["eligible_posting_version_ids"])
    reqs = [
        r
        for r in market.payload["requirements"]
        if r["posting_version_id"] in eligible and r["origin"] == "explicit"
    ]
    priority = {"workflow_state": 0, "exception_handling": 1, "evaluation": 2}
    reqs.sort(key=lambda r: (priority.get(r["normalized_skill"], 10), r["requirement_id"]))
    report = AlignmentReport(owner_id=owner, goal_version_id=target.id, run_id=run.id, payload={})
    db.add(report)
    db.flush()
    selected: dict[str, UUID] = {}
    for requirement in reqs:
        concept = requirement["normalized_skill"]
        if concept in selected or len(selected) >= 3:
            continue
        # Reuse a stable objective covering this exact requirement.
        existing = list(
            db.scalars(
                select(LearningObjectiveVersion)
                .where(
                    LearningObjectiveVersion.owner_id == owner,
                    LearningObjectiveVersion.goal_version_id == target.id,
                )
                .order_by(LearningObjectiveVersion.version_no.desc())
            )
        )
        prior = next(
            (
                v
                for v in existing
                if requirement["requirement_id"] in v.payload.get("requirement_ids", [])
            ),
            None,
        )
        if prior:
            selected[concept] = prior.id
            continue
        cs = [
            CriterionInput(
                dimension=cast(Literal["artifact", "explanation", "transfer"], dimension),
                scenario=f"本人项目中一个明确的 {concept} 变更场景；先核对范围再开始",
                expected_behavior=behavior,
                pass_rule=passed,
                fail_rule="材料显示此条件不满足",
                unknown_rule="材料缺失、版本或环境不一致时保持未知",
                critical=True,
                allowed_methods=["user_reported", "human_review"],
                required_artifact_types=[artifact],
                limitations=["仅覆盖提交版本与声明环境；运行记录来自用户，不是平台执行"],
            )
            for dimension, behavior, passed, artifact in [
                (
                    "artifact",
                    "提供固定版本实现与正常/失败场景观察记录",
                    "记录可追踪到代码版本、命令、环境、退出码及实际输出",
                    "execution_record",
                ),
                (
                    "explanation",
                    "解释状态变化、失败边界和防重复取舍",
                    "回答引用具体实现并说明一个反例",
                    "explanation_answer",
                ),
                (
                    "transfer",
                    "将参考机制用于本人项目中有变化的场景",
                    "提供变更差异、场景变化及验证范围",
                    "transfer_note",
                ),
            ]
        ]
        value = ObjectiveInput(
            goal_version_id=target.id,
            title=f"在本人项目验证 {concept} 的一个机制",
            target_behavior=f"围绕原文要求“{requirement['text']}”，提交一个有版本、异常观察和解释的独立增量",
            scope="一个本人项目、固定输入、正常与失败场景；采纳前核对范围",
            deliverables=["版本化实现", "用户提供的实验记录", "机制解释及迁移差异"],
            criteria=cs,
            scope_confirmed=True,
        )
        root = LearningObjective(owner_id=owner, goal_id=target.goal_id)
        db.add(root)
        db.flush()
        version = persist_objective(
            db, owner, value, root, "inferred_from_requirement", [requirement["requirement_id"]]
        )
        version.payload = {k: v for k, v in version.payload.items() if k != "scope_confirmed"}
        selected[concept] = version.id
    plan = db.scalar(
        select(LearningPlan).where(LearningPlan.goal_id == target.goal_id).with_for_update()
    )
    if plan is None:
        plan = LearningPlan(owner_id=owner, goal_id=target.goal_id)
        db.add(plan)
        db.flush()
    else:
        plan.revision += 1
    plan_version = new_plan_version(
        db,
        plan,
        target.id,
        list(selected.values()),
        "draft",
        {
            "user_constraints": data["user_constraints"],
            "schedule_status": "unconfirmed",
            "deferred": [],
            "warnings": ["工时与排期未知；采纳前请核对目标范围与条件。"],
        },
        report.id,
    )
    report.payload = {
        "input_manifest": data,
        "mode": "offline_conservative_rules",
        "synthetic_model": False,
        "alignments": [
            {
                "requirement_ids": [r["requirement_id"]],
                "evidence_version_ids": data["personal_evidence_version_ids"],
                "category": "unknown",
                "reason": "静态材料不能证明本人贡献、运行或理解深度。",
                "unknown_questions": [
                    "本人实现范围是什么？",
                    "能否补充与该条件对应的版本和实验记录？",
                ],
                "objective_version_ids": [str(selected[r["normalized_skill"]])]
                if r["normalized_skill"] in selected
                else [],
            }
            for r in reqs
        ],
        "objective_version_ids": [str(i) for i in selected.values()],
        "plan_id": str(plan.id),
        "plan_version_id": str(plan_version.id),
    }
    db.add(
        ArtifactDependency(
            owner_id=owner,
            consumer_kind="alignment",
            consumer_id=report.id,
            input_kind="market_report",
            input_id=market.id,
        )
    )
    for identifier in data["personal_evidence_version_ids"]:
        from careerforge.application.planning_service import evidence_version

        evidence = evidence_version(db, owner, UUID(identifier))
        db.add(
            ArtifactDependency(
                owner_id=owner,
                consumer_kind="alignment",
                consumer_id=report.id,
                input_kind="evidence",
                input_id=evidence.evidence_id,
            )
        )
    return {
        "alignment_report_id": str(report.id),
        "plan_id": str(plan.id),
        "plan_version_id": str(plan_version.id),
        "_run_status": "succeeded" if selected else "failed",
    }


def execute(run_id: UUID, worker: str) -> None:
    execute_pure(run_id, worker, VERSION, validate, produce)
