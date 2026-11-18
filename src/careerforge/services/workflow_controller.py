"""Workflow ownership, links and replayable conversation activity."""

from sqlalchemy import func, select

from ..db import locked
from ..errors import DomainError
from ..models import Outbox, Principal, RunEvent, SourceSet, StreamEvent, Turn
from ..models_dialogue import ConversationActivityEvent, WorkflowRunLink, WorkflowSession
from .runtime import TERMINAL
from .workflow_status import StatusProjector, run_family, workflow_runs


class WorkflowController:
    def __init__(self, app):
        self.app, self.db = app, app.db
        self.projector = StatusProjector(app)

    def conversation(self, db, actor, conversation_id, *, lock=False):
        return self.app.conversations._owned(db, actor, conversation_id, lock=lock)

    def owned_in(self, db, conversation, workflow_id=None):
        identifier = workflow_id or conversation.focus.get("active_workflow_id")
        workflow = db.get(WorkflowSession, identifier) if identifier else None
        if identifier and (
            workflow is None
            or workflow.conversation_id != conversation.id
            or workflow.principal_id != conversation.principal_id
            or workflow.namespace_id != conversation.namespace_id
        ):
            raise DomainError("NOT_FOUND", "工作流不存在。", 404)
        return workflow

    def create_in(self, db, conversation, *, parent=None, target=None):
        workflow = WorkflowSession(
            orchestration_engine=self.app.settings.workflow_engine,
            session_graph_version=self.app.settings.session_graph_version,
            learning_graph_version=self.app.settings.learning_graph_version,
            conversation_id=conversation.id,
            principal_id=conversation.principal_id,
            namespace_id=conversation.namespace_id,
            parent_workflow_id=parent.id if parent else None,
            target=target or {},
        )
        db.add(workflow)
        db.flush()
        self.focus_in(db, conversation, workflow)
        self.emit_in(db, conversation, "workflow.created", "workflow:" + workflow.id, workflow)
        return workflow

    def focus_in(self, db, conversation, workflow):
        focus = {**conversation.focus}
        if focus.get("active_workflow_id") != workflow.id:
            # Project focus, checker continuations and retrieval memory are goal-bound.
            focus = {
                key: value
                for key, value in focus.items()
                if key
                not in {
                    "project_source_ids",
                    "topic_keys",
                    "last_retrieval_manifest_id",
                    "evidence_answer_state",
                    "evidence_continuity_contract",
                    "resolved_scope",
                }
            }
        focus.update(
            active_workflow_id=workflow.id, active_goal_version_id=workflow.goal_version_id
        )
        sources = workflow.target.get("source_ids") or (
            [workflow.target["source_id"]] if workflow.target.get("source_id") else []
        )
        if sources:
            focus["project_source_ids"] = list(sources)
        conversation.focus = focus
        conversation.revision += 1

    def link_in(self, db, workflow, run, milestone):
        if (
            run is None
            or run.parent_id
            or run.principal_id != workflow.principal_id
            or run.namespace_id != workflow.namespace_id
        ):
            raise DomainError("WORKFLOW_RUN_SCOPE", "任务不属于当前工作流。", 409)
        previous = db.scalar(select(WorkflowRunLink).where(WorkflowRunLink.run_id == run.id))
        if previous is not None:
            if previous.workflow_id != workflow.id or previous.milestone != milestone:
                raise DomainError("WORKFLOW_RUN_SCOPE", "任务已绑定到其他工作流。", 409)
            return previous
        source_id = run.input_manifest.get("source_id")
        source = db.get(SourceSet, source_id) if source_id else None
        ordinal = (
            db.scalar(
                select(func.max(WorkflowRunLink.ordinal)).where(
                    WorkflowRunLink.workflow_id == workflow.id,
                    WorkflowRunLink.milestone == milestone,
                )
            )
            or 0
        )
        link = WorkflowRunLink(
            workflow_id=workflow.id,
            run_id=run.id,
            milestone=milestone,
            ordinal=ordinal + 1,
            goal_version_id=workflow.goal_version_id,
            source_scope_hash=source.scope_hash if source else None,
        )
        db.add(link)
        db.flush()
        return link

    def acknowledge_pause(self, message_id, root_id):
        with self.db.transaction() as db:
            link = db.scalar(select(WorkflowRunLink).where(WorkflowRunLink.run_id == root_id))
            workflow = db.get(WorkflowSession, link.workflow_id) if link else None
            if workflow:
                locked(db, Principal, workflow.principal_id)
                try:
                    conv = self.conversation(
                        db,
                        workflow.principal_id,
                        workflow.conversation_id,
                        lock=True,
                    )
                    workflow = self.owned_in(db, conv, workflow.id)
                    db.refresh(workflow)
                except DomainError:
                    workflow = None
            message = locked(db, Outbox, message_id)
            if message is None or message.delivered:
                return
            if workflow and workflow.control_state == "pause_requested":
                if workflow.control_generation > 0:
                    self.app.workflow_controls.acknowledge_in(db, conv, workflow)
                    message.delivered = True
                    return
                roots = workflow_runs(db, workflow.id)
                if all(
                    run.status in TERMINAL | {"paused"}
                    for _, root in roots
                    for run in run_family(db, root)
                ):
                    workflow.control_state = "paused"
                    workflow.revision += 1
                    self.emit_in(
                        db,
                        conv,
                        "workflow.paused",
                        f"paused:{workflow.id}:{workflow.revision}",
                        workflow,
                    )
            message.delivered = True

    def status(self, actor, conversation_id, workflow_id=None):
        with self.db.read() as db:
            conv = self.conversation(db, actor, conversation_id)
            status = self.projector.current(db, actor, conv, self.owned_in(db, conv, workflow_id))
            status["event_cursor"] = (
                db.scalar(
                    select(func.max(ConversationActivityEvent.seq)).where(
                        ConversationActivityEvent.conversation_id == conv.id
                    )
                )
                or 0
            )
            return status

    def list(self, actor, conversation_id):
        with self.db.read() as db:
            conv = self.conversation(db, actor, conversation_id)
            active = self.owned_in(db, conv)
            rows = list(
                db.scalars(
                    select(WorkflowSession)
                    .where(WorkflowSession.conversation_id == conv.id)
                    .order_by(WorkflowSession.created_at, WorkflowSession.id)
                )
            )
            return {
                "active_workflow_id": active.id if active else None,
                "focus_revision": conv.revision,
                "workflows": [
                    self.projector.current(db, actor, conv, self.owned_in(db, conv, row.id))
                    for row in rows
                ],
            }

    def emit_in(self, db, conv, kind, origin_key, workflow=None, **reference):
        # Callers hold the Conversation row lock for both allocation and deduplication.
        old = db.scalar(
            select(ConversationActivityEvent).where(
                ConversationActivityEvent.conversation_id == conv.id,
                ConversationActivityEvent.origin_key == origin_key,
            )
        )
        if old:
            return old
        seq = (
            db.scalar(
                select(func.max(ConversationActivityEvent.seq)).where(
                    ConversationActivityEvent.conversation_id == conv.id
                )
            )
            or 0
        )
        row = ConversationActivityEvent(
            conversation_id=conv.id,
            workflow_id=workflow.id if workflow else None,
            seq=seq + 1,
            kind=kind,
            origin_key=origin_key,
            reference=reference,
        )
        db.add(row)
        db.flush()
        return row

    def activity(self, actor, conversation_id, after):
        with self.db.transaction() as db:
            conv = self.conversation(db, actor, conversation_id, lock=True)
            work = list(
                db.scalars(
                    select(WorkflowSession).where(WorkflowSession.conversation_id == conv.id)
                )
            )
            known = set(
                db.scalars(
                    select(ConversationActivityEvent.origin_key).where(
                        ConversationActivityEvent.conversation_id == conv.id
                    )
                )
            )
            candidates = []
            for row in work:
                self.owned_in(db, conv, row.id)
                for _, root in workflow_runs(db, row.id):
                    for run in run_family(db, root):
                        for e in db.scalars(select(RunEvent).where(RunEvent.run_id == run.id)):
                            key = f"run:{e.run_id}:{e.seq}"
                            if key not in known:
                                reference = {"run_id": e.run_id, "source_seq": e.seq}
                                reference.update(
                                    {
                                        k: e.payload[k]
                                        for k in ("key", "status", "code")
                                        if k in e.payload
                                    }
                                )
                                candidates.append((e.created_at, key, e.event_type, row, reference))
            workflow_map = {row.id: row for row in work}
            for turn in db.scalars(select(Turn).where(Turn.conversation_id == conv.id)):
                row = workflow_map.get(turn.focus.get("active_workflow_id"))
                for e in db.scalars(select(StreamEvent).where(StreamEvent.turn_id == turn.id)):
                    key = f"turn:{e.turn_id}:{e.seq}"
                    if key not in known:
                        candidates.append(
                            (
                                e.created_at,
                                key,
                                e.event_type,
                                row,
                                {
                                    "turn_id": e.turn_id,
                                    "source_seq": e.seq,
                                },
                            )
                        )
            for _, key, kind, row, reference in sorted(
                candidates, key=lambda item: (item[0], item[1])
            ):
                self.emit_in(db, conv, kind, key, row, **reference)
            items = list(
                db.scalars(
                    select(ConversationActivityEvent)
                    .where(
                        ConversationActivityEvent.conversation_id == conv.id,
                        ConversationActivityEvent.seq > after,
                    )
                    .order_by(ConversationActivityEvent.seq)
                    .limit(150)
                )
            )
            return [
                {
                    "seq": row.seq,
                    "kind": row.kind,
                    "workflow_id": row.workflow_id,
                    "reference": row.reference,
                    "source_event_id": row.origin_key,
                }
                for row in items
            ]
