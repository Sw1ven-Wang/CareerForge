"""Adapt new study modules to existing objectives, practice submissions and human assessment."""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application import assistance_service as assistance
from careerforge.application import planning_service as planning
from careerforge.application import studio_service as studio
from careerforge.application import study_service as study
from careerforge.application.common import check_revision, fingerprint, idempotency
from careerforge.application.goal_service import save_goal
from careerforge.application.ownership import owned
from careerforge.application.repository_service import citation_detail
from careerforge.application.run_service import db_now
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.models import (
    CareerGoalVersion,
    LearningObjective,
    LearningWorkspace,
)
from careerforge.infrastructure.db.study_models import StudyArtifact
from careerforge.schemas.planning import CriterionInput, ObjectiveInput
from careerforge.schemas.resources import GoalInput
from careerforge.schemas.study_practice import AttemptCloseInput, ModuleAttemptInput

CORE = ("plan_id", "module_key", "module_hash", "requirements_id", "definition", "references")


def spec_current(db: Session, owner: UUID, identifier: UUID | str) -> StudyArtifact:
    item = owned(db, StudyArtifact, owner, UUID(str(identifier)))
    if item.kind != "exercise":
        raise BusinessError(404, "NOT_FOUND", "练习定义不可用。")
    track = study.track(db, owner, item.track_id)
    payload = item.payload
    if fingerprint({k: payload[k] for k in CORE}) != payload["content_hash"]:
        raise BusinessError(409, "EXERCISE_HASH_MISMATCH", "练习版本完整性校验失败。")
    plan = study.artifact_row(db, owner, track.id, payload["plan_id"], "plan")
    study.validate_plan_scope(db, track, plan)
    module = next((m for m in plan.payload["modules"] if m["key"] == payload["module_key"]), None)
    if not module or fingerprint(module) != payload["module_hash"]:
        raise BusinessError(409, "EXERCISE_SOURCE_CHANGED", "练习所依据的模块版本已变化。")
    for ref in payload["references"]:
        actual = citation_detail(db, owner, UUID(ref["citation_id"]))
        if actual["content_hash"] != ref["content_hash"] or actual["commit"] != ref["commit"]:
            raise BusinessError(409, "EXERCISE_REFERENCE_CHANGED", "练习引用与固定版本不一致。")
    return item


def validate(
    db: Session, owner: UUID, workspace: LearningWorkspace, item: Json
) -> tuple[Json, Json]:
    spec = spec_current(db, owner, item["exercise_spec_id"])
    attempt = study.artifact_row(db, owner, spec.track_id, item["attempt_id"], "attempt")
    binding = workspace.payload.get("study_attempt", {})
    if (
        attempt.payload["workspace_id"] != str(workspace.id)
        or attempt.payload["practice_id"] != item["id"]
        or item["source_hash"] != spec.payload["content_hash"]
        or item["objective_version_id"] != spec.payload["objective_version_id"]
        or binding.get("practice_id") != item["id"]
        or binding.get("help_mode") != attempt.payload["help_mode"]
    ):
        raise BusinessError(409, "ATTEMPT_BINDING_MISMATCH", "尝试、工作区与练习版本不一致。")
    return item, spec.payload


def context(
    db: Session, owner: UUID, row: LearningWorkspace, item: Json
) -> tuple[Json, list[Json], Json]:
    _, spec = validate(db, owner, row, item)
    references = [citation_detail(db, owner, UUID(r["citation_id"])) for r in spec["references"]]
    return (
        item,
        references,
        {
            "practice_id": item["id"],
            "exercise": item["exercise"],
            "definition": spec["definition"],
            "module_key": spec["module_key"],
            "help_mode": row.payload["study_attempt"]["help_mode"],
            "note_untrusted": item.get("note", ""),
            "verified": False,
        },
    )


