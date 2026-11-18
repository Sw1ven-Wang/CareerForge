"""Durable session continuation with database-authorized, exact-version approval."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..errors import DomainError
from ..models_dialogue import WorkflowExecution
from .checkpointer import Checkpointers


class SessionState(TypedDict):
    execution_id: str
    workflow_id: str
    goal_binding_hash: str
    graph_version: str
    state_schema_version: int
    current_stage: str


WAITS = {
    "wait_builds": "builds",
    "wait_plan": "plan",
    "wait_approval": "approval",
    "wait_authorization": "authorization",
    "wait_evidence": "evidence",
    "wait_plan_failure": "plan_failure",
}


class SessionGraphAdapter:
    def __init__(self, app):
        self.app, self.service = app, app.session_workflows
        self.checkpointers = Checkpointers(app.settings)

    def graph(self, saver, execution, epoch, worker):
        service, identifier = self.service, execution.id

        def fence(state):
            if (
                any(
                    state.get(key) != getattr(execution, key)
                    for key in (
                        "workflow_id",
                        "goal_binding_hash",
                        "graph_version",
                        "state_schema_version",
                    )
                )
                or state.get("execution_id") != identifier
            ):
                raise DomainError("GRAPH_STATE", "Checkpoint 与会话执行不匹配。", 409)
            with service.transaction(identifier, epoch, worker):
                pass

        def route():
            with service.transaction(identifier, epoch, worker) as (db, _, workflow, row):
                return service.route_in(db, workflow, row)

        async def dispatch(state):
            fence(state)
            return {"current_stage": "route"}

        def operation(name):
            async def execute(state):
                fence(state)
                with service.transaction(identifier, epoch, worker) as (db, conv, workflow, row):
                    # Stale control flow must not authorize a side effect.
                    if service.route_in(db, workflow, row) != name:
                        return {"current_stage": name}
                    if name == "aggregate_builds":
                        self.app.continuation.aggregate_build_results_in(
                            db, workflow.principal_id, conv, workflow
                        )
                    elif name == "ensure_plan":
                        service.ensure_plan_in(db, conv, workflow, row)
                    elif name == "observe_plan":
                        plan = service.runs(db, workflow, "plan")[0]
                        self.app.continuation.observe_plan_result_in(
                            db, workflow.principal_id, conv, workflow, plan
                        )
                    elif name == "completed":
                        if not service.acceptance_in(db, workflow, row):
                            raise DomainError("GRAPH_APPROVAL", "计划尚未采纳。", 409)
                    self.app.workflows.emit_in(
                        db,
                        conv,
                        "workflow.stage.completed",
                        f"session:{row.id}:{name}",
                        workflow,
                        execution_id=row.id,
                        stage=name,
                        graph_version=row.graph_version,
                    )
                return {"current_stage": name}

            return execute

        def waiting(name):
            async def wait(state):
                fence(state)
                while route() == name:
                    with service.transaction(identifier, epoch, worker) as (
                        db,
                        conv,
                        workflow,
                        row,
                    ):
                        self.app.workflows.emit_in(
                            db,
                            conv,
                            "workflow.waiting",
                            f"session:{row.id}:{name}",
                            workflow,
                            execution_id=row.id,
                            wait_kind=WAITS[name],
                        )
                    interrupt({"execution_id": identifier, "kind": WAITS[name]})
                    fence(state)
                return {"current_stage": name}

            return wait

        builder = StateGraph(SessionState)
        builder.add_node("route", dispatch)
        builder.add_edge(START, "route")
        stages = ["aggregate_builds", "ensure_plan", "observe_plan", "completed", *WAITS]
        for name in stages:
            builder.add_node(name, waiting(name) if name in WAITS else operation(name))
            builder.add_edge(name, END if name == "completed" else "route")
        builder.add_conditional_edges("route", lambda state: route(), stages)
        return builder.compile(checkpointer=saver)

    def reconcile(self, identifier, epoch, worker):
        with self.service.transaction(identifier, epoch, worker) as (db, _, workflow, execution):
            if not self.service.acceptance_in(db, workflow, execution):
                raise DomainError("GRAPH_APPROVAL", "Checkpoint 不能替代明确的计划采纳。", 409)

    async def execute(self, identifier, epoch, worker, generation):
        with self.service.transaction(identifier, epoch, worker) as (_, _, _, execution):
            initial = SessionState(
                execution_id=identifier,
                workflow_id=execution.workflow_id,
                goal_binding_hash=execution.goal_binding_hash,
                graph_version=execution.graph_version,
                state_schema_version=execution.state_schema_version,
                current_stage="pending",
            )
        config = {"configurable": {"thread_id": identifier}, "recursion_limit": 64}
        async with self.checkpointers.open() as saver:
            graph = self.graph(saver, execution, epoch, worker)
            snapshot = await graph.aget_state(config)
            if snapshot.values:
                if any(
                    snapshot.values.get(key) != value
                    for key, value in initial.items()
                    if key != "current_stage"
                ):
                    raise DomainError("GRAPH_STATE", "会话 Checkpoint 身份或版本不兼容。", 409)
                if not snapshot.next and snapshot.values.get("current_stage") == "completed":
                    self.reconcile(identifier, epoch, worker)
                    return True
            paused = [item for task in snapshot.tasks for item in task.interrupts]
            if paused:
                if any(
                    not isinstance(item.value, dict)
                    or item.value.get("execution_id") != identifier
                    or item.value.get("kind") not in WAITS.values()
                    for item in paused
                ):
                    raise DomainError("GRAPH_STATE", "会话等待引用无效。", 409)
                with self.service.transaction(identifier, epoch, worker) as (db, _, workflow, row):
                    route = self.service.route_in(db, workflow, row)
                if route in WAITS and all(item.value["kind"] == WAITS[route] for item in paused):
                    self.service.project_wait(identifier, epoch, worker, generation, WAITS[route])
                    return False
            result = await graph.ainvoke(
                Command(resume=True)
                if paused
                else None
                if snapshot.values and snapshot.next
                else initial,
                config=config,
                durability="sync",
            )
            if result.get("__interrupt__"):
                kind = result["__interrupt__"][0].value["kind"]
                self.service.project_wait(identifier, epoch, worker, generation, kind)
                return False
            self.reconcile(identifier, epoch, worker)
            return True

    async def summary(self, identifier):
        with self.app.db.read() as db:
            row = db.get(WorkflowExecution, identifier)
            if row is None:
                raise DomainError("NOT_FOUND", "会话执行不存在。", 404)
        async with self.checkpointers.open() as saver:
            saved = await saver.aget_tuple({"configurable": {"thread_id": identifier}})
        return {
            "execution_id": identifier,
            "checkpoint_exists": saved is not None,
            "graph_version": row.graph_version,
            "state_schema_version": row.state_schema_version,
        }

    async def completion_witness(self, identifier, epoch, worker):
        """Read, never migrate, the registered execution's durable terminal state."""
        with self.service.transaction(identifier, epoch, worker, control=False) as (_, _, _, row):
            execution = row
        async with self.checkpointers.open() as saver:
            graph = self.graph(saver, execution, epoch, worker)
            snapshot = await graph.aget_state({"configurable": {"thread_id": identifier}})
        if (
            snapshot.next
            or snapshot.values.get("current_stage") != "completed"
            or snapshot.values.get("execution_id") != identifier
            or any(
                snapshot.values.get(key) != getattr(execution, key)
                for key in (
                    "workflow_id",
                    "goal_binding_hash",
                    "graph_version",
                    "state_schema_version",
                )
            )
        ):
            raise DomainError("GRAPH_STATE", "缺少匹配的持久完成检查点。", 409)
        return snapshot.config["configurable"]["checkpoint_id"]
