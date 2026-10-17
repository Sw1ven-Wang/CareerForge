"""Workspace-level assistance isolation and immutable submission-time observations."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from careerforge.application.common import fingerprint
from careerforge.application.run_service import db_now
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.models import AgentRun, LearningWorkspace, User
from careerforge.infrastructure.db.study_models import StudyArtifact


def lock_owner(db: Session, owner: UUID) -> None:
    if db.scalar(select(User).where(User.id == owner).with_for_update()) is None:
        raise BusinessError(404, "NOT_FOUND", "账号不可用。")


def active_independent(db: Session, owner: UUID) -> LearningWorkspace | None:
    return db.scalar(
        select(LearningWorkspace)
        .where(
            LearningWorkspace.owner_id == owner,
            LearningWorkspace.payload["study_attempt"]["help_mode"].astext == "independent",
            LearningWorkspace.payload["study_attempt"]["ended_at"].astext.is_(None),
        )
        .limit(1)
    )


def guard_agent(db: Session, owner: UUID, workspace: LearningWorkspace | None = None) -> None:
    # Serialize starts with independent-attempt creation; no extra permission can come from prompts.
    lock_owner(db, owner)
    if (
        workspace and workspace.payload.get("study_attempt", {}).get("help_mode") == "independent"
    ) or active_independent(db, owner):
        raise BusinessError(
            409,
            "INDEPENDENT_ATTEMPT_ACTIVE",
            "独立练习中禁用本系统导师和 IDE Agent；请先提交或结束该次尝试。",
        )


def before_independent(db: Session, owner: UUID) -> None:
    lock_owner(db, owner)
    if active_independent(db, owner):
        raise BusinessError(409, "INDEPENDENT_ATTEMPT_ACTIVE", "请先提交或结束当前独立尝试。")
    active = db.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.owner_id == owner,
            AgentRun.status.in_(["queued", "running", "waiting_user"]),
            (AgentRun.input_manifest["workspace_id"].astext.is_not(None))
            | (AgentRun.input_manifest["role"].astext == "tutor"),
        )
        .limit(1)
    )
    if active:
        raise BusinessError(
            409, "ASSISTANCE_RUN_ACTIVE", "请先结束已有导师或 IDE Agent 任务，再开始独立尝试。"
        )


def bound_practice(row: LearningWorkspace, supplied: UUID | None) -> UUID | None:
    binding = row.payload.get("study_attempt")
    if not binding:
        return supplied
    expected = UUID(binding["practice_id"])
    if supplied and supplied != expected:
        raise BusinessError(422, "ATTEMPT_WORKSPACE_MISMATCH", "工作区已绑定另一练习。")
    return expected


def guard_patch(row: LearningWorkspace, item: Json) -> None:
    if row.payload.get("study_attempt", {}).get("help_mode") == "independent" and not item.get(
        "command_id"
    ):
        raise BusinessError(
            409, "INDEPENDENT_PATCH_FORBIDDEN", "独立工作区不能应用 Agent 或来源不明的补丁。"
        )


def snapshot(db: Session, owner: UUID, row: LearningWorkspace, declaration: Json) -> Json | None:
    binding = row.payload.get("study_attempt")
    if not binding:
        return None
    if binding.get("ended_reason") == "abandoned" or (
        binding["help_mode"] == "independent" and binding.get("ended_at")
    ):
        raise BusinessError(409, "ATTEMPT_CLOSED", "此独立尝试已冻结或结束，请从模块创建新尝试。")
    requests = (
        db.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.owner_id == owner,
                AgentRun.input_manifest["workspace_id"].astext == str(row.id),
            )
        )
        or 0
    )
    tutor_turns = (
        db.scalar(
            select(func.count())
            .select_from(StudyArtifact)
            .where(
                StudyArtifact.owner_id == owner,
                StudyArtifact.track_id == UUID(binding["track_id"]),
                StudyArtifact.kind == "tutor",
                StudyArtifact.created_at >= row.created_at,
                StudyArtifact.payload["module_key"].astext == binding["module_key"],
            )
        )
        or 0
    )
    patches = [
        a
        for a in row.payload["actions"]
        if a["kind"] == "patch" and a["status"] == "applied" and not a.get("command_id")
    ]
    observed = requests > 0 or tutor_turns > 0 or bool(patches)
    declared = declaration["external_help"] == "used" or declaration["reported_hint_count"] > 0
    return {
        **binding,
        "submitted_at": db_now(db).isoformat(),
        "files_hash": fingerprint(row.payload["files"]),
        "system_agent_requests": requests,
        "system_tutor_turns": tutor_turns,
        "system_applied_agent_patches": len(patches),
        "external_help": declaration["external_help"],
        "reported_hint_count": declaration["reported_hint_count"],
        "assistance_note": declaration["assistance_note"],
        "effective_help": "assisted"
        if binding["help_mode"] == "agent_assisted" or observed or declared
        else "independent_declared"
        if declaration["external_help"] == "none"
        else "unknown",
        "observation_scope": (
            "本系统请求与补丁记录；外部帮助和提示次数由本人声明，"
            "不能证明其他设备未获帮助。"
        ),
        "competence_verified": False,
    }


def end_after_submission(db: Session, row: LearningWorkspace) -> None:
    binding = row.payload.get("study_attempt")
    if binding and binding["help_mode"] == "independent":
        row.payload = {
            **row.payload,
            "study_attempt": {
                **binding,
                "ended_at": db_now(db).isoformat(),
                "ended_reason": "submitted",
            },
        }


def learning_state(profile: Json | None, effective: bool) -> str:
    if not effective:
        return "needs_review"
    if not profile:
        return "accepted_help_unknown"
    if profile["effective_help"] == "assisted":
        return "assisted_completion"
    if profile["effective_help"] == "independent_declared":
        return (
            "transfer_passed_declared_independent"
            if profile["exercise_kind"] == "transfer"
            else "independent_completion_declared"
        )
    return "accepted_help_unknown"
