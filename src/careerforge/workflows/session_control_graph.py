"""Registered M3 graph. V1 topology/state remains in session_graph.py unchanged."""

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..errors import DomainError
from .session_graph import WAITS, SessionGraphAdapter, SessionState

CONTROL_WAITS = {**WAITS, "wait_control": "control"}


class ControlSessionGraphAdapter(SessionGraphAdapter):
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
                raise DomainError("GRAPH_STATE", "Checkpoint 与会话执行不匹配。", 409)
            with service.transaction(identifier, epoch, worker, control=False):
                pass

        def current_route(db, workflow, row):
            if workflow.control_state in {"pause_requested", "paused"}:
                return "wait_control"
            if workflow.control_state != "active":
                raise DomainError("CONTROL_STATE", "执行需要合法的恢复命令。", 409)
            return service.route_in(db, workflow, row)

        def route():
            with service.transaction(identifier, epoch, worker, control=False) as (
                db,
                _,
                workflow,
                row,
            ):
                return current_route(db, workflow, row)

        async def dispatch(state):
            fence(state)
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
                    if current_route(db, workflow, row) != name:
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
                    elif name == "completed" and not service.acceptance_in(db, workflow, row):
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
                    with service.transaction(identifier, epoch, worker, control=False) as (
                        db,
                        conv,
                        workflow,
                        row,
                    ):
                        payload = {"execution_id": identifier, "kind": CONTROL_WAITS[name]}
                        if name == "wait_control":
                            command = self.app.workflow_controls.validate(
                                db, workflow, self.app.workflow_controls.current(db, workflow), row
                            )
                            if command.kind != "pause":
                                raise DomainError("CONTROL_BINDING", "控制等待需要暂停命令。", 409)
                            payload.update(command_id=command.id, generation=command.generation)
                        self.app.workflows.emit_in(
                            db,
                            conv,
                            "workflow.waiting",
                            f"session:{row.id}:{name}:" + str(payload.get("generation", 0)),
                            workflow,
                            execution_id=row.id,
                            wait_kind=CONTROL_WAITS[name],
                        )
                    interrupt(payload)
                    fence(state)
                return {"current_stage": name}

            return wait

        builder = StateGraph(SessionState)
        builder.add_node("route", dispatch)
        builder.add_edge(START, "route")
        stages = ["aggregate_builds", "ensure_plan", "observe_plan", "completed", *CONTROL_WAITS]
        for name in stages:
            builder.add_node(name, waiting(name) if name in CONTROL_WAITS else operation(name))
            # Even completion passes through a final control-aware route.
            if name == "completed":
                builder.add_conditional_edges(
                    name, lambda state: END if route() == "completed" else "route", [END, "route"]
                )
            else:
                builder.add_edge(name, "route")
        builder.add_conditional_edges("route", lambda state: route(), stages)
        return builder.compile(checkpointer=saver)

    def checkpoint_pause(self, identifier, epoch, worker, generation, snapshot):
        checkpoints = [item.value for task in snapshot.tasks for item in task.interrupts]
        checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
        if not checkpoint_id or len(checkpoints) != 1 or checkpoints[0].get("kind") != "control":
            raise DomainError("GRAPH_STATE", "暂停未形成持久控制检查点。", 409)
        value = checkpoints[0]
        with self.service.transaction(identifier, epoch, worker, control=False) as (
            db,
            conv,
            workflow,
            row,
        ):
            command = self.app.workflow_controls.current(db, workflow)
            if (
                command is None
                or value.get("command_id") != command.id
                or value.get("generation") != command.generation
                or value.get("execution_id") != identifier
            ):
                raise DomainError("CONTROL_STALE", "暂停检查点命令不匹配。", 409)
            row.handled_generation = max(row.handled_generation, generation)
            self.app.workflow_controls.checkpoint_in(
                db, conv, workflow, row, command.id, checkpoint_id
            )
            if row.status == "running":
                row.status, row.wait_kind = "waiting", "control"
                row.worker_id, row.lease_until = None, None
                row.revision += 1

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
                snapshot.values.get(key) != value
                for key, value in initial.items()
                if key != "current_stage"
            ):
                raise DomainError("GRAPH_STATE", "会话 Checkpoint 身份或版本不兼容。", 409)
            paused = [item for task in snapshot.tasks for item in task.interrupts]
            if any(
                not isinstance(item.value, dict)
                or item.value.get("execution_id") != identifier
                or item.value.get("kind") not in CONTROL_WAITS.values()
                for item in paused
            ):
                raise DomainError("GRAPH_STATE", "会话等待引用无效。", 409)
            with self.service.transaction(identifier, epoch, worker, control=False) as (
                db,
                conv,
                workflow,
                row,
            ):
                if workflow.control_state == "resume_requested":
                    command = self.app.workflow_controls.current(db, workflow)
                    if (
                        len(paused) != 1
                        or paused[0].value.get("kind") != "control"
                        or paused[0].value.get("command_id") != command.resume_command_id
                    ):
                        raise DomainError(
                            "CONTROL_BINDING", "原暂停 interrupt 与恢复命令不匹配。", 409
                        )
                    self.app.workflow_controls.resume_in(
                        db,
                        conv,
                        workflow,
                        row,
                        (snapshot.config or {}).get("configurable", {}).get("checkpoint_id"),
                    )
                control = workflow.control_state
                control_command = self.app.workflow_controls.current(db, workflow)
                route = (
                    self.service.route_in(db, workflow, row)
                    if control == "active"
                    else "wait_control"
                )
            if (
                paused
                and control in {"pause_requested", "paused"}
                and all(
                    item.value["kind"] == "control"
                    and item.value.get("command_id") == control_command.id
                    for item in paused
                )
            ):
                self.checkpoint_pause(identifier, epoch, worker, generation, snapshot)
                return False
            if (
                paused
                and control == "active"
                and route in WAITS
                and all(item.value["kind"] == WAITS[route] for item in paused)
            ):
                self.service.project_wait(identifier, epoch, worker, generation, WAITS[route])
                return False
            if (
                snapshot.values
                and not snapshot.next
                and snapshot.values.get("current_stage") == "completed"
            ):
                self.service.complete_checkpoint(
                    identifier,
                    epoch,
                    worker,
                    generation,
                    snapshot.config["configurable"]["checkpoint_id"],
                )
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
                if kind == "control":
                    # Re-read the committed saver record. The yielded value alone is not a durability certificate.
                    self.checkpoint_pause(
                        identifier, epoch, worker, generation, await graph.aget_state(config)
                    )
                else:
                    self.service.project_wait(identifier, epoch, worker, generation, kind)
                return False
            saved = await graph.aget_state(config)
            if saved.next or saved.values.get("current_stage") != "completed":
                raise DomainError("GRAPH_STATE", "图未形成持久完成检查点。", 409)
            self.service.complete_checkpoint(
                identifier, epoch, worker, generation, saved.config["configurable"]["checkpoint_id"]
            )
            return False