def create_attempt(
    db: Session, owner: UUID, track_id: UUID, body: ModuleAttemptInput, key: str | None
) -> Json:
    track = study.track(db, owner, track_id, True)
    record = idempotency(db, owner, f"study.attempt:{track_id}", key, body.model_dump(mode="json"))
    if record.response_summary:
        attempt = study.artifact_row(
            db, owner, track_id, record.response_summary["attempt_id"], "attempt"
        )
        row = studio.workspace(db, owner, UUID(attempt.payload["workspace_id"]))
        validate(db, owner, row, studio.action(row, attempt.payload["practice_id"]))
        return {**record.response_summary, "workspace": studio.detail(row, db)}
    check_revision(track.revision, body.expected_revision)
    if body.help_mode == "independent":
        assistance.before_independent(db, owner)
    plan = study.artifact_row(db, owner, track_id, body.plan_id, "plan")
    study.validate_plan_scope(db, track, plan)
    module = next((m for m in plan.payload["modules"] if m["key"] == body.module_key), None)
    if not module or any(m["parent_key"] == body.module_key for m in plan.payload["modules"]):
        raise BusinessError(422, "LEAF_MODULE_REQUIRED", "请选择一个有代码依据的叶模块开始练习。")
    # The selected immutable plan authorizes its own frozen reviews, including historical ones.
    highlights = [
        h
        for rid in plan.payload["review_ids"]
        for h in study.artifact_row(db, owner, track_id, rid, "review").payload["highlights"]
    ]
    highlights += plan.payload.get("focus_highlights", [])
    selected = {h["citation_id"] for h in highlights if h["citation_id"] in module["highlight_ids"]}
    if not selected or not set(module["highlight_ids"]) <= selected:
        raise BusinessError(409, "EXERCISE_REFERENCE_REQUIRED", "模块的代码依据不可用。")
    references = [citation_detail(db, owner, UUID(i)) for i in sorted(selected)]
    from careerforge.application.study_exercises import profile

    exercise_profile = profile(plan.payload["intent"]["kind"], module, body.exercise_kind)
    if exercise_profile["execution_required"]:
        from careerforge.infrastructure.runtime_capabilities import planning as runtime_plan

        runtime_plan(
            plan.payload["intent"],
            study.artifact_row(
                db, owner, track_id, plan.payload["requirements_id"], "requirements"
            ).payload["requirements"],
            [
                study.packed(study.artifact_row(db, owner, track_id, i, "review"))
                for i in plan.payload["review_ids"]
            ],
        )
    core = {
        "plan_id": str(plan.id),
        "module_key": body.module_key,
        "module_hash": fingerprint(module),
        "requirements_id": plan.payload["requirements_id"],
        "definition": {
            "instruction": body.instruction,
            "acceptance": body.acceptance,
            "exercise_kind": body.exercise_kind,
            "variation": body.variation,
            "deliverable": module["deliverable"],
            "module": module,
            "profile": exercise_profile,
        },
        "references": [
            {
                k: r[k]
                for k in ("citation_id", "commit", "content_hash", "path", "start_line", "end_line")
            }
            for r in references
        ],
    }
    hashed = fingerprint(core)
    records = list(
        db.scalars(
            select(StudyArtifact).where(
                StudyArtifact.track_id == track_id, StudyArtifact.kind.in_(["exercise", "attempt"])
            )
        )
    )
    attempts = [r for r in records if r.kind == "attempt"]
    if len(attempts) >= 100:
        raise BusinessError(422, "ATTEMPT_LIMIT", "此方向已记录100次尝试，请整理或建立新方向。")
    specs = [
        r
        for r in records
        if r.kind == "exercise"
        and r.payload["plan_id"] == str(plan.id)
        and r.payload["module_key"] == body.module_key
    ]
    spec = next((s for s in specs if s.payload["content_hash"] == hashed), None)
    if spec is None:
        spec = study.put(
            db, track, "exercise", {**core, "content_hash": hashed, "version": len(specs) + 1}
        )
        goal_id = track.payload.get("practice_goal_version_id")
        if not goal_id:
            saved = save_goal(
                db,
                owner,
                GoalInput(
                    role_direction=track.name,
                    job_type="unspecified",
                    user_objectives=["已有模块计划的练习验收适配；不代表新增岗位事实"],
                ),
                f"study-practice:{track.id}",
            )
            goal_id = saved["goal_version_id"]
            track.payload = {**track.payload, "practice_goal_version_id": goal_id}
        goal = owned(db, CareerGoalVersion, owner, UUID(goal_id))
        conditions = "；".join(body.acceptance)
        criteria = [
            CriterionInput.model_validate(
                dict(
                    dimension=c["dimension"],
                    scenario=c["scenario"],
                    expected_behavior=conditions
                    if c["dimension"] == "artifact"
                    else (
                        body.variation
                        if c["dimension"] == "transfer"
                        else "本人解释机制、依据、取舍与未验证边界。"
                    ),
                    pass_rule="核对本次冻结材料与范围，记录本人观察及成果版本。",
                    fail_rule="材料显示条件未满足。",
                    unknown_rule="缺少材料时保持未知。",
                    critical=True,
                    allowed_methods=["human_review"],
                    required_artifact_types=c["materials"],
                    limitations=["同账号自验不等于第三方认证；帮助条件单独记录。"],
                )
            )
            for c in exercise_profile["criteria"]
        ]
        objective_root = LearningObjective(owner_id=owner, goal_id=goal.goal_id)
        db.add(objective_root)
        db.flush()
        objective = planning.persist_objective(
            db,
            owner,
            ObjectiveInput(
                goal_version_id=goal.id,
                title=module["title"],
                target_behavior=body.instruction,
                scope="固定模块与练习定义；参考源码可读；Agent帮助条件在每次尝试冻结。",
                deliverables=[module["deliverable"]],
                criteria=criteria,
                scope_confirmed=True,
            ),
            objective_root,
            "user_goal",
            [],
        )
        objective.payload = {
            **objective.payload,
            "study_basis": {"exercise_spec_id": str(spec.id), "content_hash": hashed},
        }
        objective.content_hash = fingerprint(objective.payload)
        spec.payload = {**spec.payload, "objective_version_id": str(objective.id)}
    else:
        spec_current(db, owner, spec.id)
    practice_id = str(uuid4())
    workspace = LearningWorkspace(
        owner_id=owner,
        name=(
            module["title"]
            + " · "
            + ("独立" if body.help_mode == "independent" else "辅助")
            + "尝试"
        )[:100],
        payload={"files": {}, "actions": []},
    )
    db.add(workspace)
    db.flush()
    attempt = study.put(
        db,
        track,
        "attempt",
        {
            "exercise_spec_id": str(spec.id),
            "workspace_id": str(workspace.id),
            "practice_id": practice_id,
            "plan_id": str(plan.id),
            "module_key": body.module_key,
            "help_mode": body.help_mode,
            "ordinal": 1 + sum(a.payload["exercise_spec_id"] == str(spec.id) for a in attempts),
        },
    )
    binding = {
        **attempt.payload,
        "attempt_id": str(attempt.id),
        "track_id": str(track.id),
        "source_hash": hashed,
        "exercise_version": spec.payload["version"],
        "exercise_kind": body.exercise_kind,
        "module_hash": core["module_hash"],
        "started_at": db_now(db).isoformat(),
        "ended_at": None,
        "conditions": "固定参考源码可读；独立模式禁用本系统导师和IDE Agent；外部帮助须声明。",
    }
    workspace.payload = {**workspace.payload, "study_attempt": binding}
    studio.append(
        workspace,
        {
            "id": practice_id,
            "kind": "practice",
            "origin": "study_module",
            "status": "draft",
            "exercise_spec_id": str(spec.id),
            "attempt_id": str(attempt.id),
            "source_hash": hashed,
            "objective_version_id": spec.payload["objective_version_id"],
            "objective_title": module["title"],
            "exercise": body.instruction,
            "citation_ids": sorted(selected),
            "note": "",
            "verified": False,
        },
    )
    track.revision += 1
    record.response_summary = {
        "attempt_id": str(attempt.id),
        "workspace_id": str(workspace.id),
        "practice_id": practice_id,
        "objective_version_id": spec.payload["objective_version_id"],
    }
    return {**record.response_summary, "workspace": studio.detail(workspace, db)}


