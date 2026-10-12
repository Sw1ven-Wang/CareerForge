from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from careerforge.application import attachment_service
from careerforge.application.common import check_revision, fingerprint, idempotency
from careerforge.application.learning_service import unit
from careerforge.application.ownership import owned
from careerforge.application.planning_service import (
    criteria,
    current_goal,
    evidence_version,
    exact_ids,
    objective_version,
)
from careerforge.application.run_service import create_run, db_now
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.models import (
    Assessment,
    AssessmentConfirmation,
    AssessmentReview,
    BuildProject,
    BuildTask,
    BuildTaskVersion,
    CareerGoalVersion,
    LearningObjective,
    LearningPlan,
    PlanDeltaProposal,
    PlanMembership,
    Submission,
    TaskDraft,
)
from careerforge.schemas.builds import (
    AssessmentInput,
    ConfirmationInput,
    ProjectInput,
    RevocationInput,
    SubmissionInput,
    TaskInput,
    TaskRevision,
    TransitionInput,
)

VERSION = "assessment-offline-v1"
EDITABLE = {"todo", "in_progress", "submitted", "needs_revision"}


def project(db: Session, owner: UUID, identifier: UUID) -> BuildProject:
    row = owned(db, BuildProject, owner, identifier)
    for i in row.payload["evidence_refs"]:
        evidence_version(db, owner, UUID(i))
    return row


def create_project(db: Session, owner: UUID, body: ProjectInput, key: str | None) -> dict[str, Any]:
    record = idempotency(db, owner, "build_project.create", key, body.model_dump(mode="json"))
    for i in body.evidence_refs:
        evidence_version(db, owner, i)
    if record.response_summary:
        project(db, owner, UUID(record.response_summary["build_project_id"]))
        return record.response_summary
    row = BuildProject(owner_id=owner, payload=body.model_dump(mode="json"))
    db.add(row)
    db.flush()
    record.response_summary = {"build_project_id": str(row.id), **row.payload}
    return record.response_summary


def task(db: Session, owner: UUID, identifier: UUID, lock: bool = False) -> BuildTask:
    row = owned(db, BuildTask, owner, identifier, lock=lock)
    project(db, owner, row.project_id)
    return row


def task_version(db: Session, owner: UUID, identifier: UUID | None) -> BuildTaskVersion:
    if identifier is None:
        raise BusinessError(409, "TASK_VERSION_MISSING", "任务尚未建立内容版本。")
    row = owned(db, BuildTaskVersion, owner, identifier)
    task(db, owner, row.task_id)
    objective_version(db, owner, row.objective_version_id)
    return row


def frozen_criteria(db: Session, owner: UUID, version: BuildTaskVersion) -> list[Any]:
    values = criteria(db, owner, version.objective_version_id)
    exact_ids([c.id for c in values], [UUID(i) for i in version.payload["criterion_version_ids"]])
    return values


