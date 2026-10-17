from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application.attachment_service import available
from careerforge.application.common import check_revision, fingerprint, idempotency
from careerforge.application.goal_service import get_goal
from careerforge.application.market_report_service import read_report
from careerforge.application.ownership import owned
from careerforge.application.run_service import create_run, db_now
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.models import (
    AcceptanceCriterionVersion,
    AlignmentReport,
    ArtifactDependency,
    CareerGoalVersion,
    LearningObjective,
    LearningObjectiveVersion,
    LearningPlan,
    LearningPlanVersion,
    PersonalEvidence,
    PersonalEvidenceVersion,
    PlanMembership,
)
from careerforge.schemas.planning import (
    AlignmentInput,
    EvidenceInput,
    EvidenceRevision,
    ObjectiveInput,
    ObjectiveRevision,
    PlanAdoption,
    PlanObjectiveRevision,
    PlanRevision,
    UserConstraints,
)

VERSION = "alignment-offline-v1"


def evidence_version(db: Session, owner: UUID, identifier: UUID) -> PersonalEvidenceVersion:
    row = owned(db, PersonalEvidenceVersion, owner, identifier)
    owned(db, PersonalEvidence, owner, row.evidence_id)
    if row.payload.get("public_repository_snapshot_id"):
        from careerforge.application.repository_service import snapshot

        snapshot(db, owner, UUID(row.payload["public_repository_snapshot_id"]))
    for attachment_id in row.payload["attachment_ids"]:
        available(db, owner, UUID(attachment_id))
    return row


def save_evidence(
    db: Session, owner: UUID, value: EvidenceInput, key: str | None, identifier: UUID | None = None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"evidence.{identifier or 'create'}", key, value.model_dump(mode="json")
    )
    root = owned(db, PersonalEvidence, owner, identifier, lock=True) if identifier else None
    if value.public_repository_snapshot_id is not None:
        from careerforge.application.repository_service import snapshot as repository_snapshot

        repository_snapshot(db, owner, value.public_repository_snapshot_id)
    attachments = [available(db, owner, i, lock=True) for i in value.attachment_ids]
    if len(set(value.attachment_ids)) != len(value.attachment_ids):
        raise BusinessError(422, "DUPLICATE_ATTACHMENT", "附件不可重复。")
    if record.response_summary:
        evidence_version(db, owner, UUID(record.response_summary["evidence_version_id"]))
        return record.response_summary
    if root:
        assert isinstance(value, EvidenceRevision)
        check_revision(root.revision, value.expected_version)
        root.revision += 1
    else:
        root = PersonalEvidence(owner_id=owner)
        db.add(root)
        db.flush()
    for a in attachments:
        if (
            a.purpose != "personal_evidence"
            or a.source_id is not None
            or a.evidence_id not in {None, root.id}
        ):
            raise BusinessError(409, "ATTACHMENT_ROOT_CONFLICT", "请为此个人材料重新上传附件。")
        a.evidence_id = root.id
    payload = value.model_dump(mode="json", exclude={"expected_version"})
    payload["verification_scope"] = "attached_material" if attachments else "self_reported"
    payload["runtime_verified"] = False
    row = PersonalEvidenceVersion(
        evidence_id=root.id,
        owner_id=owner,
        version_no=root.revision,
        payload=payload,
        content_hash=fingerprint(payload),
    )
    db.add(row)
    db.flush()
    if identifier:
        for dep in db.scalars(
            select(ArtifactDependency).where(
                ArtifactDependency.owner_id == owner,
                ArtifactDependency.input_kind == "evidence",
                ArtifactDependency.input_id == identifier,
            )
        ):
            dep.needs_review = True
    result = {
        "evidence_id": str(root.id),
        "evidence_version_id": str(row.id),
        "verification_scope": payload["verification_scope"],
        "version": root.revision,
    }
    record.response_summary = result
    return result


