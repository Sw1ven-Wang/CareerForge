"""Freeze IDE deliverables and reuse S07 review rules; never execute or call a model."""

import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application import assistance_service as assistance
from careerforge.application import attachment_service
from careerforge.application import build_service as build
from careerforge.application import planning_service as planning
from careerforge.application import studio_practice_service as practice
from careerforge.application import studio_service as studio
from careerforge.application.common import check_revision, fingerprint, idempotency
from careerforge.application.ownership import owned
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.models import (
    Assessment,
    AssessmentConfirmation,
    BuildProject,
    BuildTask,
    CareerGoalVersion,
    LearningPlan,
    LearningPlanVersion,
    LearningWorkspace,
    PlanDeltaProposal,
    Submission,
)
from careerforge.schemas.builds import (
    AssessmentInput,
    ConfirmationInput,
    ExecutionRecord,
    ProjectInput,
    RevocationInput,
    SubmissionInput,
    TaskInput,
)
from careerforge.schemas.studio import PracticePlanUpdateInput, PracticeSubmissionInput


def roots(
    db: Session, owner: UUID, workspace_id: UUID, practice_id: UUID, lock: bool = False
) -> tuple[LearningWorkspace, Json]:
    workspace = studio.workspace(db, owner, workspace_id, lock)
    item, _ = practice.validate(db, owner, workspace, practice_id)
    return workspace, item


def linked_task(db: Session, owner: UUID, item: Json) -> BuildTask | None:
    identifier = item.get("build_task_id")
    if not identifier:
        return None
    task = build.task(db, owner, UUID(identifier))
    version = build.task_version(db, owner, task.current_task_version_id)
    if str(version.objective_version_id) != item["objective_version_id"]:
        raise BusinessError(409, "PRACTICE_TASK_MISMATCH", "任务与练习目标不一致。")
    return task


def validate_snapshot(db: Session, owner: UUID, saved: Submission) -> None:
    snapshot = saved.payload.get("workspace_snapshot")
    if not snapshot:
        return
    _, item = roots(db, owner, UUID(snapshot["workspace_id"]), snapshot["practice_id"])
    if (
        str(saved.task_id) != item.get("build_task_id")
        or snapshot["source_hash"] != item["source_hash"]
    ):
        raise BusinessError(404, "PRACTICE_SOURCE_UNAVAILABLE", "提交依据不可用。")
    if snapshot.get("report"):
        if fingerprint({"files": snapshot["files"], "report": snapshot["report"]}) != snapshot.get(
            "artifact_hash"
        ):
            raise BusinessError(409, "SUBMISSION_HASH_MISMATCH", "学习报告快照不一致。")
    if fingerprint(snapshot["files"]) != snapshot["files_hash"]:
        raise BusinessError(409, "SUBMISSION_HASH_MISMATCH", "代码快照完整性校验失败。")
    attachment = attachment_service.available(db, owner, UUID(snapshot["code_attachment_id"]))
    bundle = json.loads((attachment.body or b"").decode("utf-8"))
    profile = saved.payload.get("study_evidence")
    if profile and (
        snapshot.get("assistance_hash") != fingerprint(profile)
        or bundle.get("study_evidence") != profile
        or profile["practice_id"] != snapshot["practice_id"]
        or profile["source_hash"] != item["source_hash"]
    ):
        raise BusinessError(409, "ASSISTANCE_HASH_MISMATCH", "帮助条件快照校验失败。")
    if bundle.get("report") != snapshot.get("report"):
        raise BusinessError(409, "SUBMISSION_HASH_MISMATCH", "学习报告附件与快照不一致。")
    if bundle["files"] != snapshot["files"] or bundle["files_hash"] != snapshot["files_hash"]:
        raise BusinessError(409, "SUBMISSION_HASH_MISMATCH", "代码附件与快照不一致。")


def code_version(saved: Submission) -> str | None:
    snapshot = saved.payload.get("workspace_snapshot")
    return (
        "workspace-sha256:" + snapshot.get("artifact_hash", snapshot["files_hash"])
        if snapshot
        else saved.payload.get("code_commit")
    )