def validate_task(
    db: Session, owner: UUID, project_id: UUID, body: TaskInput, identifier: UUID | None = None
) -> list[str]:
    obj = objective_version(db, owner, body.objective_version_id)
    target = owned(db, CareerGoalVersion, owner, obj.goal_version_id)
    root = owned(db, LearningObjective, owner, obj.objective_id)
    plan = db.scalar(
        select(LearningPlan).where(
            LearningPlan.owner_id == owner, LearningPlan.goal_id == target.goal_id
        )
    )
    adopted = (
        plan is not None
        and plan.active_version_id is not None
        and db.get(PlanMembership, (plan.active_version_id, obj.id)) is not None
    )
    if not adopted:
        current_goal(db, owner, target)
        if (
            obj.payload["origin"] != "user_goal"
            or root.revision != obj.version_no
            or not obj.payload.get("scope_confirmed")
        ):
            raise BusinessError(
                409,
                "OBJECTIVE_NOT_EXECUTABLE",
                "须先采纳目标，或选择当前已明确确认范围的手工目标。",
            )
    values = criteria(db, owner, obj.id)
    exact_ids([c.id for c in values], body.criterion_version_ids)
    if body.task_draft_id:
        draft = owned(db, TaskDraft, owner, body.task_draft_id)
        assert body.learning_unit_id is not None
        learning = unit(db, owner, body.learning_unit_id)
        if (
            draft.unit_id != learning.id
            or draft.objective_version_id != obj.id
            or learning.objective_version_id != obj.id
        ):
            raise BusinessError(422, "DRAFT_SCOPE_MISMATCH", "草稿、单元与目标必须关联。")
        exact_ids([c.id for c in values], [UUID(i) for i in draft.payload["criterion_version_ids"]])
    if len(set(body.prerequisite_task_ids)) != len(body.prerequisite_task_ids):
        raise BusinessError(422, "DUPLICATE_PREREQUISITE", "依赖任务不可重复。")
    seen: set[UUID] = set()

    def visit(i: UUID) -> None:
        if i == identifier:
            raise BusinessError(422, "TASK_DEPENDENCY_CYCLE", "任务依赖包含循环。")
        if i in seen:
            return
        seen.add(i)
        dep = task(db, owner, i)
        if dep.project_id != project_id or dep.status == "cancelled":
            raise BusinessError(422, "TASK_DEPENDENCY_SCOPE", "依赖须为同项目的有效任务。")
        version = task_version(db, owner, dep.current_task_version_id)
        for child in version.payload["prerequisite_task_ids"]:
            visit(UUID(child))

    for i in body.prerequisite_task_ids:
        visit(i)
    return [str(c.id) for c in values]


def task_detail(db: Session, owner: UUID, identifier: UUID) -> dict[str, Any]:
    row = task(db, owner, identifier)
    version = task_version(db, owner, row.current_task_version_id)
    history = list(
        db.scalars(
            select(BuildTaskVersion)
            .where(BuildTaskVersion.task_id == row.id)
            .order_by(BuildTaskVersion.version_no)
        )
    )
    submissions = list(
        db.scalars(
            select(Submission).where(Submission.task_id == row.id).order_by(Submission.version_no)
        )
    )
    return {
        "task_id": str(row.id),
        "build_project_id": str(row.project_id),
        "status": row.status,
        "version": row.revision,
        "current_task_version_id": str(version.id),
        "current_submission_id": str(row.current_submission_id)
        if row.current_submission_id
        else None,
        "effective_confirmation_id": str(row.effective_confirmation_id)
        if row.effective_confirmation_id
        else None,
        "objective_version_id": str(version.objective_version_id),
        **version.payload,
        "criteria": [
            {"criterion_version_id": str(c.id), **c.payload}
            for c in frozen_criteria(db, owner, version)
        ],
        "versions": [{"task_version_id": str(v.id), "version_no": v.version_no} for v in history],
        "submissions": [
            {"submission_id": str(s.id), "submission_version": s.version_no} for s in submissions
        ],
    }


def create_task(
    db: Session, owner: UUID, project_id: UUID, body: TaskInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"project.{project_id}.tasks", key, body.model_dump(mode="json")
    )
    owned(
        db, BuildProject, owner, project_id, lock=True
    )  # Serializes same-project drafts/dependency edits.
    project(db, owner, project_id)
    full = validate_task(db, owner, project_id, body)
    if record.response_summary:
        task(db, owner, UUID(record.response_summary["task_id"]))
        return record.response_summary
    if body.task_draft_id:
        existing = db.scalar(
            select(BuildTask).where(
                BuildTask.project_id == project_id, BuildTask.task_draft_id == body.task_draft_id
            )
        )
        if existing:
            original = db.scalar(
                select(BuildTaskVersion).where(
                    BuildTaskVersion.task_id == existing.id, BuildTaskVersion.version_no == 1
                )
            )
            assert original is not None
            if fingerprint(original.payload["creation_input"]) != fingerprint(
                body.model_dump(mode="json")
            ):
                raise BusinessError(409, "TASK_DRAFT_CONFLICT", "此草稿已物化为不同内容的任务。")
            record.response_summary = task_detail(db, owner, existing.id)
            return record.response_summary
    row = BuildTask(owner_id=owner, project_id=project_id, task_draft_id=body.task_draft_id)
    db.add(row)
    db.flush()
    version = BuildTaskVersion(
        owner_id=owner,
        task_id=row.id,
        version_no=1,
        objective_version_id=body.objective_version_id,
        payload={
            **body.model_dump(mode="json"),
            "criterion_version_ids": full,
            "creation_input": body.model_dump(mode="json"),
        },
    )
    db.add(version)
    db.flush()
    row.current_task_version_id = version.id
    record.response_summary = task_detail(db, owner, row.id)
    return record.response_summary