def revoke_evidence(
    db: Session, owner: UUID, identifier: UUID, expected: int, key: str | None
) -> dict[str, Any]:
    from careerforge.infrastructure.db.models import Attachment

    record = idempotency(
        db, owner, f"evidence.{identifier}.delete", key, {"expected_version": expected}
    )
    if record.response_summary:
        return record.response_summary
    root = owned(db, PersonalEvidence, owner, identifier, lock=True)
    check_revision(root.revision, expected)
    root.revoked_at = db_now(db)
    root.revision += 1
    for a in db.scalars(select(Attachment).where(Attachment.evidence_id == root.id)):
        a.revoked_at = root.revoked_at
        a.revision += 1
    # S08 consumes the maintenance record for physical erasure.
    from careerforge.infrastructure.db.models import EvidenceDeletion

    deletion = EvidenceDeletion(owner_id=owner, evidence_id=identifier)
    db.add(deletion)
    db.flush()
    record.response_summary = {
        "deletion_id": str(deletion.id),
        "status": "revoked",
        "version": root.revision,
    }
    return record.response_summary


def objective_version(db: Session, owner: UUID, identifier: UUID) -> LearningObjectiveVersion:
    row = owned(db, LearningObjectiveVersion, owner, identifier)
    if row.payload.get("study_basis"):
        from careerforge.application.study_practice_service import spec_current

        basis = row.payload["study_basis"]
        spec = spec_current(db, owner, basis["exercise_spec_id"])
        if spec.payload["content_hash"] != basis["content_hash"]:
            raise BusinessError(409, "EXERCISE_SOURCE_CHANGED", "练习定义已变化。")
    if row.payload.get("planning_basis"):
        from careerforge.application.job_contract_service import read

        read(db, owner, UUID(row.payload["planning_basis"]["planning_context_id"]), "context")
    owned(db, LearningObjective, owner, row.objective_id)
    from careerforge.application.source_service import get_snapshot
    from careerforge.infrastructure.db.models import (
        JobPostingVersion,
        Requirement,
        RequirementCollection,
    )

    for requirement_id in row.payload.get("requirement_ids", []):
        requirement = db.get(Requirement, UUID(requirement_id))
        if requirement is None:
            from careerforge.domain.errors import not_found

            raise not_found()
        collection = db.get(RequirementCollection, requirement.collection_id)
        assert collection is not None
        posting = owned(db, JobPostingVersion, owner, collection.posting_version_id)
        get_snapshot(db, owner, posting.source_id, posting.snapshot_id)
    return row


def criteria(db: Session, owner: UUID, identifier: UUID) -> list[AcceptanceCriterionVersion]:
    objective_version(db, owner, identifier)
    return list(
        db.scalars(
            select(AcceptanceCriterionVersion)
            .where(
                AcceptanceCriterionVersion.objective_version_id == identifier,
                AcceptanceCriterionVersion.owner_id == owner,
            )
            .order_by(AcceptanceCriterionVersion.id)
        )
    )


def exact_ids(actual: list[UUID], submitted: list[UUID]) -> None:
    if len(set(submitted)) != len(submitted) or set(actual) != set(submitted):
        raise BusinessError(
            422, "CRITERIA_SET_MISMATCH", "必须使用服务端完整条件集合，不可省略或替换。"
        )


def latest_objective(db: Session, root: LearningObjective) -> LearningObjectiveVersion:
    row = db.scalar(
        select(LearningObjectiveVersion)
        .where(LearningObjectiveVersion.objective_id == root.id)
        .order_by(LearningObjectiveVersion.version_no.desc())
        .limit(1)
    )
    assert row is not None
    return row


def validate_prerequisites(
    db: Session, owner: UUID, goal_id: UUID, identifier: UUID, requested: list[UUID]
) -> None:
    if len(set(requested)) != len(requested):
        raise BusinessError(422, "DUPLICATE_PREREQUISITE", "先修不可重复。")
    seen: set[UUID] = set()

    def visit(current: UUID) -> None:
        if current == identifier:
            raise BusinessError(422, "PREREQUISITE_CYCLE", "先修依赖包含循环。")
        if current in seen:
            return
        seen.add(current)
        root = owned(db, LearningObjective, owner, current)
        if root.goal_id != goal_id:
            raise BusinessError(422, "PREREQUISITE_GOAL_MISMATCH", "先修须属于同一职业目标。")
        for item in latest_objective(db, root).payload["prerequisite_objective_ids"]:
            visit(UUID(item))

    for item in requested:
        visit(item)


