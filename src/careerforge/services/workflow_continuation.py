"""Delegate confirmed milestones to the existing extraction/build/learning services."""

import re

from sqlalchemy import select

from ..contracts import FeedbackInput, KnowledgeBuildInput
from ..contracts_learning_ext import ActivityInput, AttemptInput
from ..contracts_session_plan_v166 import SessionPlanInput, SessionPreferences
from ..contracts_session_tutor_v166 import SessionTutorTurnInput
from ..db import aware, clock, locked
from ..errors import DomainError
from ..models import (
    AgentRun,
    FundingAgreement,
    GoalVersion,
    LearningPlan,
    Outbox,
    PlanVersion,
    Principal,
    Publication,
    StageArtifact,
    Turn,
    UnitVersion,
)
from ..models_dialogue import (
    ConversationActivityEvent,
    GoalAddendum,
    WorkflowRunLink,
    WorkflowSession,
)
from ..util import fingerprint
from .consumer_context import CONSUMER_CONTEXT_CONTRACT, standalone_fact_ids
from .jd_topics import mapped_learning_topics
from .learning_ext import _profile
from .research_coverage import read_goal_version
from .runtime import TERMINAL
from .taxonomy import visible_topics
from .workflow_status import workflow_runs

_SUCCESS = {"succeeded", "partially_succeeded"}


def validate_dialogue_goal(db, actor, scope):
    workflow = db.get(WorkflowSession, scope.get("workflow_id"))
    if (
        workflow is None
        or workflow.principal_id != actor
        or workflow.goal_version_id != scope.get("goal_version_id")
    ):
        raise DomainError("WORKFLOW_GOAL_SCOPE", "学习计划与工作流冻结目标不一致。", 409)
    if scope.get("goal_addendum_id"):
        item = db.get(GoalAddendum, scope["goal_addendum_id"])
        if (
            item is None
            or item.workflow_id != workflow.id
            or item.author_id != actor
            or item.manifest_hash != fingerprint(item.manifest)
            or item.manifest != scope.get("addendum")
        ):
            raise DomainError("WORKFLOW_GOAL_SCOPE", "学习增补的归属或内容已变化。", 409)
    if workflow.goal_version_id:
        version = db.get(GoalVersion, workflow.goal_version_id)
        if version and version.origin_contract == "command-goal-v1":
            from .goal_build_scope import validate_goal_build_scope_in

            goal = read_goal_version(db, actor, workflow.goal_version_id)
            frozen = validate_goal_build_scope_in(
                db, actor, scope.get("goal_build_scope"), goal_snapshot=goal
            )
            if (
                scope.get("contract") != "dialogue-goal-amendment-v1"
                or scope.get("version_hash") != goal["version_hash"]
                or frozen != workflow.target.get("goal_build_scope")
            ):
                raise DomainError(
                    "WORKFLOW_GOAL_SCOPE", "计划未绑定用户新增目标的精确构建范围。", 409
                )
    return workflow.goal_version_id


def _result(db, run):
    item = db.scalar(
        select(StageArtifact).where(
            StageArtifact.run_id == run.id,
            StageArtifact.stage_key == "result",
            StageArtifact.input_hash == run.input_hash,
        )
    )
    return item.payload if item else {}


def _runs_for(db, workflow, milestone):
    return [run for link, run in workflow_runs(db, workflow.id) if link.milestone == milestone]


def _wait(workflow, milestone, user_action=None, **target):
    workflow.target = {**workflow.target, **target, "waiting_user_action": user_action}
    workflow.milestone = milestone
    workflow.revision += 1