def revise_task(
    db: Session, owner: UUID, identifier: UUID, body: TaskRevision, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"task.{identifier}.versions", key, body.model_dump(mode="json")
    )
    initial = task(db, owner, identifier)
    owned(db, BuildProject, owner, initial.project_id, lock=True)
    row = task(db, owner, identifier, lock=True)
    if record.response_summary:
        return record.response_summary
    check_revision(row.revision, body.expected_version)
    if row.status not in EDITABLE:
        raise BusinessError(409, "TASK_NOT_EDITABLE", "已验收须先撤销；已取消任务不可改版。")
    full = validate_task(db, owner, row.project_id, body, row.id)
    old = task_version(db, owner, row.current_task_version_id)
    version = BuildTaskVersion(
        owner_id=owner,
        task_id=row.id,
        version_no=old.version_no + 1,
        objective_version_id=body.objective_version_id,
        payload={
            **body.model_dump(mode="json", exclude={"expected_version"}),
            "criterion_version_ids": full,
        },
    )
    db.add(version)
    db.flush()
    row.current_task_version_id = version.id
    row.current_submission_id = None
    row.status = "todo" if row.status == "todo" else "needs_revision"
    row.revision += 1
    record.response_summary = task_detail(db, owner, identifier)
    return record.response_summary


def dependencies_ready(db: Session, owner: UUID, version: BuildTaskVersion) -> None:
    for i in version.payload["prerequisite_task_ids"]:
        if task(db, owner, UUID(i), lock=True).status != "accepted":
            raise BusinessError(409, "PREREQUISITES_INCOMPLETE", "前置任务尚未验收。")


def transition_task(
    db: Session, owner: UUID, identifier: UUID, body: TransitionInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"task.{identifier}.transition", key, body.model_dump(mode="json")
    )
    initial = task(db, owner, identifier)
    owned(db, BuildProject, owner, initial.project_id, lock=True)
    row = task(db, owner, identifier, lock=True)
    if record.response_summary:
        return record.response_summary
    check_revision(row.revision, body.expected_version)
    if row.status not in EDITABLE or (
        body.target_status == "in_progress" and row.status not in {"todo", "needs_revision"}
    ):
        raise BusinessError(409, "INVALID_TASK_TRANSITION", "当前任务不允许此状态变更。")
    if body.target_status == "in_progress":
        dependencies_ready(db, owner, task_version(db, owner, row.current_task_version_id))
    row.status = body.target_status
    row.revision += 1
    record.response_summary = task_detail(db, owner, identifier)
    return record.response_summary


def submission(db: Session, owner: UUID, identifier: UUID) -> Submission:
    row = owned(db, Submission, owner, identifier)
    version = task_version(db, owner, row.task_version_id)
    if version.task_id != row.task_id:
        raise BusinessError(409, "SUBMISSION_VERSION_MISMATCH", "提交版本关系无效。")
    frozen_criteria(db, owner, version)
    for i in row.payload["attachment_ids"]:
        attachment_service.available(db, owner, UUID(i))
    return row