def persist_objective(
    db: Session,
    owner: UUID,
    value: ObjectiveInput,
    root: LearningObjective,
    origin: str,
    requirements: list[str],
) -> LearningObjectiveVersion:
    validate_prerequisites(db, owner, root.goal_id, root.id, value.prerequisite_objective_ids)
    payload = value.model_dump(mode="json", exclude={"criteria", "expected_version"})
    payload.update(origin=origin, requirement_ids=requirements)
    version = LearningObjectiveVersion(
        objective_id=root.id,
        owner_id=owner,
        goal_version_id=value.goal_version_id,
        version_no=root.revision,
        payload=payload,
        content_hash=fingerprint(payload),
    )
    db.add(version)
    db.flush()
    for criterion in value.criteria:
        db.add(
            AcceptanceCriterionVersion(
                owner_id=owner,
                objective_version_id=version.id,
                payload=criterion.model_dump(mode="json"),
            )
        )
    db.flush()
    return version


def objective_result(db: Session, owner: UUID, row: LearningObjectiveVersion) -> dict[str, Any]:
    root = owned(db, LearningObjective, owner, row.objective_id)
    cs = criteria(db, owner, row.id)
    return {
        **row.payload,
        "objective_id": str(root.id),
        "objective_version_id": str(row.id),
        "goal_version_id": str(row.goal_version_id),
        "version": root.revision,
        "version_no": row.version_no,
        "criterion_version_ids": [str(c.id) for c in cs],
        "criteria": [{"criterion_version_id": str(c.id), **c.payload} for c in cs],
    }


def save_objective(
    db: Session, owner: UUID, value: ObjectiveInput, key: str | None, identifier: UUID | None = None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"objectives.{identifier or 'create'}", key, value.model_dump(mode="json")
    )
    goal = owned(db, CareerGoalVersion, owner, value.goal_version_id)
    get_goal(db, owner, goal.goal_id, lock=True)
    root = owned(db, LearningObjective, owner, identifier, lock=True) if identifier else None
    if root and root.goal_id != goal.goal_id:
        raise BusinessError(422, "OBJECTIVE_GOAL_MISMATCH", "不可修改目标归属。")
    if record.response_summary:
        objective_version(db, owner, UUID(record.response_summary["objective_version_id"]))
        return record.response_summary
    origin, requirements = "user_goal", []
    if root:
        assert isinstance(value, ObjectiveRevision)
        check_revision(root.revision, value.expected_version)
        previous = latest_objective(db, root)
        origin, requirements = previous.payload["origin"], previous.payload["requirement_ids"]
        root.revision += 1
    else:
        root = LearningObjective(owner_id=owner, goal_id=goal.goal_id)
        db.add(root)
        db.flush()
    row = persist_objective(db, owner, value, root, origin, requirements)
    if identifier and previous.payload.get("planning_basis"):
        objective_version(db, owner, previous.id)
        row.payload = {**row.payload, "planning_basis": previous.payload["planning_basis"]}
        row.content_hash = fingerprint(row.payload)
    result = objective_result(db, owner, row)
    record.response_summary = result
    return result


def members(db: Session, row: LearningPlanVersion) -> list[UUID]:
    return list(
        db.scalars(
            select(PlanMembership.objective_version_id)
            .where(PlanMembership.plan_version_id == row.id)
            .order_by(PlanMembership.position)
        )
    )


def validate_members(
    db: Session, owner: UUID, goal_version_id: UUID, ids: list[UUID], constraints: dict[str, Any]
) -> None:
    if len(ids) != len(set(ids)) or not ids:
        raise BusinessError(422, "INVALID_PLAN_MEMBERS", "计划目标集合为空或重复。")
    versions = [objective_version(db, owner, i) for i in ids]
    if any(v.goal_version_id != goal_version_id for v in versions):
        raise BusinessError(409, "PLAN_GOAL_VERSION_MISMATCH", "目标版本与计划职业目标不一致。")
    by_root = {v.objective_id: n for n, v in enumerate(versions)}
    if len(by_root) != len(versions):
        raise BusinessError(422, "DUPLICATE_OBJECTIVE", "同一目标不可选两个版本。")
    for n, v in enumerate(versions):
        for prerequisite in v.payload["prerequisite_objective_ids"]:
            index = by_root.get(UUID(prerequisite))
            if index is None or index >= n:
                raise BusinessError(
                    422, "MISSING_PREREQUISITE", "请包含必需先修，并排列在依赖目标之前。"
                )
    budget = constraints.get("available_hours")
    if budget is not None:
        if any(v.payload.get("effort_hours") is None for v in versions):
            raise BusinessError(422, "EFFORT_UNKNOWN", "给定时间预算时需补全目标工时区间。")
        total = sum(v.payload["effort_hours"]["max"] for v in versions)
        if total > budget:
            raise BusinessError(
                422, "PLAN_TIME_EXCEEDED", "目标超出可用工时，请明确暂缓或缩小范围。"
            )


