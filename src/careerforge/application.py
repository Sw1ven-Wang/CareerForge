from __future__ import annotations

import logging
from uuid import uuid4

from sqlalchemy import select

from .adapters.cache import MemoryCache, RedisCache
from .adapters.github import GitHubProvider
from .adapters.model import ModelGateway, OpenAICompatibleProvider
from .config import Settings
from .db import Database
from .errors import DomainError, GraphCheckpointUnavailable, LeaseLost, PauseBoundary
from .lease_heartbeat import LeaseHeartbeat
from .models import AgentRun, Namespace, Outbox, Principal
from .services.answers import AnswerService
from .services.build import BuildService
from .services.dependencies import propagate_batch
from .services.knowledge import KnowledgeService
from .services.orchestration import Coordinator
from .services.research import ResearchService
from .services.retrieval import RetrievalService
from .services.runtime import Runtime
from .services.security import create_principal, private_namespace
from .services.sources import SourceService
from .services.taxonomy import seed

log = logging.getLogger(__name__)


class Application:
    def __init__(
        self,
        settings: Settings,
        provider=None,
        repositories=None,
        cache=None,
        database: Database | None = None,
    ):
        self.settings = settings
        from .observability import setup

        setup(settings.otlp_endpoint)
        self.db = database or Database(settings)
        if (
            provider is not None
            and getattr(provider, "synthetic", False)
            and settings.mode != "test"
        ):
            raise DomainError("FIXTURE_FORBIDDEN", "测试模型只能通过 test 模式显式注入。", 500)
        if (
            repositories is not None
            and getattr(repositories, "synthetic", False)
            and settings.mode != "test"
        ):
            raise DomainError("FIXTURE_FORBIDDEN", "测试搜索不能冒充真实GitHub。", 500)
        self.runtime = Runtime(self.db)
        self.provider = provider or OpenAICompatibleProvider(settings)
        self.runtime.current_model_profile = lambda: self.provider.profile_version
        self.gateway = ModelGateway(self.runtime, self.provider, settings)
        self.sources = SourceService(self.db)
        self.knowledge = KnowledgeService(self.db, self.runtime)
        self.cache = cache or (
            RedisCache(settings.redis_cache_url) if settings.mode == "production" else MemoryCache()
        )
        self.repositories = repositories or GitHubProvider(self.db, settings)
        self.retrieval = RetrievalService(self.db, self.knowledge, self.cache, self.gateway)
        self.research = ResearchService(self.runtime, self.gateway, self.repositories)
        from .services.jd import JDService

        self.jd = JDService(self.db, runtime=self.runtime, gateway=self.gateway)
        self.build = BuildService(
            self.runtime,
            self.gateway,
            self.sources,
            self.knowledge,
            self.retrieval,
            self.research,
            self.repositories,
        )
        self.coordinator = Coordinator(self.db, self.gateway)
        self.answers = AnswerService(self.db, self.runtime, self.gateway, self.knowledge)
        # Import here keeps service dependencies explicit, without cyclic module initialization.
        from .services.coding import CodingService
        from .services.conversations import ConversationService
        from .services.evolution import EvolutionService
        from .services.learning import LearningService

        self.conversations = ConversationService(self)
        from .services.workflow_controller import WorkflowController

        self.workflows = WorkflowController(self)
        from .services.conversation_actions import ActionExecutor

        self.dialogue = ActionExecutor(self)
        from .services.workflow_continuation import WorkflowContinuation

        self.continuation = WorkflowContinuation(self)
        from .services.session_execution import SessionExecutions

        self.session_workflows = SessionExecutions(self)
        from .services.learning_execution import LearningExecutions
        from .services.learning_requests import LearningRequests

        self.learning_requests = LearningRequests(self)
        self.learning_workflows = LearningExecutions(self)
        from .services.workflow_practice import WorkflowPractice

        self.practice_workflows = WorkflowPractice(self)
        from .services.workflow_controls import WorkflowControls

        self.workflow_controls = WorkflowControls(self)
        self.learning = LearningService(self)
        from .services.assessment import AssessmentService

        self.assessments = AssessmentService(self)
        self.evolution = EvolutionService(self)
        from .services.evolution_workflow import EvolutionExecutions

        self.evolution_workflows = EvolutionExecutions(self)
        self.coding = CodingService(self)
        from .services.rollouts import RolloutService

        self.rollouts = RolloutService(self)
        from .services.lifecycle import LifecycleService

        self.lifecycle = LifecycleService(self)
        from .services.shared_builds import SharedBuildService
        from .services.taxonomy_governance import TaxonomyGovernanceService

        self.shared_builds = SharedBuildService(self)
        self.governance = TaxonomyGovernanceService(self)
        from .services.legacy import LegacyImportService

        self.legacy = LegacyImportService(self)
        from .services.privacy import PrivacyService

        self.privacy = PrivacyService(self)

    def bootstrap(self, name="owner") -> dict[str, str]:
        self.db.create_schema()
        with self.db.transaction() as db:
            if db.scalar(select(Principal).where(Principal.name == name)):
                raise DomainError("EXISTS", "账号已存在；不会覆盖密钥。")
            principal, private, token = create_principal(db, name, admin=True)
            private = private_namespace(db, principal.id)
            public = db.scalar(select(Namespace).where(Namespace.kind == "shared_public"))
            if public is None:
                public = Namespace(
                    kind="shared_public", owner_id=principal.id, name="Public source knowledge"
                )
                db.add(public)
                db.flush()
            result = {
                "principal_id": principal.id,
                "namespace_id": private.id,
                "public_namespace_id": public.id,
                "token": token,
            }
        seed(self.db, result["principal_id"], result["public_namespace_id"])
        return result

    async def execute(self, run_id: str, worker: str | None = None) -> None:
        worker = worker or "worker-" + str(uuid4())
        epoch = self.runtime.claim(run_id, worker)
        if epoch is None:
            return
        with self.runtime.maintain_lease(run_id, epoch, worker) as heartbeat:
            await self._execute(run_id, epoch, worker, heartbeat)

    async def _execute(
        self, run_id: str, epoch: int, worker: str, heartbeat: LeaseHeartbeat
    ) -> None:
        with self.db.read() as db:
            run = db.get(AgentRun, run_id)
        if run is None:
            raise DomainError("RUN_STATE", "已领取的任务记录缺失，停止执行。", 500)

        try:
            self.runtime.validate_execution(run_id, epoch, worker)
            if (
                run.input_manifest.get("model_profile")
                and run.input_manifest["model_profile"] != self.provider.profile_version
            ):
                raise DomainError(
                    "MODEL_PROFILE_CHANGED", "运行模型配置已改变，必须重新授权新任务。"
                )
            if run.workflow_engine == "langgraph":
                from .workflows.runtime_adapter import GraphOutcome, GraphRuntimeAdapter

                adapter = getattr(self, "graph_runtime", None)
                if adapter is None:
                    adapter = self.graph_runtime = GraphRuntimeAdapter(self)
                outcome = await adapter.execute(run, epoch, worker)
                if outcome == GraphOutcome.WAITING_CHILDREN:
                    return
            elif run.workflow_engine != "legacy":
                raise DomainError("GRAPH_ENGINE", "未知的冻结执行引擎。")
            elif run.workflow == "knowledge_build":
                if run.input_manifest.get("curation_publication_contract"):
                    from .services.curation_publication import publish_cohort

                    completed = await publish_cohort(self, run, epoch, worker)
                else:
                    completed = await self.build.root(run, epoch, worker)
                if not completed:
                    return
            elif run.workflow == "project_discovery":
                from .services.project_pool import ProjectPoolService

                await ProjectPoolService(self).execute(run, epoch, worker)
                return
            elif run.workflow == "analysis":
                if run.input_manifest.get("curation_publication_contract"):
                    from .services.curation_publication import materialize

                    await materialize(self, run, epoch, worker)
                elif run.input_manifest.get("linked_preview_contract"):
                    from .services.linked_analysis import analyze_preview

                    await analyze_preview(self, run, epoch, worker)
                else:
                    await self.build.analyze(run, epoch, worker)
            elif run.workflow == "curation_validation":
                from .services.curation_validation import execute

                await execute(self, run, epoch, worker)
            elif run.workflow == "review":
                if run.input_manifest.get("curation_publication_contract"):
                    from .services.curation_publication import review_versions

                    await review_versions(self, run, epoch, worker)
                else:
                    await self.build.review(run, epoch, worker)
            elif run.workflow == "desk_intent":
                from .learning_intent import execute

                await execute(self, run, epoch, worker)
            elif run.workflow == "desk_dialogue":
                from .desk_dialogue import execute

                await execute(self, run, epoch, worker)
            elif run.workflow == "chat":
                await self.conversations.process(
                    run.input_manifest["turn_id"], (run.id, epoch, worker)
                )
            elif run.workflow == "coding":
                await self.coding.generate(run, epoch, worker)
            elif run.workflow == "evolution":
                await self.evolution.execute(run, epoch, worker)
            elif run.workflow == "learning_plan":
                await self.learning.execute_plan(run, epoch, worker)
            elif run.workflow == "jd_extract":
                await self.jd.execute(run, epoch, worker)
            elif run.workflow == "learning_assessment":
                await self.assessments.execute(run, epoch, worker)
            elif run.workflow == "evolution_replay":
                await self.evolution.execute_replay(run, epoch, worker)
            elif run.workflow == "retrieval_index":
                await self.retrieval.execute_index(run, epoch, worker)
            else:
                raise DomainError("WORKFLOW_UNREGISTERED", "未注册工作流。")
            result = self.runtime.stage(run.id, "result") or {}
            status = "partially_succeeded" if result.get("gaps") else "succeeded"
            heartbeat.check()
            self.runtime.finish(run_id, epoch, worker, status)
        except GraphCheckpointUnavailable:
            # No new domain attempt here. Existing lease expiry/reaper fences the
            # old worker, reconciles attempted receipts, then emits the outbox retry.
            log.warning("Graph checkpoint unavailable; awaiting fenced runtime recovery")
            return
        except PauseBoundary:
            self.runtime.acknowledge_pause(run_id, epoch, worker)
            return
        except LeaseLost:
            return
        except DomainError as exc:
            terminated = self.runtime.finish(
                run_id,
                epoch,
                worker,
                "budget_stopped" if "BUDGET" in exc.code else "failed",
                exc.code,
            )
            if terminated and run.workflow == "chat":
                self.conversations.mark_failed(run.input_manifest["turn_id"], exc.code)
        except Exception:
            log.error(
                "Uncaught workflow error; inspect local authorized test report, private payload omitted"
            )
            terminated = self.runtime.finish(run_id, epoch, worker, "failed", "INTERNAL_ERROR")
            if terminated and run.workflow == "chat":
                self.conversations.mark_failed(run.input_manifest["turn_id"], "INTERNAL_ERROR")
            # The test/local runner surfaces unexpected coding errors instead of masking them.
            if self.settings.mode == "test":
                raise

    async def execute_workflow(self, execution_id, worker=None):
        from .workflows.session_graph import SessionGraphAdapter

        if await self.workflow_controls.execute_terminal(execution_id):
            return
        worker = worker or "session-" + str(uuid4())
        from .models_dialogue import WorkflowExecution

        with self.db.read() as db:
            identity = db.get(WorkflowExecution, execution_id)
        if identity is not None:
            from .workflows.registry import validate_session_binding

            validate_session_binding(identity)
        service = (
            self.evolution_workflows
            if identity and identity.workflow_type == "evolution_session"
            else self.learning_workflows
            if identity and identity.workflow_type == "learning_session"
            else self.session_workflows
        )
        claimed = service.claim(execution_id, worker)
        if claimed is None:
            return
        epoch, generation = claimed
        with service.heartbeats.maintain(
            (execution_id, epoch, worker),
            lambda: service.heartbeat(execution_id, epoch, worker),
            self.runtime.heartbeat_interval,
        ) as heartbeat:
            try:
                from .models_dialogue import WorkflowExecution
                from .workflows.session_control_compat import LegacySessionControlAdapter
                from .workflows.session_control_graph import ControlSessionGraphAdapter

                with self.db.read() as db:
                    execution = db.get(WorkflowExecution, execution_id)
                    version = execution.graph_version
                    from .models_dialogue import WorkflowSession

                    controlled = (
                        db.get(WorkflowSession, execution.workflow_id).control_generation > 0
                    )
                from .workflows.evolution_session import EvolutionSessionGraphAdapter
                from .workflows.learning_session import LearningSessionGraphAdapter
                from .workflows.practice_session import PracticeSessionGraphAdapter

                slot = (
                    "evolution_session_graph"
                    if version == "evolution-session-v1"
                    else "practice_session_graph"
                    if version == "learning-session-practice-v1"
                    else "learning_session_graph"
                    if version == "learning-session-v1"
                    else "session_control_graph"
                    if version == "knowledge-session-v2"
                    else "session_compat_control_graph"
                    if controlled
                    else "session_graph"
                )
                adapter = getattr(self, slot, None)
                if adapter is None:
                    adapter = (
                        EvolutionSessionGraphAdapter
                        if version == "evolution-session-v1"
                        else PracticeSessionGraphAdapter
                        if version == "learning-session-practice-v1"
                        else LearningSessionGraphAdapter
                        if version == "learning-session-v1"
                        else ControlSessionGraphAdapter
                        if version == "knowledge-session-v2"
                        else LegacySessionControlAdapter
                        if controlled
                        else SessionGraphAdapter
                    )(self)
                    if slot == "session_compat_control_graph" and getattr(
                        self, "session_graph", None
                    ):
                        adapter.checkpointers = self.session_graph.checkpointers
                    setattr(self, slot, adapter)
                completed = await adapter.execute(execution_id, epoch, worker, generation)
                if completed:
                    heartbeat.check()
                    checkpoint_id = await adapter.completion_witness(execution_id, epoch, worker)
                    service.complete_checkpoint(
                        execution_id, epoch, worker, generation, checkpoint_id
                    )
            except GraphCheckpointUnavailable:
                log.warning("Session checkpoint unavailable; awaiting fenced recovery")
            except PauseBoundary:
                service.project_wait(execution_id, epoch, worker, generation, "control")
            except LeaseLost:
                pass
            except DomainError as exc:
                service.finish(execution_id, epoch, worker, generation, exc.code)
            except Exception:
                service.finish(execution_id, epoch, worker, generation, "INTERNAL_ERROR")
                if self.settings.mode == "test":
                    raise

    async def drain(self, limit=100) -> dict[str, int]:
        """Local test/development worker. Production uses Celery's registered execute task."""
        if self.settings.mode == "production":
            raise DomainError("LOCAL_RUNNER_FORBIDDEN", "生产模式使用持久 Celery Worker。")
        self.lifecycle.run_due_maintenance()
        runs = 0
        while runs < limit:
            self.runtime.reap()
            self.session_workflows.reap()
            self.process_projections()
            with self.db.read() as db:
                run = db.scalar(
                    select(AgentRun)
                    .where(AgentRun.status == "queued")
                    .order_by(AgentRun.created_at, AgentRun.id)
                )
                from .models_dialogue import (
                    WorkflowControlCommand,
                    WorkflowExecution,
                    WorkflowSession,
                )

                execution = db.scalar(
                    select(WorkflowExecution)
                    .where(WorkflowExecution.status == "queued")
                    .order_by(WorkflowExecution.created_at, WorkflowExecution.id)
                )
                if execution is None:
                    execution = db.scalar(
                        select(WorkflowExecution)
                        .join(WorkflowSession, WorkflowSession.id == WorkflowExecution.workflow_id)
                        .join(
                            WorkflowControlCommand,
                            WorkflowControlCommand.workflow_id == WorkflowSession.id,
                        )
                        .where(
                            WorkflowExecution.status == "completed",
                            WorkflowControlCommand.generation == WorkflowSession.control_generation,
                            WorkflowControlCommand.status == "requested",
                            WorkflowSession.control_state.in_(
                                ["pause_requested", "resume_requested"]
                            ),
                        )
                        .order_by(WorkflowExecution.created_at, WorkflowExecution.id)
                    )
                if execution is None:
                    from .models_dialogue import WorkflowTutorRequest

                    execution = db.scalar(
                        select(WorkflowExecution)
                        .join(
                            WorkflowSession,
                            WorkflowSession.current_execution_id == WorkflowExecution.id,
                        )
                        .join(
                            WorkflowTutorRequest,
                            WorkflowTutorRequest.predecessor_execution_id == WorkflowExecution.id,
                        )
                        .where(
                            WorkflowExecution.status == "completed",
                            WorkflowExecution.workflow_type == "knowledge_session",
                            WorkflowSession.control_state == "active",
                            WorkflowTutorRequest.status == "pending_handoff",
                        )
                        .limit(1)
                    )
                use_session = execution is not None and (run is None or runs % 2 == 0)
                identifier = (
                    execution.id
                    if use_session and execution is not None
                    else run.id
                    if run
                    else None
                )
                deliveries = (
                    list(
                        db.scalars(
                            select(Outbox.id).where(
                                Outbox.kind == ("workflow.execute" if use_session else "run"),
                                Outbox.aggregate_id == identifier,
                                Outbox.delivered.is_(False),
                            )
                        )
                    )
                    if identifier
                    else []
                )
            if not identifier:
                break
            if use_session:
                await self.execute_workflow(identifier)
            else:
                await self.execute(identifier)
            with self.db.transaction() as db:
                for delivery_id in deliveries:
                    delivery = db.get(Outbox, delivery_id)
                    if delivery is not None:
                        delivery.delivered = True
            runs += 1
        self.process_projections()
        return {"executions": runs}

    def process_projections(self) -> int:
        with self.db.read() as db:
            messages = [
                (m.id, m.kind, m.aggregate_id, m.payload)
                for m in db.scalars(
                    select(Outbox)
                    .where(
                        Outbox.kind.notin_(["run", "workflow.execute"]), Outbox.delivered.is_(False)
                    )
                    .order_by(Outbox.created_at)
                    .limit(100)
                )
            ]
        for identifier, kind, aggregate, payload in messages:
            if kind == "invalidation":
                with self.db.transaction() as db:
                    progress = propagate_batch(db, aggregate, payload["generation"], batch_size=256)
                if not progress.complete:
                    # The frontier is committed, but the outbox retains work for
                    # the next bounded dispatcher tick or a replacement worker.
                    continue
            elif kind == "feedback.withdrawn":
                from .models import Feedback

                with self.db.read() as db:
                    feedback = db.get(Feedback, aggregate)
                    owner = feedback.principal_id if feedback is not None else None
                if owner is not None:
                    # The consent fence was committed with this durable event.
                    # Cleanup may safely be retried after a worker crash.
                    self.evolution.finish_feedback_withdrawal(owner, aggregate)
            elif kind == "evolution.withdrawn":
                self.evolution_workflows.reconcile_withdrawal(aggregate)
            elif kind in {"gap.auto", "gap.answer"}:
                from .services.gap_scheduling import process_auto_gap

                process_auto_gap(self.lifecycle, aggregate, after_answer=kind == "gap.answer")
            elif kind == "workflow.pause_checkpoint":
                self.workflows.acknowledge_pause(identifier, aggregate)
                continue
            elif kind == "workflow.milestone":
                self.continuation.consume(identifier, aggregate)
                continue
            elif kind == "index":
                from .models import Publication

                with self.db.read() as db:
                    pub = db.get(Publication, aggregate)
                    namespace = db.get(Namespace, pub.namespace_id) if pub else None
                    if pub is not None and namespace is None:
                        raise DomainError("PROJECTION_SCOPE", "发布所属知识域缺失，停止索引。", 500)
                    actor = namespace.owner_id if namespace is not None else None
                if actor:
                    try:
                        self.retrieval.index_lexical(actor, aggregate)
                    except DomainError:
                        pass  # A revoked item must NOT be re-exposed by projection replay.
            with self.db.transaction() as db:
                message = db.get(Outbox, identifier)
                if message is not None:
                    message.delivered = True
        return len(messages)
