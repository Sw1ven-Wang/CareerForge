"""Build editable draft plans from server-frozen demand expressions, without model calls."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application.common import fingerprint
from careerforge.application.goal_service import get_goal
from careerforge.application.job_contract_service import read
from careerforge.application.ownership import owned
from careerforge.application.planning_service import new_plan_version, persist_objective
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.models import (
    AgentRun,
    AlignmentReport,
    CareerGoalVersion,
    LearningObjective,
    LearningPlan,
)
from careerforge.schemas.planning import CriterionInput, ObjectiveInput


def choose(expr: Json, preferences: set[str], decisions: list[Json]) -> list[str]:
    if expr["type"] == "constraint":
        return []
    if expr["type"] == "concept":
        return [expr["concept_id"]]
    if expr["type"] == "all_of":
        return [
            concept
            for child in expr["children"]
            for concept in choose(child, preferences, decisions)
        ]
    branches = []
    for child in expr["children"]:
        nested: list[Json] = []
        concepts = choose(child, preferences, nested)
        branches.append((concepts, nested, child))
    scored = sorted(
        enumerate(branches),
        key=lambda item: (
            -sum(c.split(".")[-1].casefold() in preferences for c in item[1][0]),
            item[0],
        ),
    )
    index, (selected, nested, child) = scored[0]
    decisions.extend(nested)
    decisions.append(
        {
            "expression": expr,
            "selected_branch": child,
            "selected_concepts": selected,
            "reason": "用户语言偏好优先；无匹配偏好时取原文首个分支，草稿待确认",
        }
    )
    return selected


def produce(db: Session, run: AgentRun) -> Json:
    owner = run.owner_id
    data = run.input_manifest
    context = read(db, owner, UUID(data["planning_context_id"]), "context").payload["context"]
    report = read(db, owner, UUID(context["demand_report_id"]), "report").payload
    target = owned(db, CareerGoalVersion, owner, UUID(context["goal_version_id"]))
    get_goal(db, owner, target.goal_id, lock=True)
    selected = set(context["selected_demand_ids"])
    facts = {f["requirement_id"]: f for f in report["facts"]}
    demands = [d for d in report["report"]["demands"] if d["demand_id"] in selected]
    preferences = {p.casefold() for p in context["user_constraints"]["preferred_languages"]}
    objective_ids = []
    alignments = []
    covered: set[str] = set()
    alignment = AlignmentReport(
        owner_id=owner, goal_version_id=target.id, run_id=run.id, payload={}
    )
    db.add(alignment)
    db.flush()
    for demand in demands:
        for fact_id in demand["supporting_requirement_ids"]:
            fact = facts[fact_id]
            decisions: list[Json] = []
            concepts = choose(fact["expression"], preferences, decisions)
            for concept in dict.fromkeys(concepts):
                if concept in covered:
                    continue
                covered.add(concept)
                criteria = [
                    CriterionInput.model_validate(
                        dict(
                            dimension=dimension,
                            scenario=f"本人项目中的一个 {concept} 场景",
                            expected_behavior=behavior,
                            pass_rule="提交材料能追溯到版本并支持所述结论",
                            fail_rule="材料明确显示条件不满足",
                            unknown_rule="缺少材料或贡献不明时保持未知",
                            critical=True,
                            allowed_methods=["user_reported", "human_review"],
                            required_artifact_types=[artifact],
                            limitations=["静态材料不能证明本人贡献或实际运行"],
                        )
                    )
                    for dimension, behavior, artifact in [
                        ("artifact", "提供实现与正常、失败场景记录", "execution_record"),
                        ("explanation", "解释实现机制及失败边界", "explanation_answer"),
                        ("transfer", "在本人项目中完成有差异的应用", "transfer_note"),
                    ]
                ]
                value = ObjectiveInput(
                    goal_version_id=target.id,
                    title=f"在本人项目验证 {concept}",
                    target_behavior=(
                        f"针对岗位原文：{fact['evidence']['quote']}，"
                        f"完成 {concept} 的一个可验证增量"
                    ),
                    scope="固定项目与版本；具体范围和工时待本人确认",
                    deliverables=["版本化实现", "实验记录", "机制解释"],
                    criteria=criteria,
                    scope_confirmed=True,
                )
                root = LearningObjective(owner_id=owner, goal_id=target.goal_id)
                db.add(root)
                db.flush()
                version = persist_objective(db, owner, value, root, "job_contract", [])
                basis = {
                    "planning_context_id": context["planning_context_id"],
                    "demand_report_id": context["demand_report_id"],
                    "demand_ids": [demand["demand_id"]],
                    "requirement_ids": [fact_id],
                    "selected_alternatives": decisions,
                    "personal_evidence_version_ids": context["personal_evidence_version_ids"],
                }
                version.payload = {
                    **{k: v for k, v in version.payload.items() if k != "scope_confirmed"},
                    "planning_basis": basis,
                }
                version.content_hash = fingerprint(version.payload)
                objective_ids.append(version.id)
                alignments.append(
                    {
                        "requirement_ids": [fact_id],
                        "evidence_version_ids": context["personal_evidence_version_ids"],
                        "category": "unknown",
                        "reason": "静态材料尚不能证明本人能力；保留待确认。",
                        "unknown_questions": ["本人实现范围与运行证据是什么？"],
                        "objective_version_ids": [str(version.id)],
                        "selected_alternatives": decisions,
                    }
                )
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
        objective_ids,
        "draft",
        {
            "user_constraints": data["user_constraints"],
            "schedule_status": "unconfirmed",
            "deferred": [],
            "planning_context_id": context["planning_context_id"],
            "eligibility_checks": context["eligibility_checks"],
            "warnings": ["资格条件待本人确认；工时和排期待编辑。"],
        },
        alignment.id,
    )
    alignment.payload = {
        "input_manifest": data,
        "mode": "job_contract_offline",
        "synthetic_model": False,
        "alignments": alignments,
        "eligibility_checks": context["eligibility_checks"],
        "objective_version_ids": [str(i) for i in objective_ids],
        "plan_id": str(plan.id),
        "plan_version_id": str(plan_version.id),
    }
    return {
        "alignment_report_id": str(alignment.id),
        "plan_id": str(plan.id),
        "plan_version_id": str(plan_version.id),
        "_run_status": "succeeded" if objective_ids else "failed",
    }