def new_plan_version(
    db: Session,
    root: LearningPlan,
    goal_id: UUID,
    ids: list[UUID],
    kind: str,
    payload: dict[str, Any],
    alignment_id: UUID | None = None,
) -> LearningPlanVersion:
    row = LearningPlanVersion(
        plan_id=root.id,
        owner_id=root.owner_id,
        goal_version_id=goal_id,
        version_no=root.revision,
        kind=kind,
        payload=payload,
        alignment_report_id=alignment_id,
    )
    db.add(row)
    db.flush()
    for n, i in enumerate(ids):
        db.add(
            PlanMembership(
                plan_version_id=row.id, objective_version_id=i, owner_id=root.owner_id, position=n
            )
        )
    db.flush()
    return row


def plan_result(db: Session, owner: UUID, row: LearningPlanVersion) -> dict[str, Any]:
    owned(db, LearningPlanVersion, owner, row.id)
    if row.alignment_report_id:
        alignment_report(db, owner, row.alignment_report_id)
    root = owned(db, LearningPlan, owner, row.plan_id)
    from careerforge.application.studio_delivery_service import effective_outcome

    outcomes = {
        k: effective_outcome(db, owner, v)
        for k, v in row.payload.get("practice_outcomes", {}).items()
    }
    return {
        **row.payload,
        "practice_outcomes": outcomes,
        "plan_id": str(root.id),
        "plan_version_id": str(row.id),
        "kind": row.kind,
        "version": root.revision,
        "version_no": row.version_no,
        "goal_version_id": str(row.goal_version_id),
        "active_plan_version_id": str(root.active_version_id) if root.active_version_id else None,
        "objective_version_ids": [str(i) for i in members(db, row)],
        "objectives": [
            objective_result(db, owner, objective_version(db, owner, i)) for i in members(db, row)
        ],
    }


def current_goal(db: Session, owner: UUID, version: CareerGoalVersion) -> None:
    root = get_goal(db, owner, version.goal_id, lock=True)
    if root.revision != version.version_no:
        raise BusinessError(409, "GOAL_VERSION_CHANGED", "请以当前职业目标版本重新规划。")


def edit_plan(
    db: Session, owner: UUID, identifier: UUID, body: PlanRevision, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"plans.{identifier}.versions", key, body.model_dump(mode="json")
    )
    source = owned(db, LearningPlanVersion, owner, body.base_plan_version_id)
    if source.plan_id != identifier:
        raise BusinessError(422, "PLAN_VERSION_MISMATCH", "计划版本不属于此计划。")
    target = owned(db, CareerGoalVersion, owner, source.goal_version_id)
    get_goal(db, owner, target.goal_id, lock=True)
    root = owned(db, LearningPlan, owner, identifier, lock=True)
    if record.response_summary:
        return record.response_summary
    check_revision(root.revision, body.expected_version)
    constraints = body.user_constraints.model_dump()
    validate_members(db, owner, target.id, body.objective_version_ids, constraints)
    root.revision += 1
    row = new_plan_version(
        db,
        root,
        target.id,
        body.objective_version_ids,
        "draft",
        {
            **{
                k: v
                for k, v in source.payload.items()
                if k
                in {"planning_context_id", "eligibility_checks", "warnings", "practice_outcomes"}
            },
            "adjustment_reason": body.adjustment_reason,
            "deferred": body.deferred,
            "user_constraints": constraints,
            "schedule_status": "unconfirmed",
        },
        source.alignment_report_id,
    )
    record.response_summary = plan_result(db, owner, row)
    return record.response_summary