def submit(
    db: Session, owner: UUID, identifier: UUID, body: SubmissionInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(db, owner, f"task.{identifier}.submit", key, body.model_dump(mode="json"))
    row = task(db, owner, identifier, lock=True)
    version = task_version(db, owner, body.task_version_id)
    exact_ids([c.id for c in frozen_criteria(db, owner, version)], body.criterion_version_ids)
    if record.response_summary:
        submission(db, owner, UUID(record.response_summary["submission_id"]))
        return record.response_summary
    check_revision(row.revision, body.expected_version)
    if (
        version.task_id != row.id
        or row.current_task_version_id != version.id
        or row.status not in EDITABLE
    ):
        raise BusinessError(409, "STALE_SUBMISSION", "须提交当前可修改任务版本。")
    if len(set(body.attachment_ids)) != len(body.attachment_ids):
        raise BusinessError(422, "DUPLICATE_ATTACHMENT", "附件不可重复。")
    attached = [attachment_service.available(db, owner, i, lock=True) for i in body.attachment_ids]
    if any(
        a.purpose != "submission" or a.submission_id or a.source_id or a.evidence_id
        for a in attached
    ):
        raise BusinessError(409, "ATTACHMENT_ROOT_CONFLICT", "请为本次提交独立上传附件。")
    for entry in body.execution_records:
        if entry.output_attachment_id not in body.attachment_ids:
            raise BusinessError(
                422, "EXECUTION_ATTACHMENT_REQUIRED", "执行记录须指向本次上传的输出附件。"
            )
    for answer in body.explanation_answers:
        if not set(answer.criterion_version_ids) <= set(body.criterion_version_ids):
            raise BusinessError(422, "EXPLANATION_SCOPE_MISMATCH", "解释引用了其他条件。")
    number = (
        db.scalar(select(func.max(Submission.version_no)).where(Submission.task_id == row.id)) or 0
    ) + 1
    saved = Submission(
        owner_id=owner,
        task_id=row.id,
        task_version_id=version.id,
        version_no=number,
        payload=body.model_dump(mode="json", exclude={"expected_version"}),
    )
    db.add(saved)
    db.flush()
    for a in attached:
        a.submission_id = saved.id
    row.current_submission_id = saved.id
    row.status = "submitted"
    row.revision += 1
    record.response_summary = {
        "submission_id": str(saved.id),
        "submission_version": number,
        "task_status": row.status,
        "current_task_version_id": str(version.id),
        "current_submission_id": str(saved.id),
        "version": row.revision,
    }
    return record.response_summary


def start_assessment(
    db: Session, owner: UUID, identifier: UUID, body: AssessmentInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"submission.{identifier}.assessment", key, body.model_dump(mode="json")
    )
    saved = submission(db, owner, identifier)
    version = task_version(db, owner, saved.task_version_id)
    exact_ids([c.id for c in frozen_criteria(db, owner, version)], body.criterion_version_ids)
    if record.response_summary:
        return record.response_summary
    data = {"submission_id": str(saved.id), **body.model_dump(mode="json")}
    run = create_run(
        db,
        owner,
        "assessment",
        VERSION,
        data,
        call_budget=0,
        goal_version_id=objective_version(db, owner, version.objective_version_id).goal_version_id,
    )
    record.response_summary = {"run_id": str(run.id), "version": run.version, "status": run.status}
    return record.response_summary


def assessment(db: Session, owner: UUID, identifier: UUID, lock: bool = False) -> Assessment:
    row = owned(db, Assessment, owner, identifier, lock=lock)
    submission(db, owner, row.submission_id)
    for review in db.scalars(
        select(AssessmentReview).where(AssessmentReview.assessment_id == row.id)
    ):
        for attachment_id in review.payload.get("supplementary_evidence_ids", []):
            attachment_service.available(db, owner, UUID(attachment_id))
    return row


def assessment_detail(db: Session, owner: UUID, identifier: UUID) -> dict[str, Any]:
    row = assessment(db, owner, identifier)
    confirmations = list(
        db.scalars(
            select(AssessmentConfirmation)
            .where(AssessmentConfirmation.assessment_id == row.id)
            .order_by(AssessmentConfirmation.created_at)
        )
    )
    reviews = list(
        db.scalars(
            select(AssessmentReview)
            .where(AssessmentReview.assessment_id == row.id)
            .order_by(AssessmentReview.created_at)
        )
    )
    return {
        "assessment_id": str(row.id),
        "submission_id": str(row.submission_id),
        "version": row.revision,
        "generated_by_run_id": str(row.run_id),
        **row.payload,
        "confirmation": [
            {
                "confirmation_id": str(c.id),
                "review_id": str(c.review_id),
                "decision": c.decision,
                "revoked": c.revoked_at is not None,
                "revocation_reason": c.revocation_reason,
            }
            for c in confirmations
        ],
        "reviews": [{"review_id": str(r.id), **r.payload} for r in reviews],
    }


def materials(saved: Submission, criterion_id: UUID | None = None) -> set[str]:
    data = saved.payload
    result = set()
    if data["execution_records"] and data["environment"]:
        result.add("execution_record")
    if any(
        criterion_id is None or str(criterion_id) in a["criterion_version_ids"]
        for a in data["explanation_answers"]
    ):
        result.add("explanation_answer")
    if data.get("transfer_notes"):
        result.add("transfer_note")
    if data["code_commit"] and data["attachment_ids"]:
        result.add("code_artifact")
    return result


def confirm(
    db: Session, owner: UUID, identifier: UUID, body: ConfirmationInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"assessment.{identifier}.confirm", key, body.model_dump(mode="json")
    )
    proposal = assessment(db, owner, identifier)
    saved = submission(db, owner, proposal.submission_id)
    initial = task(db, owner, saved.task_id)
    owned(db, BuildProject, owner, initial.project_id, lock=True)
    row = task(db, owner, saved.task_id, lock=True)
    proposal = assessment(db, owner, identifier, lock=True)
    version = task_version(db, owner, saved.task_version_id)
    full = frozen_criteria(db, owner, version)
    exact_ids([c.id for c in full], [d.criterion_version_id for d in body.criterion_decisions])
    if record.response_summary:
        return record.response_summary
    check_revision(proposal.revision, body.expected_version)
    check_revision(row.revision, body.expected_task_version)
    if (
        row.status != "submitted"
        or row.current_submission_id != saved.id
        or row.current_task_version_id != saved.task_version_id
    ):
        raise BusinessError(409, "STALE_ASSESSMENT", "此审阅属于旧提交；返修后须重新提交。")
    decisions = {d.criterion_version_id: d for d in body.criterion_decisions}
    supplied = set(body.supplementary_evidence_ids)
    supplementary_hashes = {}
    for i in supplied:
        attachment = attachment_service.available(db, owner, i, lock=True)
        if attachment.purpose != "submission" or attachment.submission_id not in {None, saved.id}:
            raise BusinessError(422, "EVIDENCE_SCOPE_MISMATCH", "补充证据须独立归属于本次提交。")
        attachment.submission_id = saved.id
        supplementary_hashes[str(i)] = attachment.content_hash
    original = {i["criterion_version_id"]: i for i in proposal.payload["items"]}
    for c in full:
        d = decisions[c.id]
        if d.method not in c.payload["allowed_methods"]:
            raise BusinessError(422, "METHOD_NOT_ALLOWED", "检查方法不在条件允许范围。")
        for ref in d.evidence_refs:
            attachment_service.available(db, owner, ref)
            if str(ref) not in saved.payload["attachment_ids"] and ref not in supplied:
                raise BusinessError(
                    422, "EVIDENCE_SCOPE_MISMATCH", "分项证据须属于提交或本次补充材料。"
                )
        if d.verdict == "pass":
            if d.method == "model_review" and (
                "execution_record" in c.payload["required_artifact_types"]
                or original[str(c.id)]["verdict"] != "pass"
            ):
                raise BusinessError(
                    422, "STATIC_REVIEW_INSUFFICIENT", "静态审阅不足以证明此项通过。"
                )
            trace = d.self_review_record or {}
            traced = d.method == "human_review" and all(
                trace.get(k, "").strip()
                for k in ("procedure", "observed", "environment", "code_version")
            )
            if traced and trace["code_version"] != saved.payload.get("code_commit"):
                raise BusinessError(
                    422, "SELF_REVIEW_VERSION_MISMATCH", "自验代码版本须对应本次提交的固定 commit。"
                )
            if c.payload["critical"] and original[str(c.id)]["verdict"] == "unknown" and not traced:
                raise BusinessError(
                    422,
                    "UNKNOWN_REQUIRES_EVIDENCE",
                    "关键未知项须有对应补充证据或可追踪的分项自验记录。",
                )
            if not set(c.payload["required_artifact_types"]) <= materials(saved, c.id):
                raise BusinessError(
                    422, "REQUIRED_MATERIAL_MISSING", "提交缺少此条件要求的材料类型；请补交新提交。"
                )
    if body.decision == "accept":
        dependencies_ready(db, owner, version)
        if any(c.payload["critical"] and decisions[c.id].verdict != "pass" for c in full):
            raise BusinessError(422, "CRITICAL_CRITERIA_INCOMPLETE", "所有关键条件通过后才能验收。")
    review = AssessmentReview(
        owner_id=owner,
        assessment_id=proposal.id,
        payload={
            "assessor_id": str(owner),
            "self_review": True,
            "supplementary_hashes": supplementary_hashes,
            "submission_id": str(saved.id),
            "task_version_id": str(version.id),
            **body.model_dump(mode="json"),
        },
    )
    db.add(review)
    db.flush()
    confirmation = AssessmentConfirmation(
        owner_id=owner, assessment_id=proposal.id, review_id=review.id, decision=body.decision
    )
    db.add(confirmation)
    db.flush()
    row.effective_confirmation_id = confirmation.id
    row.status = "accepted" if body.decision == "accept" else "needs_revision"
    row.revision += 1
    proposal.revision += 1
    delta = db.scalar(
        select(PlanDeltaProposal).where(PlanDeltaProposal.assessment_id == proposal.id)
    )
    record.response_summary = {
        "assessment_id": str(proposal.id),
        "review_id": str(review.id),
        "confirmation_id": str(confirmation.id),
        "task_status": row.status,
        "current_task_version_id": str(version.id),
        "task_revision": row.revision,
        "assessment_revision": proposal.revision,
        "plan_delta_proposal_id": str(delta.id) if delta else None,
    }
    return record.response_summary


def revoke(
    db: Session, owner: UUID, identifier: UUID, body: RevocationInput, key: str | None
) -> dict[str, Any]:
    record = idempotency(
        db, owner, f"assessment.{identifier}.revoke", key, body.model_dump(mode="json")
    )
    proposal = assessment(db, owner, identifier)
    saved = submission(db, owner, proposal.submission_id)
    initial = task(db, owner, saved.task_id)
    owned(db, BuildProject, owner, initial.project_id, lock=True)
    row = task(db, owner, saved.task_id, lock=True)
    proposal = assessment(db, owner, identifier, lock=True)
    if record.response_summary:
        return record.response_summary
    check_revision(proposal.revision, body.expected_version)
    check_revision(row.revision, body.expected_task_version)
    confirmation = (
        owned(db, AssessmentConfirmation, owner, row.effective_confirmation_id)
        if row.effective_confirmation_id
        else None
    )
    if (
        confirmation is None
        or confirmation.assessment_id != proposal.id
        or confirmation.revoked_at
        or row.current_submission_id != saved.id
    ):
        raise BusinessError(409, "STALE_CONFIRMATION", "此审阅已不是当前有效确认。")
    confirmation.revoked_at = db_now(db)
    confirmation.revocation_reason = body.reason
    row.effective_confirmation_id = None
    row.status = "needs_revision"
    row.revision += 1
    proposal.revision += 1
    record.response_summary = {
        "assessment_id": str(proposal.id),
        "task_status": row.status,
        "task_revision": row.revision,
        "assessment_revision": proposal.revision,
    }
    return record.response_summary