def close_attempt(
    db: Session, owner: UUID, track_id: UUID, identifier: UUID, body: AttemptCloseInput
) -> Json:
    attempt = study.artifact_row(db, owner, track_id, identifier, "attempt")
    row = studio.workspace(db, owner, UUID(attempt.payload["workspace_id"]), True)
    studio.revision(row, body.expected_revision)
    binding = row.payload["study_attempt"]
    if any(a["kind"] == "command" and a["status"] == "running" for a in row.payload["actions"]):
        raise BusinessError(409, "WORKSPACE_BUSY", "命令执行中，请等待结束后关闭尝试。")
    if not binding.get("ended_at"):
        row.payload = {
            **row.payload,
            "study_attempt": {
                **binding,
                "ended_at": db_now(db).isoformat(),
                "ended_reason": "abandoned",
                "close_note": body.reason,
            },
        }
        row.revision += 1
    return {"attempt_id": str(identifier), "workspace": studio.detail(row, db)}


def attempts(
    db: Session, owner: UUID, track_id: UUID, plan_id: UUID, module_key: str
) -> list[Json]:
    track = study.track(db, owner, track_id)
    plan = study.artifact_row(db, owner, track_id, plan_id, "plan")
    study.validate_plan_scope(db, track, plan)
    module = next((m for m in plan.payload["modules"] if m["key"] == module_key), None)
    if not module:
        raise BusinessError(404, "NOT_FOUND", "模块不存在。")
    result = []
    from careerforge.application import studio_delivery_service as delivery

    for attempt in db.scalars(
        select(StudyArtifact)
        .where(StudyArtifact.track_id == track_id, StudyArtifact.kind == "attempt")
        .order_by(StudyArtifact.created_at)
    ):
        if attempt.payload["module_key"] != module_key:
            continue
        try:
            spec = spec_current(db, owner, attempt.payload["exercise_spec_id"])
            from careerforge.application.study_versions import compatible

            origin = study.artifact_row(db, owner, track_id, spec.payload["plan_id"], "plan")
            if not compatible(db, origin, plan, module_key):
                continue
            row = studio.workspace(db, owner, UUID(attempt.payload["workspace_id"]))
            state = delivery.detail(db, owner, row.id, UUID(attempt.payload["practice_id"]))
            result.append(
                {
                    "attempt_id": str(attempt.id),
                    **attempt.payload,
                    "exercise_version": spec.payload["version"],
                    "instruction": spec.payload["definition"]["instruction"],
                    "binding": row.payload["study_attempt"],
                    "workspace_revision": row.revision,
                    "learning_state": state["learning_evidence"]["learning_state"],
                    "submission_id": state["submission"]["submission_id"]
                    if state["submission"]
                    else None,
                    "evidence": state["learning_evidence"],
                }
            )
        except BusinessError:
            result.append(
                {"attempt_id": str(attempt.id), "available": False, "learning_state": "unavailable"}
            )
    return result