def adopt(
    db: Session, owner: UUID, identifier: UUID, body: PlanAdoption, key: str | None
) -> dict[str, Any]:
    record = idempotency(db, owner, f"plans.{identifier}.adopt", key, body.model_dump(mode="json"))
    target = owned(db, CareerGoalVersion, owner, body.expected_goal_version_id)
    # All writers use the same lock order, including creation of the stable plan.
    get_goal(db, owner, target.goal_id, lock=True)
    root = owned(db, LearningPlan, owner, identifier, lock=True)
    source = owned(db, LearningPlanVersion, owner, body.plan_version_id)
    if (
        source.plan_id != root.id
        or root.goal_id != target.goal_id
        or source.goal_version_id != target.id
    ):
        raise BusinessError(409, "PLAN_GOAL_VERSION_MISMATCH", "计划、源草稿和职业目标版本不一致。")
    if record.response_summary:
        return record.response_summary
    current_goal(db, owner, target)
    check_revision(root.revision, body.expected_version)
    if source.kind != "draft":
        raise BusinessError(422, "DRAFT_REQUIRED", "请选择源草稿，采纳结果不能冒充草稿。")
    if not set(body.selected_objective_version_ids).issubset(members(db, source)):
        raise BusinessError(422, "SELECTION_OUTSIDE_DRAFT", "选中目标不属于源草稿。")
    validate_members(
        db,
        owner,
        target.id,
        body.selected_objective_version_ids,
        source.payload.get("user_constraints", {}),
    )
    if source.alignment_report_id:
        alignment_report(db, owner, source.alignment_report_id)
    root.revision += 1
    row = new_plan_version(
        db,
        root,
        target.id,
        body.selected_objective_version_ids,
        "adopted",
        {**source.payload, "source_plan_version_id": str(source.id)},
        source.alignment_report_id,
    )
    root.active_version_id = row.id
    result = {
        "plan_id": str(root.id),
        "source_plan_version_id": str(source.id),
        "active_plan_version_id": str(row.id),
        "selected_objective_version_ids": [str(i) for i in body.selected_objective_version_ids],
        "version": root.revision,
    }
    record.response_summary = result
    return result


def plan_diff(
    db: Session, owner: UUID, identifier: UUID, before: UUID, after: UUID
) -> dict[str, Any]:
    rows = [owned(db, LearningPlanVersion, owner, i) for i in [before, after]]
    if any(r.plan_id != identifier for r in rows):
        raise BusinessError(422, "PLAN_VERSION_MISMATCH", "版本不属于此计划。")
    maps = [{objective_version(db, owner, i).objective_id: i for i in members(db, r)} for r in rows]
    a, b = maps
    return {
        "retained": [str(i) for i in a.keys() & b.keys() if a[i] == b[i]],
        "adjusted": [str(i) for i in a.keys() & b.keys() if a[i] != b[i]],
        "removed": [str(i) for i in a.keys() - b.keys()],
        "added": [str(i) for i in b.keys() - a.keys()],
    }