def plans(db: Session, owner: UUID, objective_id: UUID | str) -> list[Json]:
    objective = planning.objective_version(db, owner, UUID(str(objective_id)))
    goal = owned(db, CareerGoalVersion, owner, objective.goal_version_id)
    root = db.scalar(
        select(LearningPlan).where(
            LearningPlan.owner_id == owner, LearningPlan.goal_id == goal.goal_id
        )
    )
    if not root:
        return []
    latest = db.scalar(
        select(LearningPlanVersion)
        .where(LearningPlanVersion.plan_id == root.id)
        .order_by(LearningPlanVersion.version_no.desc())
        .limit(1)
    )
    if not latest or UUID(str(objective_id)) not in planning.members(db, latest):
        return []
    return [
        {
            "plan_id": str(root.id),
            "plan_version_id": str(latest.id),
            "version": root.revision,
            "kind": latest.kind,
            "version_no": latest.version_no,
        }
    ]


def detail(
    db: Session,
    owner: UUID,
    workspace_id: UUID,
    practice_id: UUID,
    submission_id: UUID | None = None,
) -> Json:
    workspace, item = roots(db, owner, workspace_id, practice_id)
    criteria = planning.criteria(db, owner, UUID(item["objective_version_id"]))
    task = linked_task(db, owner, item)
    rows = (
        list(
            db.scalars(
                select(Submission)
                .where(Submission.task_id == task.id)
                .order_by(Submission.version_no.desc())
                .limit(20)
            )
        )
        if task
        else []
    )
    identifier = submission_id or (task.current_submission_id if task else None)
    saved = build.submission(db, owner, identifier) if identifier else None
    if saved and (task is None or saved.task_id != task.id):
        raise BusinessError(404, "NOT_FOUND", "提交不属于此练习。")
    proposal = (
        db.scalar(
            select(Assessment)
            .where(Assessment.submission_id == saved.id)
            .order_by(Assessment.created_at.desc())
            .limit(1)
        )
        if saved
        else None
    )
    commands = [
        {k: a.get(k) for k in ("id", "argv", "status", "exit_code", "execution_input_hash")}
        for a in workspace.payload["actions"]
        if a["kind"] == "command" and a.get("practice_id") == str(practice_id)
    ]
    for command in commands:
        command["matches_current_code"] = bool(
            command.get("execution_input_hash") == fingerprint(workspace.payload["files"])
        )
    effective = bool(
        task
        and saved
        and task.status == "accepted"
        and task.current_submission_id == saved.id
        and task.effective_confirmation_id
    )
    target_plans = plans(db, owner, item["objective_version_id"])
    if item.get("origin") == "study_module":
        from careerforge.application.study_practice_service import plan_target

        target_plans = plan_target(db, owner, item)
    return {
        "learning_evidence": {
            "learning_state": assistance.learning_state(
                saved.payload.get("study_evidence"), effective
            )
            if saved
            else "not_submitted",
            "assistance": saved.payload.get("study_evidence")
            if saved
            else workspace.payload.get("study_attempt"),
            "competence_verified": False,
            "assessment_source": "same_account_human_review" if effective else "not_accepted",
        },
        "practice_id": item["id"],
        "exercise": item["exercise"],
        "workspace_revision": workspace.revision,
        "objective_version_id": item["objective_version_id"],
        "commands": commands,
        "criteria": [{"criterion_version_id": str(c.id), **c.payload} for c in criteria],
        "task": build.task_detail(db, owner, task.id) if task else None,
        "submissions": [
            {
                "submission_id": str(s.id),
                "version_no": s.version_no,
                "code_version": code_version(s),
                "current": bool(task and s.id == task.current_submission_id),
            }
            for s in rows
        ],
        "submission": {
            "submission_id": str(saved.id),
            "version_no": saved.version_no,
            "code_version": code_version(saved),
            **saved.payload,
        }
        if saved
        else None,
        "assessment": build.assessment_detail(db, owner, proposal.id) if proposal else None,
        "assessment_run_id": saved.payload.get("assessment_run_id") if saved else None,
        "plans": target_plans,
    }


