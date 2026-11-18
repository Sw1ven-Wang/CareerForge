"""Control old v1 executions at their original checkpoints without migrating topology."""

from ..errors import DomainError, PauseBoundary
from .session_graph import SessionGraphAdapter, SessionState


class LegacySessionControlAdapter(SessionGraphAdapter):
    async def execute(self, identifier, epoch, worker, generation):
        with self.service.transaction(identifier, epoch, worker, control=False) as (
            _,
            _,
            workflow,
            execution,
        ):
            control = workflow.control_state
            initial = SessionState(
                execution_id=identifier,
                workflow_id=execution.workflow_id,
                goal_binding_hash=execution.goal_binding_hash,
                graph_version=execution.graph_version,
                state_schema_version=execution.state_schema_version,
                current_stage="pending",
            )
        if control in {"pause_requested", "paused", "resume_requested"}:
            config = {"configurable": {"thread_id": identifier}, "recursion_limit": 64}
            async with self.checkpointers.open() as saver:
                graph = self.graph(saver, execution, epoch, worker)
                snapshot = await graph.aget_state(config)
                if not snapshot.values and control in {"pause_requested", "resume_requested"}:
                    try:
                        await graph.ainvoke(initial, config=config, durability="sync")
                    except PauseBoundary:
                        # Original v1 START persisted before its original active-control fence.
                        pass
                    snapshot = await graph.aget_state(config)
                if not snapshot.values or any(
                    snapshot.values.get(key) != value
                    for key, value in initial.items()
                    if key != "current_stage"
                ):
                    raise DomainError("GRAPH_STATE", "旧版本执行没有匹配的持久检查点。", 409)
                checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
                if not checkpoint_id:
                    raise DomainError("GRAPH_STATE", "旧版本执行检查点未持久化。", 409)
                if (
                    not snapshot.next
                    and snapshot.values.get("current_stage") == "completed"
                    and control != "resume_requested"
                ):
                    self.service.complete_checkpoint(
                        identifier, epoch, worker, generation, checkpoint_id
                    )
                    return False
                with self.service.transaction(identifier, epoch, worker, control=False) as (
                    db,
                    conv,
                    workflow,
                    row,
                ):
                    if workflow.control_state == "resume_requested":
                        self.app.workflow_controls.resume_in(db, conv, workflow, row, checkpoint_id)
                    else:
                        command = self.app.workflow_controls.current(db, workflow)
                        self.app.workflow_controls.checkpoint_in(
                            db, conv, workflow, row, command.id, checkpoint_id
                        )
                        row.handled_generation = max(row.handled_generation, generation)
                        if row.status == "running":
                            row.status, row.wait_kind = "waiting", "control"
                            row.worker_id, row.lease_until = None, None
                            row.revision += 1
                        return False
        return await super().execute(identifier, epoch, worker, generation)
