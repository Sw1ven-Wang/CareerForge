"""Receipt-bound cooperative controls. A checkpoint is evidence, never permission."""

from sqlalchemy import select

from ..db import locked
from ..errors import DomainError
from ..models import Outbox, Principal
from ..models_dialogue import (
    ConversationActionReceipt,
    WorkflowControlCommand,
    WorkflowExecution,
    WorkflowSession,
)
from ..util import fingerprint
from .run_pause import _uncertain, request_pause_in, resume_family, resume_precheck
from .runtime import TERMINAL, lock_run
from .session_execution import SESSION_TERMINAL
from .workflow_status import run_family, workflow_runs


class WorkflowControls:
    def __init__(self, app):
        self.app = app

    def current(self, db, workflow):
        return db.scalar(
            select(WorkflowControlCommand).where(
                WorkflowControlCommand.workflow_id == workflow.id,
                WorkflowControlCommand.generation == workflow.control_generation,
            )
        )

    def roots(self, db, workflow):
        roots = sorted(
            (run for _, run in workflow_runs(db, workflow.id)), key=lambda r: (r.funding_id, r.id)
        )
        return [lock_run(db, run.id) for run in roots]

    def validate(self, db, workflow, command, execution=None, *, receipt_required=True):
        execution = execution or self.app.session_workflows.current(db, workflow)
        if (
            command is None
            or command.workflow_id != workflow.id
            or command.generation != workflow.control_generation
        ):
            raise DomainError("CONTROL_STALE", "控制命令已被替代。", 409)
        if (command.execution_id, command.goal_binding_hash) != (
            execution.id if execution else None,
            execution.goal_binding_hash if execution else None,
        ):
            raise DomainError("CONTROL_BINDING", "控制命令与执行身份不匹配。", 409)
        if command.request_hash != fingerprint(
            dict(
                workflow_id=workflow.id,
                execution_id=command.execution_id,
                goal_binding_hash=command.goal_binding_hash,
                kind=command.kind,
            )
        ):
            raise DomainError("CONTROL_BINDING", "控制命令的固定内容已变化。", 409)
        if execution:
            self.app.session_workflows.validate_in(db, workflow, execution)
        if receipt_required:
            receipt = db.get(ConversationActionReceipt, command.action_receipt_id)
            outcomes = (
                receipt.result.get("action_receipts", [])
                if receipt and isinstance(receipt.result, dict)
                else []
            )
            actions = (
                receipt.plan.get("actions", [])
                if receipt and isinstance(receipt.plan, dict)
                else []
            )
            index = command.action_index
            if (
                receipt is None
                or receipt.conversation_id != workflow.conversation_id
                or receipt.action_plan_hash != fingerprint(receipt.plan)
                or receipt.plan.get("contract_version")
                not in {
                    "conversation-action-plan-v1",
                    "conversation-action-plan-v2",
                    "conversation-action-plan-v3",
                    "conversation-action-plan-v4",
                }
                or index >= len(outcomes)
                or index >= len(actions)
            ):
                raise DomainError("CONTROL_RECEIPT", "控制请求缺少匹配的动作回执。", 409)
            outcome, action = outcomes[index], actions[index].get("action", {})
            direct = (
                action.get("type")
                == {
                    "pause": "request_pause",
                    "resume": "resume_workflow",
                    "cancel": "cancel_workflow",
                }[command.kind]
                and outcome.get("workflow_id") == workflow.id
                and outcome.get("command_id") == command.id
            )
            child = (
                db.get(WorkflowSession, outcome.get("workflow_id"))
                if outcome.get("workflow_id")
                else None
            )
            amendment = (
                command.kind == "pause"
                and action.get("type") in {"add_learning_objective", "add_learning_topic"}
                and outcome.get("old_goal_version_id") == workflow.goal_version_id
                and child is not None
                and child.parent_workflow_id == workflow.id
                and child.goal_version_id == outcome.get("new_goal_version_id")
            )
            if (
                outcome.get("status") != "applied"
                or type(outcome.get("action_index")) is not int
                or outcome.get("action_index") != index
                or not (direct or amendment)
            ):
                raise DomainError("CONTROL_RECEIPT", "控制命令没有成功的精确动作来源。", 409)
        return command

    def _command(self, db, workflow, receipt, index, kind, execution):
        payload = dict(
            workflow_id=workflow.id,
            execution_id=execution.id if execution else None,
            goal_binding_hash=execution.goal_binding_hash if execution else None,
            kind=kind,
        )
        old = db.scalar(
            select(WorkflowControlCommand).where(
                WorkflowControlCommand.action_receipt_id == receipt.id,
                WorkflowControlCommand.action_index == index,
            )
        )
        if old:
            if old.request_hash != fingerprint(payload):
                raise DomainError("IDEMPOTENCY_CONFLICT", "控制命令身份对应不同请求。", 409)
            return old
        workflow.control_generation += 1
        command = WorkflowControlCommand(
            **payload,
            action_receipt_id=receipt.id,
            action_index=index,
            generation=workflow.control_generation,
            request_hash=fingerprint(payload),
        )
        db.add(command)
        db.flush()
        return command

    def apply_in(self, db, conv, workflow, receipt, index, kind, message=None):
        execution = self.app.session_workflows.current(db, workflow)
        execution = locked(db, WorkflowExecution, execution.id) if execution else None
        completed = bool(execution and execution.status == "completed")
        if workflow.control_state == "cancelled" or (
            execution and execution.status in SESSION_TERMINAL and not completed
        ):
            raise DomainError("TERMINAL_WORKFLOW", "会话已结束，必须新建任务。", 409)
        # Existing roots are locked Funding -> Run before any Plan/Profile authority reads.
        roots = self.roots(db, workflow)
        if execution:
            self.app.session_workflows.validate_in(db, workflow, execution, access=kind != "cancel")
        previous = self.current(db, workflow)
        legacy_pause = bool(
            execution
            and execution.graph_version == "knowledge-session-v1"
            and workflow.control_generation == 0
            and workflow.control_state == "paused"
        )
        if kind == "pause" and workflow.control_state in {"pause_requested", "paused"}:
            return {
                "control_state": workflow.control_state,
                "command_id": previous.id if previous else None,
                "reply": "暂停请求已记录，只有持久安全检查点完成后才会暂停。",
            }
        if kind == "resume":
            if workflow.control_state != "paused" or (
                not legacy_pause
                and (previous is None or previous.kind != "pause" or previous.status != "applied")
            ):
                raise DomainError("PAUSE_NOT_ACKNOWLEDGED", "尚未确认持久安全暂停点。", 409)
            if not legacy_pause:
                self.validate(db, workflow, previous, execution)
            if execution and not legacy_pause and not previous.checkpoint_id:
                raise DomainError("PAUSE_NOT_ACKNOWLEDGED", "缺少持久暂停检查点。", 409)
            families = []
            for root in roots:
                if root.status == "cancelled":
                    raise DomainError("TERMINAL_RUN", "已取消任务不可恢复。", 409)
                if root.status in TERMINAL:
                    continue
                if (
                    message
                    and message.authorization_ref
                    and message.authorization_ref != root.funding_id
                ):
                    raise DomainError("WAITING_REAUTHORIZATION", "新授权不能覆盖冻结任务。", 409)
                family = resume_precheck(db, root)
                if any(_uncertain(db, run) for run in family):
                    raise DomainError("RECEIPT_RECONCILIATION", "外部调用结果待核对。", 409)
                if any(
                    run.input_manifest.get("model_profile")
                    not in (None, self.app.provider.profile_version)
                    for run in family
                ):
                    raise DomainError(
                        "MODEL_PROFILE_CHANGED", "模型配置已变化，请建立新任务。", 409
                    )
                families.append(family)
        command = self._command(db, workflow, receipt, index, kind, execution)
        workflow.revision += 1
        if kind == "pause":
            workflow.control_state = "pause_requested"
            for root in roots:
                if root.status not in TERMINAL:
                    request_pause_in(db, root)
            if completed and execution.terminal_checkpoint_id:
                command.checkpoint_id, command.status = (
                    execution.terminal_checkpoint_id,
                    "checkpointed",
                )
                self.acknowledge_in(db, conv, workflow, receipt_required=False)
            elif completed:
                db.add(
                    Outbox(
                        kind="workflow.execute",
                        aggregate_id=execution.id,
                        payload={"kind": "terminal_control", "command_id": command.id},
                    )
                )
            elif execution:
                self.app.session_workflows.wake_in(db, workflow, execution, "control_pause")
            else:
                # No graph has been admitted. There is no checkpoint to counterfeit.
                command.status = "checkpointed"
                self.acknowledge_in(db, conv, workflow, receipt_required=False)
        elif kind == "resume":
            command.resume_command_id = previous.id if previous else None
            command.checkpoint_id = previous.checkpoint_id if previous else None
            # Execution-bound roots resume only after the worker verifies the exact saved checkpoint.
            verified_terminal = completed and execution.terminal_checkpoint_id and not legacy_pause
            workflow.control_state = (
                "resume_requested" if execution and not verified_terminal else "active"
            )
            if verified_terminal and previous.checkpoint_id != execution.terminal_checkpoint_id:
                raise DomainError("CONTROL_BINDING", "完成检查点与原暂停不匹配。", 409)
            if completed and not verified_terminal:
                db.add(
                    Outbox(
                        kind="workflow.execute",
                        aggregate_id=execution.id,
                        payload={"kind": "terminal_control", "command_id": command.id},
                    )
                )
            elif execution and not completed:
                self.app.session_workflows.wake_in(db, workflow, execution, "control_resume")
            else:
                for family in families:
                    resume_family(db, family)
                command.status = "applied"
        else:
            workflow.control_state = "cancel_requested"
            for root in roots:
                if root.status not in TERMINAL:
                    self.app.runtime.cancel_in(db, workflow.principal_id, root.id, root.revision)
            workflow.control_state = "cancelled"
            self.app.learning_requests.cancel_in(db, workflow)
            self.app.practice_workflows.cancel_in(db, workflow)
            self.app.evolution_workflows.cancel_in(db, workflow)
            command.status = "applied"
            if execution and not completed:
                execution.epoch += 1
                execution.status, execution.wait_kind = "cancelled", None
                execution.worker_id, execution.lease_until = None, None
                execution.handled_generation = execution.wake_generation
                execution.revision += 1
                for wake in db.scalars(
                    select(Outbox).where(
                        Outbox.kind == "workflow.execute", Outbox.aggregate_id == execution.id
                    )
                ):
                    wake.delivered = True
        self.app.workflows.emit_in(
            db,
            conv,
            "workflow." + kind + "_requested",
            "control:" + command.id,
            workflow,
            command_id=command.id,
            execution_id=command.execution_id,
            control_state=workflow.control_state,
        )
        return {
            "command_id": command.id,
            "execution_id": command.execution_id,
            "control_state": workflow.control_state,
            "run_refs": [run.id for run in roots],
            "reply": {
                "pause": "已请求暂停，等待持久安全检查点。",
                "resume": "已请求恢复，将核验原执行的持久检查点。",
                "cancel": "已取消当前工作流；已完成的知识发布保留。",
            }[kind],
        }

    def acknowledge_in(self, db, conv, workflow, *, receipt_required=True):
        command = self.current(db, workflow)
        if (
            workflow.control_state != "pause_requested"
            or command is None
            or command.kind != "pause"
            or command.status != "checkpointed"
        ):
            return False
        execution = self.app.session_workflows.current(db, workflow)
        self.validate(db, workflow, command, execution, receipt_required=receipt_required)
        if execution and not command.checkpoint_id:
            return False
        families = [run for root in self.roots(db, workflow) for run in run_family(db, root)]
        if any(run.status not in TERMINAL | {"paused"} or _uncertain(db, run) for run in families):
            return False
        workflow.control_state, command.status = "paused", "applied"
        workflow.revision += 1
        if execution and execution.status not in SESSION_TERMINAL:
            execution.status, execution.wait_kind = "paused", "control"
            execution.worker_id, execution.lease_until = None, None
            execution.revision += 1
        self.app.workflows.emit_in(
            db,
            conv,
            "workflow.paused",
            "control-applied:" + command.id,
            workflow,
            command_id=command.id,
            execution_id=command.execution_id,
        )
        return True

    def checkpoint_in(self, db, conv, workflow, execution, command_id, checkpoint_id):
        command = self.validate(db, workflow, self.current(db, workflow), execution)
        if (
            command.id != command_id
            or command.kind != "pause"
            or workflow.control_state not in {"pause_requested", "paused"}
        ):
            raise DomainError("CONTROL_STALE", "暂停检查点不是当前控制请求。", 409)
        command.checkpoint_id, command.status = checkpoint_id, "checkpointed"
        self.acknowledge_in(db, conv, workflow)

    def resume_in(self, db, conv, workflow, execution, checkpoint_id):
        command = self.validate(db, workflow, self.current(db, workflow), execution)
        previous = (
            db.get(WorkflowControlCommand, command.resume_command_id)
            if command.resume_command_id
            else None
        )
        legacy_pause = (
            execution.graph_version == "knowledge-session-v1"
            and command.generation == 1
            and previous is None
            and command.checkpoint_id is None
        )
        if (
            command.kind != "resume"
            or workflow.control_state != "resume_requested"
            or not checkpoint_id
            or (
                not legacy_pause
                and (
                    previous is None
                    or previous.status != "applied"
                    or previous.checkpoint_id != checkpoint_id
                    or command.checkpoint_id != checkpoint_id
                )
            )
        ):
            raise DomainError("CONTROL_BINDING", "恢复请求与原暂停检查点不匹配。", 409)
        families = []
        for root in self.roots(db, workflow):
            if root.status == "cancelled":
                raise DomainError("TERMINAL_RUN", "已取消任务不可恢复。", 409)
            if root.status not in TERMINAL:
                family = resume_precheck(db, root)
                if any(_uncertain(db, run) for run in family):
                    raise DomainError("RECEIPT_RECONCILIATION", "外部调用结果待核对。", 409)
                if any(
                    run.input_manifest.get("model_profile")
                    not in (None, self.app.provider.profile_version)
                    for run in family
                ):
                    raise DomainError(
                        "MODEL_PROFILE_CHANGED", "模型配置已变化，请建立新任务。", 409
                    )
                families.append(family)
        for family in families:
            resume_family(db, family)
        command.checkpoint_id = checkpoint_id
        workflow.control_state, command.status = "active", "applied"
        workflow.revision += 1
        self.app.workflows.emit_in(
            db,
            conv,
            "workflow.resumed",
            "control-applied:" + command.id,
            workflow,
            command_id=command.id,
            execution_id=execution.id,
        )

    async def execute_terminal(self, identifier):
        """Observe retained terminal checkpoints without reopening their execution.

        The read-only saver observation has no side effect. The short final owner
        transaction rechecks the current command, so concurrent delivery/cancel
        cannot apply the observation to another request or resume roots twice.
        """
        from ..errors import GraphCheckpointUnavailable
        from ..workflows.session_control_graph import ControlSessionGraphAdapter
        from ..workflows.session_graph import SessionGraphAdapter

        with self.app.db.read() as db:
            execution = db.get(WorkflowExecution, identifier)
            if execution is None or execution.status != "completed":
                return False
            workflow = db.get(WorkflowSession, execution.workflow_id)
            command = self.current(db, workflow)
            if workflow.current_execution_id != identifier:
                return True
            if workflow.learning_graph_version and workflow.control_state == "active":
                with self.app.db.transaction() as transaction:
                    locked(transaction, Principal, execution.principal_id)
                    conv = self.app.workflows.conversation(
                        transaction, execution.principal_id, execution.conversation_id, lock=True
                    )
                    owner = locked(transaction, WorkflowSession, workflow.id)
                    predecessor = locked(transaction, WorkflowExecution, identifier)
                    self.app.learning_workflows.handoff_in(transaction, conv, owner, predecessor)
                return True
            if (
                command is None
                or command.execution_id != identifier
                or command.status != "requested"
                or workflow.control_state not in {"pause_requested", "resume_requested"}
            ):
                return True
            command_id = command.id
        from ..workflows.evolution_session import EvolutionSessionGraphAdapter

        slot = (
            "evolution_session_graph"
            if execution.graph_version == "evolution-session-v1"
            else "session_control_graph"
            if execution.graph_version == "knowledge-session-v2"
            else "session_graph"
        )
        adapter = getattr(self.app, slot, None)
        if adapter is None and slot == "session_graph":
            adapter = getattr(self.app, "session_compat_control_graph", None)
        if adapter is None:
            adapter = (
                EvolutionSessionGraphAdapter
                if execution.graph_version == "evolution-session-v1"
                else ControlSessionGraphAdapter
                if execution.graph_version == "knowledge-session-v2"
                else SessionGraphAdapter
            )(self.app)
            setattr(self.app, slot, adapter)
        try:
            async with adapter.checkpointers.open() as saver:
                graph = adapter.graph(
                    saver, execution, execution.epoch, "terminal-control-observer"
                )
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
                raise DomainError("GRAPH_STATE", "历史完成执行缺少原始持久检查点。", 409)
            checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
            if not checkpoint_id:
                raise DomainError("GRAPH_STATE", "历史完成检查点未持久化。", 409)
            with self.app.db.transaction() as db:
                locked(db, Principal, execution.principal_id)
                conv = self.app.workflows.conversation(
                    db, execution.principal_id, execution.conversation_id, lock=True
                )
                workflow = locked(db, WorkflowSession, execution.workflow_id)
                row = locked(db, WorkflowExecution, identifier)
                command = self.current(db, workflow)
                if (
                    command is None
                    or command.id != command_id
                    or command.status != "requested"
                    or row.status != "completed"
                ):
                    return True
                self.validate(db, workflow, command, row)
                if not self.app.session_workflows.acceptance_in(db, workflow, row):
                    raise DomainError("GRAPH_APPROVAL", "历史完成仍需真实的计划采纳。", 409)
                row.terminal_checkpoint_id = checkpoint_id
                row.revision += 1
                if command.kind == "pause":
                    self.checkpoint_in(db, conv, workflow, row, command.id, checkpoint_id)
                else:
                    self.resume_in(db, conv, workflow, row, checkpoint_id)
        except GraphCheckpointUnavailable:
            # The command stays requested. Reaper regenerates a durable wake.
            return True
        except DomainError as exc:
            with self.app.db.transaction() as db:
                locked(db, Principal, execution.principal_id)
                conv = self.app.workflows.conversation(
                    db, execution.principal_id, execution.conversation_id, lock=True
                )
                workflow = locked(db, WorkflowSession, execution.workflow_id)
                row = locked(db, WorkflowExecution, identifier)
                command = self.current(db, workflow)
                if command and command.id == command_id and command.status == "requested":
                    command.status = "blocked"
                    row.last_error_code = exc.code
                    self.app.workflows.emit_in(
                        db,
                        conv,
                        "workflow.control_blocked",
                        "control-blocked:" + command.id,
                        workflow,
                        command_id=command.id,
                        code=exc.code,
                    )
        return True