def start_alignment(
    db: Session, owner: UUID, body: AlignmentInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(db, owner, "alignment.create", key, body.model_dump(mode="json"))
    data = body.model_dump(mode="json")
    market = read_report(db, owner, body.market_report_id)
    if market.payload.get("demand_report_id"):
        from uuid import NAMESPACE_URL, uuid5

        from careerforge.application.job_contract_service import create_context
        from careerforge.schemas.job_contract import ContextInput

        context = create_context(
            db,
            owner,
            ContextInput(
                goal_version_id=str(body.goal_version_id),
                demand_report_id=market.payload["demand_report_id"],
                personal_evidence_version_ids=data["personal_evidence_version_ids"],
            ),
            str(uuid5(NAMESPACE_URL, f"alignment-context:{owner}:{key}")),
        )
        data["planning_context_id"] = context["contract_artifact_id"]
    validate_alignment(db, owner, data)
    if record.response_summary:
        return record.response_summary
    run = create_run(
        db,
        owner,
        "alignment",
        VERSION,
        data,
        call_budget=0,
        goal_version_id=body.goal_version_id,
    )
    record.response_summary = {"run_id": str(run.id), "status": run.status, "version": run.version}
    return record.response_summary


def validate_alignment(db: Session, owner: UUID, data: dict[str, Any]) -> None:
    if data.get("planning_context_id"):
        from careerforge.application.job_contract_service import read

        context = read(db, owner, UUID(data["planning_context_id"]), "context").payload["context"]
        if (
            context["goal_version_id"] != data["goal_version_id"]
            or context["personal_evidence_version_ids"] != data["personal_evidence_version_ids"]
        ):
            raise BusinessError(409, "CONTEXT_INPUT_MISMATCH", "规划输入与冻结上下文不一致。")
    target = owned(db, CareerGoalVersion, owner, UUID(data["goal_version_id"]))
    report = read_report(db, owner, UUID(data["market_report_id"]))
    if report.goal_version_id != target.id:
        raise BusinessError(409, "REPORT_GOAL_MISMATCH", "岗位报告与目标版本不一致。")
    for i in data["personal_evidence_version_ids"]:
        evidence_version(db, owner, UUID(i))
    if len(set(data["personal_evidence_version_ids"])) != len(
        data["personal_evidence_version_ids"]
    ):
        raise BusinessError(422, "DUPLICATE_EVIDENCE", "个人证据版本不可重复。")
    if data.get("previous_plan_version_id"):
        previous = owned(db, LearningPlanVersion, owner, UUID(data["previous_plan_version_id"]))
        root = owned(db, LearningPlan, owner, previous.plan_id)
        if root.goal_id != target.goal_id:
            raise BusinessError(422, "PLAN_GOAL_MISMATCH", "前版计划须属于同一职业目标。")
    for i in data.get("assessment_ids", []):
        from careerforge.application.build_service import assessment_detail

        assessment_detail(db, owner, UUID(i))


def alignment_report(db: Session, owner: UUID, identifier: UUID) -> AlignmentReport:
    row = owned(db, AlignmentReport, owner, identifier)
    validate_alignment(db, owner, row.payload["input_manifest"])
    return row


def edit_plan_objective(
    db: Session,
    owner: UUID,
    identifier: UUID,
    objective_id: UUID,
    body: "PlanObjectiveRevision",
    key: str | None,
) -> dict[str, Any]:
    """Save the objective revision and replace its plan membership in one transaction."""
    from uuid import NAMESPACE_URL, uuid5

    record = idempotency(
        db,
        owner,
        f"plans.{identifier}.objective.{objective_id}",
        key,
        body.model_dump(mode="json"),
    )
    source = owned(db, LearningPlanVersion, owner, body.base_plan_version_id)
    target = owned(db, CareerGoalVersion, owner, source.goal_version_id)
    get_goal(db, owner, target.goal_id, lock=True)
    root = owned(db, LearningPlan, owner, identifier, lock=True)
    if source.plan_id != identifier or body.objective.goal_version_id != target.id:
        raise BusinessError(422, "PLAN_GOAL_MISMATCH", "目标和计划必须属于同一职业目标版本。")
    if record.response_summary:
        fixed = owned(
            db, LearningPlanVersion, owner, UUID(record.response_summary["plan_version_id"])
        )
        return plan_result(db, owner, fixed)
    check_revision(root.revision, body.expected_plan_version)
    current_ids = members(db, source)
    replaced_ids = [
        i for i in current_ids if objective_version(db, owner, i).objective_id == objective_id
    ]
    if len(replaced_ids) != 1:
        raise BusinessError(422, "OBJECTIVE_NOT_IN_PLAN", "请编辑当前计划内的目标。")
    # Independent operation keys retain safe retries without exceeding the key length limit.
    objective_key = str(uuid5(NAMESPACE_URL, f"{identifier}:{objective_id}:{key}:objective"))
    plan_key = str(uuid5(NAMESPACE_URL, f"{identifier}:{objective_id}:{key}:plan"))
    revised = save_objective(db, owner, body.objective, objective_key, objective_id)
    new_id = UUID(revised["objective_version_id"])
    ids = [new_id if i == replaced_ids[0] else i for i in current_ids]
    result = edit_plan(
        db,
        owner,
        identifier,
        PlanRevision(
            expected_version=body.expected_plan_version,
            base_plan_version_id=source.id,
            objective_version_ids=ids,
            adjustment_reason=body.objective.change_reason,
            deferred=source.payload.get("deferred", []),
            user_constraints=UserConstraints.model_validate(
                source.payload.get("user_constraints", {})
            ),
        ),
        plan_key,
    )
    record.response_summary = result
    return result
