"""Explicit new-session admission after a checker implementation upgrade.

Historical conversations and turn receipts remain immutable. Only accessible
project sources are carried forward; learning/assessment authority is not copied.
"""

from sqlalchemy import select

from .db import locked
from .errors import DomainError
from .models import Conversation, Idempotency, Principal, Turn
from .services.dependencies import require_object
from .services.ordinary_checker import (
    conversation_contract_in,
    create_conversation_contract_in,
    ordinary_checker_identity,
)
from .services.runtime import TERMINAL
from .services.workflow_status import run_family, workflow_runs
from .util import fingerprint

NOTICE = "审核实现已升级，已创建续接会话并保留项目来源；旧会话与历史审核记录保持原样。"


def renew_if_stale(app, actor, identifier, *, busy=False):
    with app.db.transaction() as db:
        locked(db, Principal, actor)
        old = app.conversations._owned(db, actor, identifier, lock=True)
        snapshot = conversation_contract_in(db, actor, old)
        identity = ordinary_checker_identity()
        if snapshot is None or snapshot["identity"] == identity:
            return identifier
        if busy or db.scalar(
            select(Turn.id).where(
                Turn.conversation_id == identifier, Turn.status.in_(["queued", "running"])
            )
        ):
            raise DomainError(
                "CONVERSATION_BUSY", "当前任务尚未结束，请结束后续接新版审核会话。", 409
            )
        workflow = app.workflows.owned_in(db, old)
        if workflow and any(
            run.status not in TERMINAL
            for _, root in workflow_runs(db, workflow.id)
            for run in run_family(db, root)
        ):
            raise DomainError("CONVERSATION_BUSY", "项目工作流尚未结束，不能续接会话。", 409)
        key = fingerprint({"conversation": identifier, "identity": identity})
        receipt = db.scalar(
            select(Idempotency).where(
                Idempotency.principal_id == actor,
                Idempotency.operation == "desk.checker_successor.v1",
                Idempotency.key == key,
            )
        )
        if receipt:
            successor = app.conversations._owned(db, actor, receipt.result["conversation_id"])
            conversation_contract_in(db, actor, successor)
            return successor.id
        sources = list(dict.fromkeys(old.focus.get("project_source_ids", [])))
        for source in sources:
            require_object(db, actor, source, "source_set")
        row = Conversation(
            principal_id=actor,
            namespace_id=old.namespace_id,
            preferences={
                "auto_build": False,
                "build_call_cap": old.preferences.get("build_call_cap", 24),
                "predecessor_conversation_id": identifier,
                "renewal_notice": NOTICE,
            },
        )
        db.add(row)
        db.flush()
        create_conversation_contract_in(
            db,
            actor,
            row,
            admission="new_conversation",
            request={"predecessor_conversation_id": identifier},
        )
        if sources or workflow:
            target = {"source_ids": sources}
            if workflow and workflow.target.get("target"):
                target["target"] = workflow.target["target"]
            app.workflows.create_in(db, row, target=target)
        db.add(
            Idempotency(
                principal_id=actor,
                operation="desk.checker_successor.v1",
                key=key,
                request_hash=key,
                result={"conversation_id": row.id},
            )
        )
        return row.id