class WorkflowContinuation:
    def __init__(self, app):
        self.app, self.db, self.workflows = app, app.db, app.workflows

    def consume(self, message_id, run_id):
        with self.db.transaction() as db:
            link = db.scalar(select(WorkflowRunLink).where(WorkflowRunLink.run_id == run_id))
            workflow = db.get(WorkflowSession, link.workflow_id) if link else None
            if workflow is None:
                return
            actor = workflow.principal_id
            locked(db, Principal, actor)
            try:
                conv = self.workflows.conversation(db, actor, workflow.conversation_id, lock=True)
                workflow = self.workflows.owned_in(db, conv, workflow.id)
                db.refresh(workflow)
            except DomainError:
                message = locked(db, Outbox, message_id)
                if message is not None:
                    message.delivered = True
                return
            message = locked(db, Outbox, message_id)
            if message is None or message.delivered:
                return
            key = f"milestone-observed:{run_id}"
            if db.scalar(
                select(ConversationActivityEvent.id).where(
                    ConversationActivityEvent.conversation_id == conv.id,
                    ConversationActivityEvent.origin_key == key,
                )
            ):
                message.delivered = True
                return
            run = db.get(AgentRun, run_id)
            if run is None or run.status not in TERMINAL:
                return
            try:
                with db.begin_nested():
                    if link.milestone in {"evolution_candidate", "evolution_replay"}:
                        from ..models_dialogue import WorkflowEvolutionRequest

                        request = db.scalar(
                            select(WorkflowEvolutionRequest).where(
                                WorkflowEvolutionRequest.run_id == run.id
                            )
                        )
                        current = self.app.session_workflows.current(db, workflow)
                        from .evolution_workflow import validate_run_binding_in

                        validate_run_binding_in(
                            self.app, db, actor, workflow.namespace_id, run.input_manifest, run=run
                        )
                        if request is None or current is None or request.execution_id != current.id:
                            raise DomainError("EVOLUTION_GRAPH_BINDING", "演化运行映射缺失。", 409)
                        self.app.evolution_workflows.wake_in(
                            db, workflow, current, "evolution_terminal"
                        )
                    elif (
                        link.milestone == "assessment"
                        and run.graph_version == "learning-assessment-v1"
                    ):
                        from ..models_dialogue import (
                            WorkflowAssessmentRequest,
                            WorkflowPracticeRequest,
                        )

                        request = db.scalar(
                            select(WorkflowAssessmentRequest).where(
                                WorkflowAssessmentRequest.run_id == run.id
                            )
                        )
                        practice = (
                            db.get(WorkflowPracticeRequest, request.practice_id)
                            if request
                            else None
                        )
                        current = self.app.session_workflows.current(db, workflow)
                        if not practice or practice.workflow_id != workflow.id:
                            raise DomainError("PRACTICE_BINDING", "评分映射缺失。", 409)
                        if current and current.id == practice.execution_id:
                            self.app.learning_workflows.wake_in(
                                db, workflow, current, "assessment_terminal"
                            )
                    elif link.milestone == "tutor" and run.graph_version == "learning-tutor-v1":
                        from ..models_dialogue import WorkflowTutorRequest

                        request = db.scalar(
                            select(WorkflowTutorRequest).where(
                                WorkflowTutorRequest.run_id == run.id
                            )
                        )
                        if request is None or request.workflow_id != workflow.id:
                            raise DomainError("LEARNING_REQUEST_BINDING", "导师请求映射缺失。", 409)
                        current = self.app.session_workflows.current(db, workflow)
                        if current and current.id == request.execution_id:
                            self.app.learning_workflows.wake_in(
                                db, workflow, current, "tutor_terminal"
                            )
                    elif workflow.orchestration_engine == "langgraph" and link.milestone in {
                        "build",
                        "plan",
                    }:
                        self.app.session_workflows.wake_in(db, workflow, kind="milestone")
                    else:
                        self._observe(db, actor, conv, workflow, link.milestone, run)
            except DomainError as exc:
                _wait(
                    workflow,
                    workflow.milestone,
                    "resolve_workflow_blocker",
                    continuation_error=exc.code,
                )
                self.workflows.emit_in(
                    db,
                    conv,
                    "workflow.stage_failed",
                    f"milestone-failed:{run.id}",
                    workflow,
                    run_id=run.id,
                    code=exc.code,
                )
            self.workflows.emit_in(
                db,
                conv,
                "workflow.stage_observed",
                key,
                workflow,
                run_id=run.id,
                milestone=link.milestone,
                status=run.status,
            )
            message.delivered = True

    def _observe(self, db, actor, conv, workflow, milestone, run):
        result = _result(db, run)
        if milestone == "build":
            aggregated = self.aggregate_build_results_in(db, actor, conv, workflow)
            if (
                aggregated
                and workflow.control_state == "active"
                and workflow.target.get("publication_ids")
                and not workflow.target.get("desk_project_question")
            ):
                self.propose_plan(db, actor, conv, workflow)
            return
        if run.status not in _SUCCESS:
            _wait(workflow, workflow.milestone, "review_run_failure", failed_run_id=run.id)
            return
        if milestone == "project_discovery":
            _wait(
                workflow,
                "awaiting_project_selection",
                "select_projects_for_analysis",
                project_entry_ids=[e["entry_id"] for e in result.get("project_entries", [])],
                project_priority_count=result.get("priority_count", 0),
                project_pool_gaps=result.get("gaps", []),
            )
            if (
                workflow.target.get("build_after_discovery") == run.id
                and workflow.control_state == "active"
                and conv.focus.get("active_workflow_id") == workflow.id
            ):
                from ..contracts_conversation_actions_v1 import DialogueMessage

                primary = set(result.get("primary_entry_ids", []))
                chosen = [
                    entry["source_id"]
                    for entry in result.get("project_entries", [])
                    if entry["entry_id"] in primary
                ][:2]
                if chosen:
                    self.start_build(
                        db,
                        actor,
                        conv,
                        workflow,
                        DialogueMessage(
                            text="根据已筛选项目构建学习知识",
                            expected_focus_revision=conv.revision,
                            source_ids=chosen,
                            authorization_ref=run.funding_id,
                            idempotency_key="discovery-build:" + run.id,
                        ),
                    )
            return
        if milestone == "extract":
            goal_id = result.get("goal_version_id")
            if not goal_id:
                raise DomainError("WORKFLOW_GOAL_MISSING", "提取任务没有有效目标版本。", 409)
            read_goal_version(db, actor, goal_id)
            if (
                result.get("jd_revision_id") != workflow.source_jd_revision_id
                or run.input_manifest.get("jd_revision_id") != workflow.source_jd_revision_id
            ):
                raise DomainError("WORKFLOW_GOAL_SCOPE", "提取结果未绑定到导入的 JD。", 409)
            workflow.goal_version_id = goal_id
            _wait(workflow, "awaiting_goal_confirmation", "confirm_goal")
            from .learning_language import initialize_language_in, store_outline_in

            profile = initialize_language_in(db, actor, workflow)
            if profile and profile["status"] == "awaiting_user_choice":
                _wait(workflow, "awaiting_language_choice", "select_implementation_language")
                from .learning_language import language_prompt

                self.workflows.emit_in(
                    db,
                    conv,
                    "workflow.language_choice_required",
                    "language-choice:" + goal_id,
                    workflow,
                    question=language_prompt(profile),
                )
            elif profile:
                store_outline_in(db, actor, workflow, profile)
            if conv.focus.get("active_workflow_id") == workflow.id:
                conv.focus = {**conv.focus, "active_goal_version_id": goal_id}
        elif milestone == "plan":
            self.observe_plan_result_in(db, actor, conv, workflow, run)
        elif milestone == "assessment":
            # Persist the model's provisional feedback separately from mastery.
            _wait(workflow, "feedback", "acknowledge_module", assessment_run_id=run.id)
        elif milestone == "tutor":
            # Turn completion is independent of coverage and learner competence.
            if workflow.target.get("accepted_plan"):
                turn = db.scalar(select(Turn).where(Turn.run_id == run.id))
                if turn and turn.status == "completed":
                    _wait(workflow, "tutoring", None, last_tutor_turn_id=turn.id)

    def aggregate_build_results_in(self, db, actor, conv, workflow):
        builds = _runs_for(db, workflow, "build")
        if workflow.target.get("build_results_finalized") or any(
            item.status not in TERMINAL for item in builds
        ):
            return False
        results = [_result(db, item) for item in builds if item.status in _SUCCESS]
        ids = list(workflow.target.get("publication_ids", []))
        for saved in results:
            ids.extend(saved.get("publication_ids", []))
        valid = self.publications(db, actor, ids)
        complete = (
            not workflow.target.get("research_backlog_requirement_ids")
            and bool(results)
            and len(results) == len(builds)
            and all(saved.get("task_coverage", {}).get("status") == "complete" for saved in results)
            and set(ids) == set(valid)
        )
        coverage = "complete" if valid and complete else "partial" if valid else "unknown"
        scope = workflow.target.get("goal_build_scope")
        objective_status = {}
        if scope is not None:
            from .goal_build_scope import validate_goal_build_scope_in
            from .goal_plan_v1 import objective_coverage

            scope = validate_goal_build_scope_in(db, actor, scope)
            objective_status["user_objective_coverage"] = objective_coverage(
                read_goal_version(db, actor, workflow.goal_version_id),
                publication_ids=valid,
                selected_id=scope["user_objective_id"],
                scope_hash=scope["scope_hash"],
                task_complete=complete,
            )
            # Selected question completion never certifies the entire amended goal.
            coverage = "partial" if valid else "unknown"
        gaps = list(
            dict.fromkeys(
                [gap for saved in results for gap in saved.get("gaps", [])]
                + (["build_unavailable"] if len(results) != len(builds) else [])
            )
        )
        _wait(
            workflow,
            "building",
            None if valid else "reviewed_evidence_required",
            publication_ids=valid,
            coverage=coverage,
            coverage_gaps=gaps,
            build_results_finalized=True,
            **objective_status,
        )
        return True

    def observe_plan_result_in(self, db, actor, conv, workflow, run):
        result = _result(db, run)
        if workflow.target.get("accepted_plan"):
            return
        existing = workflow.target.get("proposal")
        if existing and existing.get("run_id") != run.id:
            raise DomainError("WORKFLOW_PLAN_SCOPE", "旧计划不能替换当前提案。", 409)
        self.app.learning.validate_plan_result(db, actor, run.id, result)
        plan_id, version_id = result["plan_id"], result["version_id"]
        proposal = {
            "plan_id": plan_id,
            "version_id": version_id,
            "revision": result["revision"],
            "run_id": run.id,
        }
        _wait(workflow, "awaiting_plan_acceptance", "accept_plan", proposal=proposal)
        self.workflows.emit_in(
            db,
            conv,
            "workflow.awaiting_confirmation",
            f"plan-proposed:{run.id}",
            workflow,
            plan_id=plan_id,
            version_id=version_id,
        )

    def publications(self, db, actor, ids, *, topic_keys=()):
        valid = []
        for publication_id in dict.fromkeys(ids):
            try:
                pack = self.app.knowledge.publication_pack(
                    db, actor, publication_id, consumer_context_contract=CONSUMER_CONTEXT_CONTRACT
                )
            except DomainError:
                continue
            if standalone_fact_ids(pack) and (
                not topic_keys
                or any(link["topic_key"] in topic_keys for link in pack["topic_links"])
            ):
                valid.append(publication_id)
        return valid[:20]

    def _topics(self, db, actor, workflow):
        rows = visible_topics(db, actor)
        named = list(workflow.target.get("user_topics", []))
        if workflow.target.get("entry") == "set_learning_topic":
            named.append(workflow.target["target"])
        explicit = [
            row.key
            for row in rows
            if any(
                name.casefold()
                in {row.key.casefold(), *(alias.casefold() for alias in row.aliases)}
                for name in named
            )
        ]
        if workflow.target.get("goal_addendum_id"):
            # Only the added scope is eligible for a new build; the original goal is kept for planning.
            explicit = [
                row.key
                for row in rows
                if any(
                    value.casefold()
                    in {row.key.casefold(), *(alias.casefold() for alias in row.aliases)}
                    for value in workflow.target.get("user_topics", [])[-1:]
                )
            ]
            return explicit
        if workflow.goal_version_id:
            snapshot = read_goal_version(db, actor, workflow.goal_version_id)
            explicit.extend(mapped_learning_topics(snapshot))
        return list(dict.fromkeys(explicit))[:15]

    def _funding(self, db, actor, workflow, reference):
        if not reference:
            return None
        row = db.get(FundingAgreement, reference)
        if row is None or row.payer_id != actor or row.namespace_id != workflow.namespace_id:
            raise DomainError("NOT_FOUND", "未找到当前工作流的授权。", 404)
        return row if row.status == "active" and aware(row.expires_at) > clock(db) else None

    def retry_build(self, db, actor, conv, parent, message):
        previous = _runs_for(db, parent, "build")
        if not previous or any(run.status not in TERMINAL for run in previous):
            raise DomainError("RESEARCH_RETRY_STATE", "等待原构建结束后才能另行补证。", 409)
        if parent.target.get("accepted_plan") or parent.target.get("proposal"):
            raise DomainError(
                "RESEARCH_RETRY_STATE", "已有计划，请先完成当前确认或明确修订目标。", 409
            )
        if self.publications(db, actor, parent.target.get("publication_ids", [])):
            raise DomainError("RESEARCH_RETRY_STATE", "已有审核知识，请先查看知识与计划缺口。", 409)
        if self._funding(db, actor, parent, message.authorization_ref) is None:
            return parent, {
                "status": "waiting_confirmation",
                "reply": "请明确确认本次补证的来源、授权与额度。",
            }
        if not (message.source_id or message.source_ids):
            raise DomainError("RESEARCH_RETRY_SOURCE", "补证需明确指定本次已固定的源码。", 422)
        old_sources = set()
        for run in previous:
            saved = self.app.runtime.stage(run.id, "sources") or {}
            old_sources.update(saved.get("source_ids", []))
            if run.input_manifest.get("source_id"):
                old_sources.add(run.input_manifest["source_id"])
        selected = message.source_ids or [message.source_id]
        if not set(selected) <= old_sources:
            raise DomainError(
                "RESEARCH_RETRY_SOURCE", "来源不属于原构建的固定快照，请另建研究目标。", 409
            )
        version = db.get(GoalVersion, parent.goal_version_id) if parent.goal_version_id else None
        if parent.target.get("goal_addendum_id") or (
            version and version.origin_contract == "command-goal-v1"
        ):
            raise DomainError(
                "RESEARCH_RETRY_SCOPE",
                "新增或修订目标须在原确认范围内处理，不支持跨工作流补证。",
                409,
            )
        child = self.workflows.create_in(db, conv, parent=parent)
        child.goal_version_id = parent.goal_version_id
        child.source_jd_revision_id = parent.source_jd_revision_id
        child.target = {
            "target": previous[-1].input_manifest["question"],
            "research_retry_of": parent.id,
            **{
                key: parent.target[key]
                for key in ("goal_addendum_id", "user_topics")
                if key in parent.target
            },
        }
        result = self.start_build(db, actor, conv, child, message)
        return child, result

    def start_project_discovery(self, db, actor, conv, workflow, message):
        from .learning_language import require_language_in
        from .project_pool import ProjectPoolService

        if not workflow.goal_version_id:
            raise DomainError("GOAL_REQUIRED", "请先确认岗位目标。", 409)
        require_language_in(db, actor, workflow)
        old = _runs_for(db, workflow, "project_discovery")
        if old:
            return {"run_refs": [r.id for r in old], "reply": "已绑定本轮项目检索与入库任务。"}
        funding = self._funding(db, actor, workflow, message.authorization_ref)
        if funding is None:
            return {"status": "waiting_confirmation", "reply": "项目检索入库需要公开来源访问授权。"}
        if not {"public_source", "private_source", "private_jd", "learning_state"} <= set(
            funding.allowed_data_classes
        ):
            raise DomainError("GITHUB_EGRESS", "项目检索需要目标、学习状态与来源的访问授权。", 403)
        preference = re.search(
            r"[，,]优先\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?=[，,。]|$)", message.text
        )
        options = (
            {"repository_urls": ["https://github.com/" + preference.group(1)], "target_count": 1}
            if preference
            else {}
        )
        identifier = ProjectPoolService(self.app).start_in(
            db, actor, workflow, funding.id, "dialogue.projects:" + workflow.id, **options
        )
        build_requested = bool(re.search(r"[，,]并构建经过审核的学习知识[。]?$", message.text))
        if build_requested:
            workflow.target = {**workflow.target, "build_after_discovery": identifier}
        self.workflows.link_in(db, workflow, db.get(AgentRun, identifier), "project_discovery")
        _wait(workflow, "discovering_projects", None)
        return {
            "run_refs": [identifier],
            "reply": "已启动项目调研，通过初审的源码将进入知识构建。"
            if build_requested
            else "已启动多项目检索与质量初审入库；优先工程质量和学习价值，不要求完全匹配技术栈。本轮不启动源码分析或知识发布。",
        }

    def start_build(self, db, actor, conv, workflow, message):
        from .learning_language import require_language_in

        language_profile = require_language_in(db, actor, workflow)
        version = (
            db.get(GoalVersion, workflow.goal_version_id) if workflow.goal_version_id else None
        )
        amended = bool(version and version.origin_contract == "command-goal-v1")
        if amended:
            current = self.app.session_workflows.current(db, workflow)
            if current:
                self.app.session_workflows.validate_in(db, workflow, current)
                admitted = current.binding_payload["admitted_scope"]
                scope = admitted["goal_build_scope"]
                requested_sources = message.source_ids or (
                    [message.source_id] if message.source_id else []
                )
                if (
                    (requested_sources and requested_sources != scope["source_ids"])
                    or (
                        message.research_question is not None
                        and message.research_question != scope["original_question"]
                    )
                    or (
                        "build_call_cap" in message.model_fields_set
                        and message.build_call_cap != admitted["build_call_cap"]
                    )
                    or (
                        "build_wall_time_contract" in message.model_fields_set
                        and message.build_wall_time_contract
                        != admitted.get("build_wall_time_contract")
                    )
                    or (
                        "auto_evidence_retry" in message.model_fields_set
                        and message.auto_evidence_retry != admitted["auto_evidence_retry"]
                    )
                ):
                    raise DomainError(
                        "WORKFLOW_GOAL_SCOPE", "已确认的新增目标执行范围不可改写。", 409
                    )
                return {
                    "run_refs": [run.id for run in _runs_for(db, workflow, "build")],
                    "execution_id": current.id,
                    "reused_publication_ids": [
                        item["publication_id"] for item in admitted["reuse_selection"]
                    ],
                    "reply": "已绑定原有新增目标执行；不会重复构建或重新选择知识。",
                }
        old = _runs_for(db, workflow, "build")
        if old:
            if "build_wall_time_contract" in message.model_fields_set and any(
                run.input_manifest.get("build_wall_time_contract")
                != message.build_wall_time_contract
                for run in old
            ):
                raise DomainError(
                    "IDEMPOTENCY_CONFLICT", "既有构建不得更改时限合同；须新建工作流。", 409
                )
            return {"run_refs": [run.id for run in old], "reply": "已绑定当前构建。"}
        if workflow.source_jd_revision_id and not workflow.goal_version_id:
            if workflow.parent_workflow_id:
                prior = self.workflows.owned_in(db, conv, workflow.parent_workflow_id)
                if (
                    prior.source_jd_revision_id == workflow.source_jd_revision_id
                    and prior.goal_version_id
                ):
                    workflow.goal_version_id = prior.goal_version_id
            if not workflow.goal_version_id:
                return {"status": "waiting_confirmation", "reply": "等待 JD 提取完成后确认。"}
        funding = self._funding(db, actor, workflow, message.authorization_ref)
        if funding is None:
            return {"status": "waiting_confirmation", "reply": "请确认这次构建的授权和预算。"}
        sources = message.source_ids or (
            [message.source_id] if message.source_id else workflow.target.get("source_ids", [])
        )
        if not sources and workflow.target.get("source_id"):
            sources = [workflow.target["source_id"]]
        repository = (
            workflow.target.get("target")
            if workflow.target.get("entry") == "set_repository"
            else None
        )
        if message.research_question is not None and (sources or repository):
            raise DomainError("RESEARCH_SCOPE", "研究问题不能与固定来源或直接仓库快照混用。", 422)
        if message.auto_evidence_retry and (len(sources) != 1 or repository):
            raise DomainError("EVIDENCE_RETRY_SCOPE", "自动补证需要一个已授权固定来源。", 422)
        goal_scope = None
        if amended:
            from .goal_build_scope import freeze_goal_build_scope_in

            try:
                goal_scope = freeze_goal_build_scope_in(
                    db,
                    actor,
                    workflow,
                    source_ids=sources,
                    repository_url=repository,
                    original_question=message.research_question,
                )
            except DomainError as exc:
                if exc.code != "GOAL_MAPPING_PENDING":
                    raise
                _wait(workflow, "awaiting_goal_confirmation", "confirm_topic")
                return {
                    "status": "waiting_confirmation",
                    "failure_code": exc.code,
                    "reply": exc.message,
                }
            topics = [item["topic_key"] for item in goal_scope["topic_refs"]]
        else:
            topics = self._topics(db, actor, workflow)
        research_batch = None
        goal_question = None
        if not amended and workflow.goal_version_id and language_profile:
            snapshot = read_goal_version(db, actor, workflow.goal_version_id)
            from .jd_topics import requires_rank_fusion

            rank_fusion = requires_rank_fusion(snapshot)
            if rank_fusion or workflow.target.get("build_after_discovery"):
                goal_question = "依据固定源码解释岗位要求中的具体机制：" + "；".join(
                    dict.fromkeys(
                        row["quote"]
                        for row in snapshot["requirements"]
                        if row.get("modality") != "not_required"
                    )
                )
                if rank_fusion:
                    goal_question = (
                        "优先解释排名融合的输入、合并规则及输出，给出实现引用和边界。"
                        + goal_question
                    )
                    workflow.target = {**workflow.target, "requires_rank_fusion": True}
            if len(snapshot["requirements"]) > 30 or len(topics) > 15:
                from .jd_research_batch import first_batch

                research_batch = first_batch(snapshot, language_profile)
                topics = research_batch["topic_keys"]
                workflow.target = {
                    **workflow.target,
                    "jd_research_batch": research_batch,
                    "research_backlog_requirement_ids": research_batch["remaining_requirement_ids"],
                }
        if workflow.target.get("goal_addendum_id") and not topics:
            _wait(workflow, "awaiting_goal_confirmation", "confirm_topic")
            return {
                "status": "waiting_confirmation",
                "reply": "新增技术尚未映射到已登记主题，请确认具体方向。",
            }
        if not sources and not repository and not topics:
            _wait(workflow, "awaiting_goal_confirmation", "select_source")
            return {
                "status": "waiting_confirmation",
                "reply": "请指定固定源码或明确的 GitHub 仓库。",
            }
        for source in sources:
            self.app.sources.get(db, actor, source)
        if not sources and "public_source" not in funding.allowed_data_classes:
            raise DomainError("EGRESS_OR_ROLE_DENIED", "仓库检索需公开来源授权。", 403)
        workflow.target = {
            **workflow.target,
            "funding_ref": funding.id,
            "source_ids": sources,
            "source_id": sources[0] if len(sources) == 1 else None,
        }
        if goal_scope:
            workflow.target = {**workflow.target, "goal_build_scope": goal_scope}
        if conv.focus.get("active_workflow_id") == workflow.id:
            self.workflows.focus_in(db, conv, workflow)
        q = (
            message.research_question
            or workflow.target.get("target")
            or (goal_question or "")[:6000]
            or "按已确认岗位要求，研究可核验的项目机制"
        )
        if message.research_question is None and workflow.target.get("user_topics"):
            q += "；本轮只新增研究 " + workflow.target["user_topics"][-1]
        if goal_scope:
            q = goal_scope["original_question"]
        reused, runs, reuse_selection = [], [], []
        for source in sources or [None]:
            if source:
                published = list(
                    db.scalars(
                        select(Publication.id)
                        .join(UnitVersion, UnitVersion.id == Publication.unit_version_id)
                        .where(UnitVersion.source_id == source)
                        .order_by(Publication.id)
                    )
                )
                valid = self.publications(db, actor, published, topic_keys=topics)
                if workflow.target.get("requires_rank_fusion"):
                    valid = self.rank_fusion_publications(db, actor, valid)
                if valid:
                    if goal_scope:
                        reuse_selection.extend(
                            {
                                "publication_id": pid,
                                "pack_hash": fingerprint(
                                    self.app.knowledge.publication_pack(
                                        db,
                                        actor,
                                        pid,
                                        consumer_context_contract=CONSUMER_CONTEXT_CONTRACT,
                                    )
                                ),
                                "source_id": source,
                                "scope_hash": goal_scope["scope_hash"],
                            }
                            for pid in valid
                        )
                    reused.extend(valid)
                    continue
            run_id = self.app.build.start(
                actor,
                KnowledgeBuildInput(
                    namespace_id=workflow.namespace_id,
                    funding_id=funding.id,
                    goal_version_id=None
                    if workflow.target.get("goal_addendum_id") and not amended
                    else workflow.goal_version_id,
                    source_id=source,
                    repository_url=None if source else repository,
                    topic_keys=topics,
                    question=q,
                    research_scope="focused",
                    required_dimensions=goal_scope["required_dimensions"] if goal_scope else [],
                    auto_evidence_retry=message.auto_evidence_retry,
                    build_wall_time_contract=message.build_wall_time_contract,
                    call_cap=message.build_call_cap,
                    duration_minutes=30,
                    planning_call_seconds=60,
                    max_projects=1,
                    max_units_per_project=1,
                    idempotency_key=f"dialogue.build:{workflow.id}:{source or 'research'}",
                ),
                db=db,
                **({"goal_build_scope": goal_scope} if goal_scope else {}),
                **({"jd_research_batch": research_batch} if research_batch else {}),
            )
            self.workflows.link_in(db, workflow, db.get(AgentRun, run_id), "build")
            runs.append(run_id)
        _wait(
            workflow,
            "building",
            None,
            publication_ids=list(dict.fromkeys(reused)),
            coverage="partial" if reused else "unknown",
        )
        if workflow.orchestration_engine == "langgraph":
            execution = self.app.session_workflows.admit_in(
                db,
                workflow,
                {
                    "source_ids": sources,
                    "repository_url": repository,
                    "topic_keys": topics,
                    "research_scope": "focused",
                    "auto_evidence_retry": message.auto_evidence_retry,
                    **(
                        {"build_wall_time_contract": message.build_wall_time_contract}
                        if message.build_wall_time_contract is not None
                        else {}
                    ),
                    "build_question_hash": fingerprint(q),
                    **(
                        {
                            "goal_build_scope": goal_scope,
                            "reuse_selection": reuse_selection,
                            "build_call_cap": message.build_call_cap,
                        }
                        if goal_scope
                        else {}
                    ),
                },
            )
            return {
                "run_refs": runs,
                "execution_id": execution.id,
                "reply": "已登记固定范围构建，随后生成已审核知识的学习计划。",
            }
        if not runs:
            plan = self.propose_plan(db, actor, conv, workflow)
            return {**plan, "reply": "已复用固定来源的审核知识。 " + plan["reply"]}
        return {"run_refs": runs, "reply": "已开始固定范围的分析和独立审核。"}

    def rank_fusion_publications(self, db, actor, ids):
        from .jd_topics import explains_rank_fusion

        selected = []
        for pid in ids:
            pack = self.app.knowledge.publication_pack(
                db, actor, pid, consumer_context_contract=CONSUMER_CONTEXT_CONTRACT
            )
            if explains_rank_fusion(pack, standalone_fact_ids(pack)):
                selected.append(pid)
        return selected

    def propose_plan(self, db, actor, conv, workflow, explicit_funding=None):
        previous = _runs_for(db, workflow, "plan")
        if workflow.target.get("proposal"):
            return {
                "status": "waiting_confirmation",
                "reply": "请先明确接受当前学习计划。",
                "confirmation_id": workflow.target["proposal"]["version_id"],
            }
        retry_failed = bool(
            previous
            and explicit_funding
            and all(r.status in {"failed", "budget_stopped", "cancelled"} for r in previous)
        )
        if previous and not retry_failed:
            return {
                "run_refs": [run.id for run in previous],
                "reply": "学习计划已在队列中或已结束，可查询进度。",
            }
        valid = self.publications(db, actor, workflow.target.get("publication_ids", []))
        if workflow.target.get("requires_rank_fusion"):
            valid = self.rank_fusion_publications(db, actor, valid)
            if not valid:
                _wait(workflow, "building", "reviewed_evidence_required")
                return {
                    "status": "waiting_confirmation",
                    "reply": "当前已审核事实未覆盖排名融合，请补充对应源码后重试。",
                }
        from .knowledge_implementations import material_matches
        from .learning_language import domain_for, read_profile_in

        language_profile = read_profile_in(db, actor, workflow)
        if language_profile:
            selected = []
            for pid in valid:
                pack = self.app.knowledge.publication_pack(db, actor, pid)
                domains = {
                    domain_for(t["topic_key"])
                    for t in pack["topic_links"]
                    if t["relation"] != "mentions"
                }
                # Ambiguous AI/backend material must satisfy the core AI track.
                domain = "ai_agent" if "ai_agent" in domains else "backend"
                if material_matches(pack, language_profile, domain):
                    selected.append(pid)
            valid = selected
        if not valid:
            return {
                "status": "waiting_confirmation",
                "reply": "还没有可用的已审核知识，先保留证据缺口。",
            }
        funding = self._funding(
            db, actor, workflow, explicit_funding or workflow.target.get("funding_ref")
        )
        if funding is None:
            _wait(workflow, "building", "authorize_plan")
            return {"status": "waiting_confirmation", "reply": "需要有效授权才能创建计划。"}
        item = (
            db.get(GoalAddendum, workflow.target.get("goal_addendum_id"))
            if workflow.target.get("goal_addendum_id")
            else None
        )
        scope = {
            "workflow_id": workflow.id,
            "goal_version_id": workflow.goal_version_id,
            "goal_addendum_id": item.id if item else None,
            "addendum": item.manifest if item else None,
            **(
                {"requires_rank_fusion": True}
                if workflow.target.get("requires_rank_fusion")
                else {}
            ),
            **(
                {"desk_reading_plan": "desk-reading-v1"}
                if workflow.target.get("desk_reading_plan") == "desk-reading-v1"
                else {}
            ),
        }
        version = (
            db.get(GoalVersion, workflow.goal_version_id) if workflow.goal_version_id else None
        )
        if version and version.origin_contract == "command-goal-v1":
            goal = read_goal_version(db, actor, workflow.goal_version_id)
            scope.update(
                contract="dialogue-goal-amendment-v1",
                version_hash=goal["version_hash"],
                goal_build_scope=workflow.target.get("goal_build_scope"),
            )
            validate_dialogue_goal(db, actor, scope)
        profile = _profile(db, actor)
        result = self.app.learning.start_session_plan(
            actor,
            SessionPlanInput(
                namespace_id=workflow.namespace_id,
                funding_id=funding.id,
                publication_ids=valid,
                learning_intent="function",
                title="按已审核项目知识学习",
                total_minutes=25,
                preferences=SessionPreferences(practice=False, requested_style="code_first"),
                expected_profile_revision=profile.revision,
                idempotency_key="dialogue.plan:"
                + workflow.id
                + (":" + str(len(previous)) if retry_failed else ""),
            ),
            db=db,
            dialogue_scope=scope,
        )
        run = db.get(AgentRun, result["run_id"])
        self.workflows.link_in(db, workflow, run, "plan")
        _wait(workflow, "planning", None, funding_ref=funding.id)
        return {"run_refs": [run.id], "reply": "已开始生成学习计划，审核知识缺口会保留。"}

    def accept_plan(self, db, actor, conv, workflow, message):
        if workflow.target.get("accepted_plan"):
            return {"status": "already_applied", "reply": "计划已接受。可说「开始讲解」继续。"}
        proposal = workflow.target.get("proposal")
        if not proposal:
            return {"status": "waiting_confirmation", "reply": "等待有效的学习计划提议。"}
        run = db.get(AgentRun, proposal["run_id"])
        self.app.learning.validate_plan_result(db, actor, run.id, _result(db, run))
        accepted = self.app.learning.accept_plan(
            actor, proposal["plan_id"], proposal["version_id"], proposal["revision"], db=db
        )
        _wait(workflow, "tutoring", None, accepted_plan=accepted)
        self.workflows.emit_in(
            db,
            conv,
            "workflow.tutor_ready",
            f"accepted:{proposal['version_id']}",
            workflow,
            version_id=proposal["version_id"],
        )
        return {
            "reply": "已接受明确的计划版本。",
            "plan_id": proposal["plan_id"],
            **(
                {"version_id": proposal["version_id"], "proposal_run_id": proposal["run_id"]}
                if workflow.orchestration_engine == "langgraph"
                else {}
            ),
        }

    def tutor(self, db, actor, conv, workflow, message, *, question=None):
        accepted = workflow.target.get("accepted_plan")
        if not accepted:
            return {"status": "waiting_confirmation", "reply": "请先明确接受学习计划。"}
        plan = db.get(LearningPlan, accepted["plan_id"])
        profile = _profile(db, actor)
        if (
            not plan
            or plan.active_version_id != accepted["active_version_id"]
            or profile.active_plan_id != plan.id
        ):
            raise DomainError("WORKFLOW_PLAN_SCOPE", "请切回已接受的学习模块再开始讲解。", 409)
        version = db.get(PlanVersion, plan.active_version_id)
        if profile.active_module_id not in version.module_ids:
            raise DomainError("WORKFLOW_PLAN_SCOPE", "当前模块不属于这份计划。", 409)
        funding = self._funding(
            db, actor, workflow, message.authorization_ref or workflow.target.get("funding_ref")
        )
        if funding is None:
            _wait(workflow, "tutoring", "authorize_tutor")
            return {"status": "waiting_confirmation", "reply": "计划已接受，请确认讲解授权。"}
        parts = re.split(r"[，,]", message.text, maxsplit=1)
        if message.text.startswith(("请根据已发布知识解释", "根据已发布知识解释")):
            question = question or message.text
        question = question or (
            parts[1].strip()
            if len(parts) == 2
            else "请基于已审核源码讲解当前模块的关键条件与适用边界。"
        )
        tutor_input = SessionTutorTurnInput(
            question=question or "请基于已审核源码讲解当前模块的关键条件与适用边界。",
            namespace_id=workflow.namespace_id,
            plan_id=plan.id,
            version_id=version.id,
            module_id=profile.active_module_id,
            expected_plan_revision=plan.revision,
            expected_revision=conv.revision,
            funding_id=funding.id,
            idempotency_key="dialogue.tutor:" + fingerprint(message.idempotency_key),
            preferences=SessionPreferences(practice=False, requested_style="code_first"),
        )
        result = self.app.conversations._submit(
            actor, conv.id, tutor_input, db=db, session_value=tutor_input
        )
        self.workflows.link_in(db, workflow, db.get(AgentRun, result["run_id"]), "tutor")
        _wait(workflow, "tutoring")
        return {
            "reply": "已开始当前模块讲解。",
            "turn_id": result["turn_id"],
            "run_id": result["run_id"],
            "run_refs": [result["run_id"]],
        }

    def practice(self, db, actor, workflow, message):
        from ..models_learning_ext import LearningAttempt

        old = (
            db.get(LearningAttempt, workflow.target.get("attempt_id"))
            if workflow.target.get("attempt_id")
            else None
        )
        if old and old.state == "started":
            return {
                "status": "already_applied",
                "reply": "当前练习还未提交，请输入“提交答案：你的解释”。",
                "attempt": {"attempt_id": old.id},
            }
        accepted = workflow.target.get("accepted_plan")
        profile = _profile(db, actor)
        if (
            not accepted
            or not profile.active_module_id
            or profile.active_plan_id != accepted["plan_id"]
        ):
            return {"status": "waiting_confirmation", "reply": "请先接受当前工作流的学习计划。"}
        activity = self.app.learning.create_activity(
            actor,
            ActivityInput(
                module_id=profile.active_module_id,
                kind="understanding_check",
                explicitly_requested=True,
            ),
            db=db,
        )
        attempt = self.app.learning.start_attempt(
            actor,
            activity["activity_id"],
            AttemptInput(
                idempotency_key="dialogue.practice:" + fingerprint(message.idempotency_key),
            ),
            db=db,
        )
        _wait(
            workflow,
            "practicing",
            "submit_practice",
            activity_id=activity["activity_id"],
            attempt_id=attempt["attempt_id"],
        )
        return {
            "reply": "已创建理解练习，请提交你的解释。执行代码需要另行批准。",
            "activity": activity,
            "attempt": attempt,
        }

    def feedback(self, db, actor, conv, workflow, message):
        if message.observation:
            attempt = workflow.target.get("attempt_id")
            if not attempt:
                return {"status": "waiting_confirmation", "reply": "请先完成当前工作流中的练习。"}
            result = self.app.learning.record_observation(
                actor,
                attempt,
                message.observation,
                db=db,
            )
            _wait(workflow, "feedback", None)
            return {"reply": "已保存这次练习的观察。", "observation": result}
        turn = (
            db.get(Turn, workflow.target.get("last_tutor_turn_id"))
            if workflow.target.get("last_tutor_turn_id")
            else None
        )
        if not turn or turn.conversation_id != conv.id or turn.status != "completed":
            return {"status": "waiting_confirmation", "reply": "请指定需要反馈的已完成回答。"}
        result = self.app.evolution.feedback(
            actor,
            FeedbackInput(
                turn_id=turn.id,
                signal="explicit_feedback",
                content=message.text,
                consent=False,
            ),
            db=db,
        )
        return {"reply": "已记录对本次讲解的反馈。你可以告诉我卡在哪个步骤。", "feedback": result}
