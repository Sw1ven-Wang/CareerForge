"""Project workflow progress from owner-visible runs, stages and publications."""

from sqlalchemy import select

from ..errors import DomainError
from ..models import AgentRun, RunEvent, StageArtifact
from ..models_dialogue import WorkflowRunLink
from .runtime import TERMINAL

ONBOARDING = "请粘贴 JD、提供 GitHub 项目，或告诉我想学习的技术。后台运行时可随时问进度。"
STAGE_LABELS = {
    "project_discovery": "项目检索、质量初审与入库",
    "extract": "JD 需求提取",
    "build": "项目知识构建",
    "analysis": "源码分析",
    "review": "独立审核",
    "plan": "学习计划",
    "tutor": "Tutor 讲解",
}


def workflow_runs(db, workflow_id):
    links = list(
        db.scalars(
            select(WorkflowRunLink)
            .where(WorkflowRunLink.workflow_id == workflow_id)
            .order_by(WorkflowRunLink.created_at, WorkflowRunLink.id)
        )
    )
    roots = [(link, db.get(AgentRun, link.run_id)) for link in links]
    return [(link, run) for link, run in roots if run is not None]


def run_family(db, root):
    return [
        root,
        *db.scalars(
            select(AgentRun)
            .where(AgentRun.parent_id == root.id)
            .order_by(AgentRun.created_at, AgentRun.id)
        ),
    ]