def submit(
    db: Session,
    owner: UUID,
    workspace_id: UUID,
    practice_id: UUID,
    body: PracticeSubmissionInput,
    key: str | None,
) -> Json:
    workspace, item = roots(db, owner, workspace_id, practice_id, True)
    record = idempotency(
        db,
        owner,
        f"studio.delivery:{workspace_id}:{practice_id}",
        key,
        body.model_dump(mode="json"),
    )
    if record.response_summary:
        return detail(
            db, owner, workspace_id, practice_id, UUID(record.response_summary["submission_id"])
        )
    studio.revision(workspace, body.expected_revision)
    profile = assistance.snapshot(db, owner, workspace, body.model_dump(mode="json"))
    files = dict(workspace.payload["files"])
    objective_id = UUID(item["objective_version_id"])
    criteria = planning.criteria(db, owner, objective_id)
    required = {kind for c in criteria for kind in c.payload["required_artifact_types"]}
    if not files and "code_artifact" in required:
        raise BusinessError(422, "CODE_REQUIRED", "此实现任务需要保存代码后提交。")
    studio.validate_files(files)
    report = None
    if "learning_report" in required:
        from careerforge.application.study_practice_service import spec_current

        spec = spec_current(db, owner, item["exercise_spec_id"])
        report = {
            "kind": spec.payload["definition"]["profile"]["intent"],
            "content": body.summary,
            "references": spec.payload["references"],
            "explanation_answers": [a.model_dump(mode="json") for a in body.explanation_answers],
        }

    task = linked_task(db, owner, item)
    if task is None:
        project_id = workspace.payload.get("build_project_id")
        if not project_id:
            project_id = build.create_project(
                db,
                owner,
                ProjectInput(
                    title=workspace.name,
                    problem_statement="已有学习计划中的 IDE 练习成果",
                    constraints=["用户提交、逐项人工验收"],
                ),
                str(uuid4()),
            )["build_project_id"]
            workspace.payload = {**workspace.payload, "build_project_id": project_id}
        created = build.create_task(
            db,
            owner,
            UUID(project_id),
            TaskInput(
                objective_version_id=objective_id,
                criterion_version_ids=[c.id for c in criteria],
                title=item["exercise"][:300],
                description=item["exercise"],
                deliverables=planning.objective_version(db, owner, objective_id).payload[
                    "deliverables"
                ],
            ),
            str(uuid4()),
        )
        task = build.task(db, owner, UUID(created["task_id"]))
        studio.update_action(workspace, practice_id, {"build_task_id": str(task.id)})
    version = build.task_version(db, owner, task.current_task_version_id)
    full = build.frozen_criteria(db, owner, version)
    files_hash = fingerprint(files)
    bundle = {
        "workspace_id": str(workspace_id),
        "practice_id": str(practice_id),
        "files_hash": files_hash,
        "files": files,
        "study_evidence": profile,
        "report": report,
    }
    artifact_hash = fingerprint({"files": files, "report": report}) if report else files_hash
    encoded = json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > attachment_service.MAX_BYTES:
        raise BusinessError(
            413, "SUBMISSION_TOO_LARGE", "完整代码快照超过 1 MiB，请使用更小的独立练习工作区。"
        )
    code_attachment = attachment_service.upload(
        db, owner, "workspace-code.json", encoded, "submission", str(uuid4())
    )
    ids = [UUID(code_attachment["attachment_id"])]
    commands, records = [], []
    if len(set(body.command_ids)) != len(body.command_ids):
        raise BusinessError(422, "DUPLICATE_COMMAND", "运行记录不可重复。")
    for identifier in body.command_ids:
        command = studio.action(workspace, identifier)
        if (
            command["kind"] != "command"
            or command.get("practice_id") != str(practice_id)
            or command["status"] != "completed"
        ):
            raise BusinessError(422, "EXECUTION_SCOPE_MISMATCH", "请选择此练习已完成的命令。")
        bound = bool(
            command.get("execution_input_hash") == files_hash
            and command.get("execution_finished_at")
        )
        commands.append(
            {
                **{
                    k: command.get(k)
                    for k in (
                        "id",
                        "argv",
                        "exit_code",
                        "stdout",
                        "stderr",
                        "execution_input_hash",
                        "execution_environment",
                        "execution_finished_at",
                    )
                },
                "matches_submitted_code": bound,
            }
        )
    if commands:
        output = attachment_service.upload(
            db,
            owner,
            "workspace-execution.json",
            json.dumps(commands, ensure_ascii=False).encode("utf-8"),
            "submission",
            str(uuid4()),
        )
        ids.append(UUID(output["attachment_id"]))
        for command in commands:
            if command["matches_submitted_code"]:
                records.append(
                    {
                        "command_text": json.dumps(command["argv"]),
                        "reported_exit_code": command["exit_code"],
                        "output_attachment_id": output["attachment_id"],
                        "environment_refs": ["careerforge-workspace-sandbox-v1"],
                        "reported_at": command["execution_finished_at"],
                    }
                )
    response = build.submit(
        db,
        owner,
        task.id,
        SubmissionInput(
            expected_version=task.revision,
            task_version_id=version.id,
            criterion_version_ids=[c.id for c in full],
            summary=body.summary,
            attachment_ids=ids,
            code_commit=None,
            environment={
                "kind": "careerforge_ide_snapshot",
                "code_version": "workspace-sha256:" + artifact_hash,
            },
            execution_records=[ExecutionRecord.model_validate(r) for r in records],
            explanation_answers=body.explanation_answers,
            transfer_notes=body.transfer_notes,
            actual_minutes=body.actual_minutes,
        ),
        str(uuid4()),
    )
    saved = db.get(Submission, UUID(response["submission_id"]))
    if saved is None:
        raise BusinessError(404, "NOT_FOUND", "提交记录不存在。")
    snapshot = {
        **bundle,
        "source_hash": item["source_hash"],
        "code_attachment_id": code_attachment["attachment_id"],
        "commands": commands,
        "assistance_hash": fingerprint(profile) if profile else None,
        "artifact_hash": artifact_hash,
    }
    saved.payload = {
        **saved.payload,
        "workspace_snapshot": snapshot,
        "study_evidence": profile,
        "execution_records": [
            {**v, "execution_provenance": "careerforge_sandbox"}
            for v in saved.payload["execution_records"]
        ],
    }
    run = build.start_assessment(
        db,
        owner,
        saved.id,
        AssessmentInput(criterion_version_ids=[c.id for c in full]),
        str(uuid4()),
    )
    saved.payload = {**saved.payload, "assessment_run_id": run["run_id"]}
    assistance.end_after_submission(db, workspace)
    workspace.revision += 1
    record.response_summary = {"submission_id": str(saved.id)}
    return detail(db, owner, workspace_id, practice_id, saved.id)


