"""Unpaid session graph: dispatch, reconcile and durable human waits, never models."""

from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt

from ..errors import DomainError
from .session_control_graph import ControlSessionGraphAdapter
from .session_graph import SessionState

WAITS = {
    "wait_selection": "selection",
    "wait_reconciliation": "reconciliation",
    "wait_authorization": "authorization",
    "wait_followup": "followup",
    "wait_turn": "tutor",
    "wait_control": "control",
}


class LearningSessionGraphAdapter(ControlSessionGraphAdapter):
    def __init__(self, app):
        super().__init__(app)
        self.service = app.learning_workflows

    def graph(self, saver, execution, epoch, worker):
        service, identifier = self.service, execution.id

        def fence(state):
            if state.get("execution_id") != identifier or any(
                state.get(key) != getattr(execution, key)
                for key in (
                    "workflow_id",
                    "goal_binding_hash",
                    "graph_version",
                    "state_schema_version",
                )
            ):
                raise DomainError("GRAPH_STATE", "学习检查点身份不一致。", 409)

        def route_in(db, workflow, row):
            if workflow.control_state in {"pause_requested", "paused"}:
                return "wait_control"
            if workflow.control_state != "active":
                raise DomainError("CONTROL_STATE", "需要合法恢复命令。", 409)
            return service.route_in(db, workflow, row)

        def route():
            with service.transaction(identifier, epoch, worker, control=False) as (
                db,
                _,
                workflow,
                row,
            ):
                return route_in(db, workflow, row)

        async def validate(state):
            fence(state)
            route()
            return {"current_stage": "route"}

        def operation(name):
            async def execute(state):
                fence(state)
                with service.transaction(identifier, epoch, worker, control=False) as (
                    db,
                    conv,
                    workflow,
                    row,
                ):
                    if route_in(db, workflow, row) == name:
                        service.operate_in(db, conv, workflow, row, name)
                return {"current_stage": name}

            return execute

        def waiting(name):
            async def wait(state):
                fence(state)
                while route() == name:
                    with service.transaction(identifier, epoch, worker, control=False) as (
                        db,
                        conv,
                        workflow,
                        row,
                    ):
                        request = self.app.learning_requests.last_in(db, workflow)
                        payload = {
                            "execution_id": identifier,
                            "kind": WAITS[name],
                            "request_id": request.id if request else None,
                            "sequence": request.sequence if request else 0,
                        }
                        if name == "wait_control":
                            command = self.app.workflow_controls.validate(
                                db, workflow, self.app.workflow_controls.current(db, workflow), row
                            )
                            if command.kind != "pause":
                                raise DomainError("CONTROL_BINDING", "暂停等待缺少暂停命令。", 409)
                            payload.update(command_id=command.id, generation=command.generation)
                        self.app.workflows.emit_in(
                            db,
                            conv,
                            "workflow.learning_waiting",
                            f"learning-wait:{row.id}:{name}:{payload['sequence']}:{payload.get('generation', 0)}",
                            workflow,
                            **{("wait_kind" if k == "kind" else k): v for k, v in payload.items()},
                        )
                    interrupt(payload)
                    fence(state)
                return {"current_stage": name}

            return wait

        builder = StateGraph(SessionState)
        builder.add_node("route", validate)
        builder.add_edge(START, "route")
        stages = ["dispatch_turn", "reconcile_turn", *WAITS]
        for name in stages:
            builder.add_node(name, waiting(name) if name in WAITS else operation(name))
            builder.add_edge(name, "route")
        builder.add_conditional_edges("route", lambda state: route(), stages)
        return builder.compile(checkpointer=saver)

    async def execute(self, identifier, epoch, worker, generation):
        with self.service.transaction(identifier, epoch, worker, control=False) as (
            _,
            _,
            _,
            execution,
        ):
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
            if snapshot.values and any(
                snapshot.values.get(k) != v for k, v in initial.items() if k != "current_stage"
            ):
                raise DomainError("GRAPH_STATE", "学习检查点版本不兼容。", 409)
            paused = [item for task in snapshot.tasks for item in task.interrupts]
            if len(paused) > 1 or any(
                not isinstance(item.value, dict)
                or item.value.get("execution_id") != identifier
                or item.value.get("kind") not in WAITS.values()
                for item in paused
            ):
                raise DomainError("GRAPH_STATE", "学习等待引用无效。", 409)
            with self.service.transaction(identifier, epoch, worker, control=False) as (
                db,
                conv,
                workflow,
                row,
            ):
                last = self.app.learning_requests.last_in(db, workflow)
                if paused and paused[0].value.get("request_id"):
                    from ..models_dialogue import WorkflowTutorRequest

                    referenced = db.get(WorkflowTutorRequest, paused[0].value["request_id"])
                    if (
                        not referenced
                        or referenced.execution_id != identifier
                        or referenced.sequence != paused[0].value.get("sequence")
                        or (last and referenced.sequence > last.sequence)
                    ):
                        raise DomainError("GRAPH_STATE", "学习等待请求身份不一致。", 409)
                if workflow.control_state == "resume_requested":
                    command = self.app.workflow_controls.current(db, workflow)
                    if (
                        len(paused) != 1
                        or paused[0].value.get("kind") != "control"
                        or paused[0].value.get("command_id") != command.resume_command_id
                    ):
                        raise DomainError("CONTROL_BINDING", "恢复不匹配原暂停。", 409)
                    self.app.workflow_controls.resume_in(
                        db, conv, workflow, row, snapshot.config["configurable"]["checkpoint_id"]
                    )
                control = workflow.control_state
                command = self.app.workflow_controls.current(db, workflow)
                route = (
                    self.service.route_in(db, workflow, row)
                    if control == "active"
                    else "wait_control"
                )
            if (
                paused
                and control in {"paused", "pause_requested"}
                and paused[0].value.get("kind") == "control"
                and paused[0].value.get("command_id") == command.id
            ):
                self.checkpoint_pause(identifier, epoch, worker, generation, snapshot)
                return False
            if (
                paused
                and control == "active"
                and route in WAITS
                and paused[0].value["kind"] == WAITS[route]
                and paused[0].value.get("request_id") == (last.id if last else None)
            ):
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
            if not result.get("__interrupt__"):
                raise DomainError("GRAPH_STATE", "学习图未形成持久等待。", 409)
            saved = await graph.aget_state(config)
            saved_interrupts = [item for task in saved.tasks for item in task.interrupts]
            if (
                not saved.config.get("configurable", {}).get("checkpoint_id")
                or len(saved_interrupts) != 1
                or saved_interrupts[0].value != result["__interrupt__"][0].value
            ):
                raise DomainError("GRAPH_STATE", "学习等待未持久化。", 409)
            kind = saved_interrupts[0].value["kind"]
            if kind == "control":
                self.checkpoint_pause(identifier, epoch, worker, generation, saved)
            else:
                self.service.project_wait(identifier, epoch, worker, generation, kind)
            return False
