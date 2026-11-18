from enum import StrEnum

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from ..errors import DomainError
from ..models import AgentRun
from .checkpointer import Checkpointers
from .events import project_event
from .registry import GRAPH_VERSION, STATE_SCHEMA_VERSION, validate_binding
from .state import CareerForgeState


class GraphOutcome(StrEnum):
    COMPLETED = "completed"
    WAITING_CHILDREN = "waiting_children"


class GraphRuntimeAdapter:
    """M1: graph scheduling around the unchanged, idempotent JD domain service.

    Celery delivery, claims, budgets, receipt reconciliation and terminal state
    remain in Application/Runtime. No blanket retry of model calls is installed.
    """

    def __init__(self, app):
        self.app = app
        self.checkpointers = Checkpointers(app.settings)

    def reconcile(self, run, epoch, worker):
        validate_binding(run)
        if run.workflow in {"evolution", "evolution_replay"}:
            from .evolution_paid import EvolutionPaidGraph

            return EvolutionPaidGraph(self.app, run, epoch, worker).reconcile()
        if run.workflow == "learning_assessment" and run.graph_version == "learning-assessment-v1":
            from .learning_assessment import LearningAssessmentGraph

            return LearningAssessmentGraph(self.app, run, epoch, worker).reconcile()
        if run.workflow == "chat" and run.graph_version == "learning-tutor-v1":
            from .learning_tutor import LearningTutorGraph

            return LearningTutorGraph(self.app, run, epoch, worker).reconcile()
        if run.workflow == "knowledge_build":
            from .knowledge_build import KnowledgeBuildGraph

            return KnowledgeBuildGraph(self.app, run, epoch, worker).reconcile()
        if run.workflow != "jd_extract":
            raise DomainError("GRAPH_VERSION", "无匹配的领域结果路由。", 409)
        self.app.runtime.validate(run.id, epoch, worker)
        result = self.app.runtime.stage(run.id, "result")
        if result is None:
            raise DomainError(
                "GRAPH_RECONCILIATION", "业务结果缺失，禁止依据 Checkpoint 宣告完成。", 409
            )
        with self.app.db.read() as db:
            current = db.get(AgentRun, run.id)
            if current is None:
                raise DomainError("GRAPH_RUN", "任务记录缺失。", 409)
            self.app.jd.validate_result(db, current.principal_id, current, result)

    def graph(self, saver, run, epoch, worker):
        validate_binding(run)
        if run.workflow in {"evolution", "evolution_replay"}:
            from .evolution_paid import EvolutionPaidGraph

            return EvolutionPaidGraph(self.app, run, epoch, worker).graph(saver)
        if run.workflow == "learning_assessment" and run.graph_version == "learning-assessment-v1":
            from .learning_assessment import LearningAssessmentGraph

            return LearningAssessmentGraph(self.app, run, epoch, worker).graph(saver)
        if run.workflow == "chat" and run.graph_version == "learning-tutor-v1":
            from .learning_tutor import LearningTutorGraph

            return LearningTutorGraph(self.app, run, epoch, worker).graph(saver)
        if run.workflow == "knowledge_build":
            from .knowledge_build import KnowledgeBuildGraph

            return KnowledgeBuildGraph(self.app, run, epoch, worker).graph(saver)
        if run.workflow != "jd_extract":
            raise DomainError("GRAPH_VERSION", "无匹配的付费图工厂。", 409)
        app = self.app

        def fence(state):
            app.runtime.validate(run.id, epoch, worker)
            with app.db.read() as db:
                current = db.get(AgentRun, run.id)
                if current is None:
                    raise DomainError("GRAPH_RUN", "Graph 对应任务不存在。", 404)
                validate_binding(current)
                app.jd.validate_run_in(db, current.principal_id, current)
            if (
                state.get("workflow_run_id"),
                state.get("graph_version"),
                state.get("state_schema_version"),
            ) != (run.id, GRAPH_VERSION, STATE_SCHEMA_VERSION):
                raise DomainError("GRAPH_STATE", "Checkpoint 与冻结任务合同不兼容。", 409)

        async def validate(state):
            fence(state)
            return {"current_stage": "validate"}

        async def execute_domain(state):
            fence(state)
            project_event(app, run.id, epoch, worker, "jd_extract", "started")
            # Business commit can precede checkpoint persistence. Reconcile first.
            if app.runtime.stage(run.id, "result") is None:
                await app.jd.execute(run, epoch, worker)
            fence(state)
            self.reconcile(run, epoch, worker)
            project_event(app, run.id, epoch, worker, "jd_extract", "completed")
            return {"current_stage": "jd_extract"}

        async def save_reference(state):
            fence(state)
            self.reconcile(run, epoch, worker)
            return {"current_stage": "completed", "result_stage_key": "result"}

        builder = StateGraph(CareerForgeState)
        builder.add_node("validate", validate)
        builder.add_node("execute_domain", execute_domain)
        builder.add_node("save_reference", save_reference)
        builder.add_edge(START, "validate")
        builder.add_edge("validate", "execute_domain")
        builder.add_edge("execute_domain", "save_reference")
        builder.add_edge("save_reference", END)
        return builder.compile(checkpointer=saver)

    async def execute(self, run, epoch, worker):
        validate_binding(run)
        config = {"configurable": {"thread_id": run.id}}
        initial = CareerForgeState(
            workflow_run_id=run.id,
            graph_version=run.graph_version,
            state_schema_version=run.state_schema_version,
            current_stage="pending",
            result_stage_key="",
        )
        async with self.checkpointers.open() as saver:
            if run.workflow == "chat" and run.graph_version == "learning-tutor-v1":
                from .learning_tutor import LearningTutorGraph

                tutor = LearningTutorGraph(self.app, run, epoch, worker)
                if tutor.delivered():
                    # Deliberately do not resume the stale paid checkpoint. The
                    # same registered graph has an explicit no-call topology.
                    finalizer = tutor.graph(saver, finalization_only=True)
                    snapshot = await finalizer.aget_state(config)
                    if snapshot.values:
                        for key in ("workflow_run_id", "graph_version", "state_schema_version"):
                            if snapshot.values.get(key) != initial[key]:
                                raise DomainError(
                                    "GRAPH_STATE", "导师 Checkpoint 版本不兼容。", 409
                                )
                    # New input starts this no-paid topology instead of resuming
                    # the previous checkpoint's possibly stale paid-stage task.
                    await finalizer.ainvoke(initial, config=config, durability="sync")
                    tutor.reconcile()
                    return GraphOutcome.COMPLETED
            graph = self.graph(saver, run, epoch, worker)
            snapshot = await graph.aget_state(config)
            if snapshot.values:
                for key in ("workflow_run_id", "graph_version", "state_schema_version"):
                    if snapshot.values.get(key) != initial[key]:
                        raise DomainError(
                            "GRAPH_STATE", "Checkpoint 版本不兼容；需要显式迁移。", 409
                        )
                if not snapshot.next:
                    self.reconcile(run, epoch, worker)
                    return GraphOutcome.COMPLETED
            interrupts = [i for task in snapshot.tasks for i in task.interrupts]
            if interrupts:
                if run.workflow != "knowledge_build":
                    raise DomainError("GRAPH_STATE", "该付费图不接受子任务中断。", 409)
                from ..services.runtime import TERMINAL
                from .knowledge_build import KnowledgeBuildGraph

                knowledge = KnowledgeBuildGraph(self.app, run, epoch, worker)
                knowledge.fence(snapshot.values)
                # Resume only after re-reading authoritative child status. A
                # checkpoint is not evidence that the child succeeded.
                for item in interrupts:
                    child_id = item.value.get("child_run_id")
                    with self.app.db.read() as db:
                        child = db.get(AgentRun, child_id)
                        if child is None or child.parent_id != run.id:
                            raise DomainError("GRAPH_STATE", "等待的子任务无效。", 409)
                        pending = child.status not in TERMINAL
                    if pending:
                        self.app.runtime.wait_children(run.id, epoch, worker)
                        return GraphOutcome.WAITING_CHILDREN
            recursion_limit = 64
            if run.workflow == "evolution_replay":
                from ..services.evolution_stages import replay_step_limit

                recursion_limit = replay_step_limit(run)
            try:
                output = await graph.ainvoke(
                    Command(resume=True) if interrupts else None if snapshot.values else initial,
                    config={**config, "recursion_limit": recursion_limit},
                    durability="sync",
                )
            except GraphRecursionError as exc:
                if run.workflow != "evolution_replay":
                    raise
                # Inspect the original ledger cursor; never reset the Run, cap,
                # receipt keys or retry an external call to repair scheduling.
                from ..services.evolution_stages import next_arm

                next_arm(self.app, run)
                raise DomainError(
                    "GRAPH_STEP_LIMIT",
                    "冻结回放拓扑未在有界步数内完成；保留原调用回执等待排查。",
                    409,
                ) from exc
            if output.get("__interrupt__"):
                # Synchronous checkpoint durability precedes releasing the lease.
                self.app.runtime.wait_children(run.id, epoch, worker)
                return GraphOutcome.WAITING_CHILDREN
            self.reconcile(run, epoch, worker)
            return GraphOutcome.COMPLETED

    async def summary(self, run):
        from ..services.evolution_stages import TOPOLOGY
        from .knowledge_build import STAGES
        from .learning_assessment import STAGES as ASSESSMENT_STAGES
        from .learning_tutor import STAGES as TUTOR_STAGES

        validate_binding(run)
        async with self.checkpointers.open() as saver:
            checkpoint = await saver.aget_tuple({"configurable": {"thread_id": run.id}})
            if checkpoint is None:
                return {"thread_id": run.id, "checkpoint_exists": False}
            values = checkpoint.checkpoint.get("channel_values", {})
            if (
                values.get("workflow_run_id"),
                values.get("graph_version"),
                values.get("state_schema_version"),
            ) != (run.id, run.graph_version, run.state_schema_version):
                raise DomainError("GRAPH_STATE", "Checkpoint 与冻结任务版本不兼容。", 409)
            return {
                "thread_id": run.id,
                "checkpoint_exists": True,
                "graph_version": run.graph_version,
                "state_schema_version": run.state_schema_version,
                "current_stage": values.get("current_stage")
                if values.get("current_stage")
                in {
                    "pending",
                    "validate",
                    "jd_extract",
                    "completed",
                    *STAGES,
                    *TUTOR_STAGES,
                    *ASSESSMENT_STAGES,
                    *TOPOLOGY["candidate"],
                    *TOPOLOGY["replay"],
                }
                else "unknown",
            }