def check_assessment(
    db: Session, owner: UUID, workspace_id: UUID, practice_id: UUID, assessment_id: UUID
) -> Assessment:
    _, item = roots(db, owner, workspace_id, practice_id)
    task = linked_task(db, owner, item)
    proposal = build.assessment(db, owner, assessment_id)
    saved = build.submission(db, owner, proposal.submission_id)
    if not task or saved.task_id != task.id:
        raise BusinessError(404, "NOT_FOUND", "审阅不属于此练习。")
    return proposal


def review(
    db: Session,
    owner: UUID,
    workspace_id: UUID,
    practice_id: UUID,
    assessment_id: UUID,
    body: ConfirmationInput,
    key: str | None,
) -> Json:
    check_assessment(db, owner, workspace_id, practice_id, assessment_id)
    build.confirm(db, owner, assessment_id, body, key)
    return detail(db, owner, workspace_id, practice_id)


def revoke(
    db: Session,
    owner: UUID,
    workspace_id: UUID,
    practice_id: UUID,
    assessment_id: UUID,
    body: RevocationInput,
    key: str | None,
) -> Json:
    check_assessment(db, owner, workspace_id, practice_id, assessment_id)
    build.revoke(db, owner, assessment_id, body, key)
    return detail(db, owner, workspace_id, practice_id)


def effective_outcome(db: Session, owner: UUID, entry: Json) -> Json:
    try:
        task = build.task(db, owner, UUID(entry["task_id"]))
        confirmation = owned(db, AssessmentConfirmation, owner, UUID(entry["confirmation_id"]))
        proposal = build.assessment(db, owner, confirmation.assessment_id)
        saved = build.submission(db, owner, proposal.submission_id)
        valid = (
            confirmation.decision == "accept"
            and confirmation.revoked_at is None
            and task.status == "accepted"
            and task.effective_confirmation_id == confirmation.id
            and task.current_submission_id == saved.id
            and task.current_task_version_id == saved.task_version_id
            and str(saved.id) == entry["submission_id"]
        )
        return {
            **entry,
            "effective": valid,
            "status": "accepted" if valid else "needs_review",
            "learning_state": assistance.learning_state(saved.payload.get("study_evidence"), valid),
            "assistance": saved.payload.get("study_evidence"),
            "competence_verified": False,
        }
    except (BusinessError, ValueError, KeyError):
        return {
            "objective_version_id": entry.get("objective_version_id"),
            "effective": False,
            "status": "unavailable",
            "learning_state": "unavailable",
            "competence_verified": False,
            "assistance": None,
        }