def plan_target(db: Session, owner: UUID, item: Json) -> list[Json]:
    spec = spec_current(db, owner, item["exercise_spec_id"])
    track = study.track(db, owner, spec.track_id)
    from careerforge.application.study_versions import versions

    origin = study.artifact_row(db, owner, track.id, spec.payload["plan_id"], "plan")
    plan = versions(db, origin)[-1]
    study.validate_plan_scope(db, track, plan)
    module = next(
        (m for m in plan.payload["modules"] if m["key"] == spec.payload["module_key"]), None
    )
    from careerforge.application.study_versions import compatible

    scope_compatible = bool(module and compatible(db, origin, plan, spec.payload["module_key"]))
    return [
        {
            "plan_id": str(plan.id),
            "plan_version_id": str(plan.id),
            "version": track.revision,
            "version_no": plan.payload.get("revision_no", 1),
            "kind": "study",
            "title": plan.payload["title"],
            "scope_compatible": scope_compatible,
        }
    ]


def update_plan(
    db: Session, owner: UUID, workspace_id: UUID, practice_id: UUID, body: Json, key: str | None
) -> Json:
    from careerforge.application import studio_delivery_service as delivery

    row = studio.workspace(db, owner, workspace_id)
    item, _ = validate(db, owner, row, studio.action(row, practice_id))
    spec = spec_current(db, owner, item["exercise_spec_id"])
    task = delivery.linked_task(db, owner, item)
    if not task or not task.effective_confirmation_id:
        raise BusinessError(409, "ACCEPTANCE_REQUIRED", "先完成人工验收，再更新已有计划。")
    from careerforge.infrastructure.db.models import AssessmentConfirmation, BuildProject, BuildTask

    owned(db, BuildProject, owner, task.project_id, lock=True)
    owned(db, BuildTask, owner, task.id, lock=True)
    track = study.track(db, owner, spec.track_id, True)
    record = idempotency(db, owner, f"study.outcome:{practice_id}", key, body)
    if record.response_summary:
        plan = study.artifact_row(
            db, owner, track.id, record.response_summary["plan_version_id"], "plan"
        )
        return {
            "plan_id": str(plan.id),
            "plan_version_id": str(plan.id),
            "version": track.revision,
            "version_no": plan.payload.get("revision_no", 1),
            "kind": "study",
        }
    target = plan_target(db, owner, item)
    if (
        not target
        or body["plan_id"] != target[0]["plan_id"]
        or body["base_plan_version_id"] != target[0]["plan_version_id"]
    ):
        raise BusinessError(
            409, "PLAN_BASE_CHANGED", "请选择包含同一模块与练习范围的最新已有计划。"
        )
    check_revision(track.revision, body["expected_plan_version"])
    confirmation = owned(db, AssessmentConfirmation, owner, task.effective_confirmation_id)
    from careerforge.application import build_service as build

    proposal = build.assessment(db, owner, confirmation.assessment_id)
    entry = {
        "task_id": str(task.id),
        "submission_id": str(proposal.submission_id),
        "confirmation_id": str(confirmation.id),
        "objective_version_id": item["objective_version_id"],
        "workspace_id": str(workspace_id),
        "practice_id": str(practice_id),
        "attempt_id": item["attempt_id"],
        "exercise_spec_id": item["exercise_spec_id"],
        "module_hash": spec.payload["module_hash"],
        "reason": body["reason"],
    }
    if not delivery.effective_outcome(db, owner, entry)["effective"]:
        raise BusinessError(409, "ACCEPTANCE_REQUIRED", "验收已失效，请重新核对。")
    source = study.artifact_row(db, owner, track.id, body["base_plan_version_id"], "plan")
    compatible = target[0]["scope_compatible"]
    updated = study.put(
        db,
        track,
        "plan",
        {
            **source.payload,
            "module_outcomes": {
                **source.payload.get("module_outcomes", {}),
                **({spec.payload["module_key"]: entry} if compatible else {}),
            },
            "historical_outcomes": [
                *source.payload.get("historical_outcomes", []),
                *(
                    []
                    if compatible
                    else [
                        {
                            **entry,
                            "needs_review": True,
                            "reason": "已验收的是旧练习范围，不代表当前模块已通过。",
                        }
                    ]
                ),
            ],
            "source_plan_id": str(source.id),
            "revision_no": source.payload.get("revision_no", 1) + 1,
            "update_reason": body["reason"],
            "update_kind": "human_accepted_practice",
            "modules_unchanged": True,
        },
    )
    progress = dict(track.payload.get("progress", {}))
    for module in source.payload["modules"]:
        prior = progress.get(f"{source.id}:{module['key']}")
        if prior:
            progress[f"{updated.id}:{module['key']}"] = dict(prior)
    from careerforge.application.study_versions import family

    active_id = track.payload.get("active_plan_id")
    active = study.artifact_row(db, owner, track.id, active_id, "plan") if active_id else None
    activate = not active or family(db, active) == family(db, source)
    track.payload = {
        **track.payload,
        "progress": progress,
        **({"active_plan_id": str(updated.id)} if activate else {}),
    }
    track.revision += 1
    record.response_summary = {"plan_version_id": str(updated.id)}
    return {
        "plan_id": str(updated.id),
        "plan_version_id": str(updated.id),
        "version": track.revision,
        "kind": "study",
        "version_no": updated.payload["revision_no"],
    }