class StatusProjector:
    def __init__(self, app):
        self.app = app

    def current(self, db, actor, conversation, workflow):
        if workflow is None:
            return {
                "conversation_id": conversation.id,
                "focus_revision": conversation.revision,
                "workflow_id": None,
                "goal_version_id": None,
                "control_state": "active",
                "execution_state": "idle",
                "milestone": "awaiting_jd",
                "onboarding": ONBOARDING,
                "entry_choices": ["jd", "repository", "topic"],
                "active_stages": [],
                "completed_stages": [],
                "publication_count": 0,
                "calls_consumed": 0,
                "calls_held": 0,
                "call_cap": 0,
                "waiting_user_action": "provide_goal",
                "coverage": "unknown",
                "last_error_code": None,
                "run_refs": [],
                "next_action": "provide_goal",
            }

        roots = workflow_runs(db, workflow.id)
        stages, completed, publication_ids, root_states, run_refs = [], [], set(), [], []
        coverage, error, waiting = "unknown", None, workflow.target.get("waiting_user_action")
        if workflow.source_jd_revision_id:
            try:
                self.app.jd.read_in(db, actor, workflow.source_jd_revision_id)
                completed.append("jd_import")
            except DomainError:
                error = "EVIDENCE_UNAVAILABLE"
        for link, root in roots:
            if root.principal_id != actor or root.namespace_id != workflow.namespace_id:
                raise DomainError("WORKFLOW_RUN_SCOPE", "任务不属于当前工作流。", 409)
            root_states.append(root.status)
            run_refs.append(root.id)
            if root.status in {"succeeded", "partially_succeeded"}:
                completed.append(link.milestone)
            for run in run_family(db, root):
                stage_key = link.milestone if run.id == root.id else run.workflow
                final = db.scalar(
                    select(RunEvent)
                    .where(RunEvent.run_id == run.id, RunEvent.event_type == "run.finished")
                    .order_by(RunEvent.seq.desc())
                    .limit(1)
                )
                if final is not None:
                    error = final.payload.get("code") or error
                if run.status in TERMINAL:
                    continue
                saved = list(
                    db.scalars(
                        select(StageArtifact.stage_key)
                        .where(
                            StageArtifact.run_id == run.id,
                            StageArtifact.input_hash == run.input_hash,
                        )
                        .order_by(StageArtifact.created_at, StageArtifact.stage_key)
                    )
                )
                stages.append(
                    {
                        "key": stage_key,
                        "label": STAGE_LABELS.get(stage_key, stage_key),
                        "status": run.status,
                        "run_ids": [run.id],
                        "stage_keys": saved,
                        "source_ids": run.input_manifest.get("source_ids", [])
                        or (
                            [run.input_manifest["source_id"]]
                            if run.input_manifest.get("source_id")
                            else []
                        ),
                        "started_at": run.created_at.isoformat(),
                    }
                )
            if link.milestone != "build":
                continue
            artifact = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == root.id,
                    StageArtifact.stage_key == "result",
                    StageArtifact.input_hash == root.input_hash,
                )
            )
            if artifact is not None:
                stated = set(artifact.payload.get("publication_ids", []))
                valid = set()
                for publication_id in stated:
                    try:
                        self.app.knowledge.publication_pack(db, actor, publication_id)
                    except DomainError:
                        continue
                    valid.add(publication_id)
                publication_ids.update(valid)
                if valid:
                    verdict = artifact.payload.get("task_coverage", {}).get("status")
                    coverage = (
                        "complete"
                        if valid == stated and verdict == "complete" and coverage != "partial"
                        else "partial"
                    )
        stored = set(workflow.target.get("publication_ids", []))
        for publication_id in stored - publication_ids:
            try:
                self.app.knowledge.publication_pack(db, actor, publication_id)
            except DomainError:
                continue
            publication_ids.add(publication_id)
        if publication_ids and (
            coverage == "unknown"
            or not stored <= publication_ids
            or workflow.target.get("coverage") == "partial"
        ):
            coverage = "partial"
        execution = next(
            (
                status
                for status in ("running", "waiting_children", "queued", "paused")
                if status in root_states
            ),
            root_states[-1] if root_states else "idle",
        )
        control = workflow.control_state
        if control == "pause_requested" and roots and workflow.control_generation == 0:
            if all(
                run.status in TERMINAL | {"paused"}
                for _, root in roots
                for run in run_family(db, root)
            ):
                control = "paused"
        if control == "pause_requested" and any(
            event["status"] == "pause_pending_inflight" for event in stages
        ):
            waiting = "needs_reconciliation"
        if control == "pause_requested" and waiting is None:
            waiting = "await_pause_checkpoint"
        if control == "paused":
            waiting = waiting or "resume"
        session_fields = {}
        if workflow.orchestration_engine == "langgraph":
            session = self.app.session_workflows.current(db, workflow)
            if session is not None:
                self.app.session_workflows.validate_in(db, workflow, session, access=False)
                session_fields = {
                    "execution_id": session.id,
                    "orchestration_engine": "langgraph",
                    "graph_version": session.graph_version,
                    "wait_kind": session.wait_kind,
                }
                if workflow.control_generation > 0:
                    command = self.app.workflow_controls.current(db, workflow)
                    session_fields["control_command"] = (
                        {
                            "command_id": command.id,
                            "kind": command.kind,
                            "status": command.status,
                            "checkpoint_persisted": command.checkpoint_id is not None,
                        }
                        if command
                        else None
                    )
                if session.status != "completed" or not stages:
                    execution = session.status
                    if session.status == "waiting":
                        execution = (
                            "waiting_user"
                            if session.wait_kind in {"approval", "authorization"}
                            else "waiting_children"
                        )
                    if session.wait_kind == "approval":
                        waiting = "accept_plan"
                    elif session.wait_kind == "authorization":
                        waiting = "authorize_plan"
                    elif session.wait_kind == "evidence":
                        waiting = "reviewed_evidence_required"
                    elif session.wait_kind == "plan_failure":
                        waiting = "review_run_failure"
                    error = session.last_error_code or error
                if session.workflow_type == "learning_session":
                    request = self.app.learning_requests.last_in(db, workflow)
                    session_fields.update(
                        phase="learning",
                        predecessor_execution_id=session.predecessor_execution_id,
                        admission_origin=session.binding_payload["origin"],
                        accepted_plan=session.binding_payload["accepted"]["plan_id"],
                        accepted_version_id=session.binding_payload["accepted"]["version_id"],
                        request_id=request.id if request else None,
                        request_status=request.status if request else None,
                        turn_id=request.turn_id if request else None,
                        run_id=request.run_id if request else None,
                    )
                    if session.graph_version == "learning-session-practice-v1":
                        practice = self.app.practice_workflows.last_in(db, workflow)
                        if practice:
                            from .workflow_practice import validate_practice_in

                            validate_practice_in(db, practice)
                            assessment = self.app.practice_workflows.assessment_in(db, practice)
                            session_fields.update(
                                practice_id=practice.id,
                                practice_status=practice.status,
                                attempt_id=practice.attempt_id,
                                specification_hash=practice.specification_hash,
                                submission_hash=practice.submission.get("submission_hash"),
                                assessment_request_id=assessment.id if assessment else None,
                                assessment_status=assessment.status if assessment else None,
                                competence_verified=False,
                            )
                        if session.wait_kind in {
                            "practice_answer",
                            "practice_feedback",
                            "assessment_authorization",
                        }:
                            execution, waiting = "waiting_user", session.wait_kind
                        elif session.wait_kind == "practice_reconciliation":
                            execution, waiting = "blocked", "needs_reconciliation"
                    if session.wait_kind in {"followup", "authorization"}:
                        execution = "waiting_user"
                        waiting = (
                            "followup"
                            if session.wait_kind == "followup"
                            else "authorize_tutor_request"
                        )
                    elif session.wait_kind == "selection":
                        waiting = "select_or_admit_accepted_plan"
                        execution = "waiting_user"
                    elif session.wait_kind == "reconciliation":
                        waiting = "needs_reconciliation"
                        execution = "blocked"
                    elif session.wait_kind == "tutor":
                        waiting = "await_tutor"
                    if request and request.failure_code:
                        error = request.failure_code
                if control in {"paused", "pause_requested"}:
                    execution = "paused" if control == "paused" else execution
                    waiting = "resume" if control == "paused" else waiting
            elif any(link.milestone == "build" for link, _ in roots):
                raise DomainError("GRAPH_STATE", "已构建的会话缺少执行绑定。", 409)
        from .learning_language import language_prompt, outline_in, read_profile_in

        language_profile = read_profile_in(db, actor, workflow)
        language_fields = {}
        if language_profile:
            language_fields = {
                "language_profile": language_profile,
                "learning_knowledge_outline": outline_in(db, actor, workflow),
                "language_question": language_prompt(language_profile)
                if language_profile["status"] == "awaiting_user_choice"
                else None,
            }
        return {
            **language_fields,
            **(
                {
                    "research_batch": {
                        "contract": workflow.target["jd_research_batch"]["contract"],
                        "requirement_count": len(
                            workflow.target["jd_research_batch"]["requirement_ids"]
                        ),
                        "remaining_requirement_count": len(
                            workflow.target["jd_research_batch"]["remaining_requirement_ids"]
                        ),
                        "topic_keys": workflow.target["jd_research_batch"]["topic_keys"],
                    }
                }
                if workflow.target.get("jd_research_batch")
                else {}
            ),
            **session_fields,
            **(
                {
                    "user_objective_coverage": [
                        {
                            key: item[key]
                            for key in (
                                "objective_id",
                                "origin",
                                "target_depth",
                                "learning_priority",
                                "material_status",
                                "target_depth_verified",
                                "competence_verified",
                            )
                            if key in item
                        }
                        for item in workflow.target.get("user_objective_coverage", [])
                    ]
                }
                if workflow.target.get("goal_build_scope")
                else {}
            ),
            "conversation_id": conversation.id,
            "focus_revision": conversation.revision,
            "workflow_id": workflow.id,
            "workflow_revision": workflow.revision,
            "goal_version_id": workflow.goal_version_id,
            "goal_addendum_id": workflow.target.get("goal_addendum_id"),
            "source_jd_revision_id": workflow.source_jd_revision_id,
            "control_state": control,
            "execution_state": execution,
            "milestone": workflow.milestone,
            "active_stages": stages,
            "completed_stages": list(dict.fromkeys(completed)),
            "publication_count": len(publication_ids),
            "coverage": coverage,
            "calls_consumed": sum(root.calls_consumed for _, root in roots),
            "calls_held": sum(root.held_calls for _, root in roots),
            "call_cap": sum(root.call_cap for _, root in roots),
            "last_error_code": error,
            "waiting_user_action": waiting,
            "run_refs": run_refs,
            "next_action": waiting or ("wait_for_run" if stages else None),
            "pending_confirmation": (
                workflow.target.get("proposal", {}).get("version_id")
                if waiting == "accept_plan"
                else None
            ),
            "coverage_gaps": workflow.target.get("coverage_gaps", []),
            "target": workflow.target.get("target"),
            "source_ids": workflow.target.get("source_ids", []),
            **(
                {
                    "project_pool": {
                        "entry_ids": workflow.target.get("project_entry_ids", []),
                        "priority_count": workflow.target.get("project_priority_count", 0),
                        "gaps": workflow.target.get("project_pool_gaps", []),
                        "knowledge_publications_created": False,
                    }
                }
                if workflow.target.get("project_entry_ids") is not None
                else {}
            ),
        }