def update_plan(
    db: Session,
    owner: UUID,
    workspace_id: UUID,
    practice_id: UUID,
    body: PracticePlanUpdateInput,
    key: str | None,
) -> Json:
    _, item = roots(db, owner, workspace_id, practice_id)
    if item.get("origin") == "study_module":
        from careerforge.application.study_practice_service import update_plan as update_module_plan

        return update_module_plan(
            db, owner, workspace_id, practice_id, body.model_dump(mode="json"), key
        )
    record = idempotency(
        db, owner, f"studio.delivery_plan:{practice_id}", key, body.model_dump(mode="json")
    )
    source = owned(db, LearningPlanVersion, owner, body.base_plan_version_id)
    target = owned(db, CareerGoalVersion, owner, source.goal_version_id)
    task = linked_task(db, owner, item)
    if task:
        owned(db, BuildProject, owner, task.project_id, lock=True)
        owned(db, BuildTask, owner, task.id, lock=True)
        db.refresh(task)
    planning.current_goal(db, owner, target)
    root = owned(db, LearningPlan, owner, body.plan_id, lock=True)
    if record.response_summary:
        row = owned(
            db, LearningPlanVersion, owner, UUID(record.response_summary["plan_version_id"])
        )
        return planning.plan_result(db, owner, row)
    check_revision(root.revision, body.expected_plan_version)
    latest = db.scalar(
        select(LearningPlanVersion)
        .where(LearningPlanVersion.plan_id == root.id)
        .order_by(LearningPlanVersion.version_no.desc())
        .limit(1)
    )
    ids = planning.members(db, source)
    if (
        source.plan_id != root.id
        or latest is None
        or latest.id != source.id
        or UUID(item["objective_version_id"]) not in ids
    ):
        raise BusinessError(409, "PLAN_BASE_CHANGED", "请选择包含此目标的最新已有计划版本。")
    task = linked_task(db, owner, item)
    if not task or not task.effective_confirmation_id:
        raise BusinessError(409, "ACCEPTANCE_REQUIRED", "先完成人工验收，再更新计划。")
    confirmation = owned(db, AssessmentConfirmation, owner, task.effective_confirmation_id)
    proposal = build.assessment(db, owner, confirmation.assessment_id)
    entry = {
        "objective_version_id": item["objective_version_id"],
        "practice_id": item["id"],
        "workspace_id": str(workspace_id),
        "task_id": str(task.id),
        "submission_id": str(proposal.submission_id),
        "assessment_id": str(proposal.id),
        "confirmation_id": str(confirmation.id),
        "reason": body.reason,
    }
    if not effective_outcome(db, owner, entry)["effective"]:
        raise BusinessError(409, "ACCEPTANCE_REQUIRED", "验收已撤销、已过期或不是当前提交。")
    # Same project/task locks as S07 confirmation and revocation.
    owned(db, BuildProject, owner, task.project_id, lock=True)
    owned(db, BuildTask, owner, task.id, lock=True)
    if not effective_outcome(db, owner, entry)["effective"]:
        raise BusinessError(409, "ACCEPTANCE_REQUIRED", "验收状态已变化。")
    planning.validate_members(db, owner, target.id, ids, source.payload.get("user_constraints", {}))
    outcomes = {**source.payload.get("practice_outcomes", {}), item["objective_version_id"]: entry}
    root.revision += 1
    row = planning.new_plan_version(
        db,
        root,
        target.id,
        ids,
        "draft",
        {
            **source.payload,
            "practice_outcomes": outcomes,
            "adjustment_reason": body.reason,
            "source_plan_version_id": str(source.id),
            "schedule_status": "unconfirmed",
        },
        source.alignment_report_id,
    )
    delta = db.scalar(
        select(PlanDeltaProposal).where(PlanDeltaProposal.assessment_id == proposal.id)
    )
    if delta:
        delta.payload = {
            **delta.payload,
            "applied_plan_version_id": str(row.id),
            "manual_confirmation": True,
        }
    record.response_summary = {"plan_version_id": str(row.id)}
    return planning.plan_result(db, owner, row)
