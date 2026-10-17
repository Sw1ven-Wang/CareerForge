"""One-level durable review delegation with root-first locking and monotonic call charges."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application.common import fingerprint
from careerforge.domain.errors import BusinessError
from careerforge.domain.json_types import JsonObject as Json
from careerforge.domain.runs import TERMINAL, LeaseLost
from careerforge.infrastructure.db.models import (
    AgentRun,
    ModelCallReceipt,
    OutboxMessage,
    RunAllocation,
)

BATCH_VERSION = "agent-study-review-batch-1.0"
CHILD_VERSION = "agent-study-review-child-1.0"


def lock_run(db: Session, identifier: UUID) -> AgentRun | None:
    parent_id = db.scalar(select(AgentRun.parent_run_id).where(AgentRun.id == identifier))
    if parent_id:
        db.scalar(
            select(AgentRun)
            .where(AgentRun.id == parent_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    return db.scalar(
        select(AgentRun)
        .where(AgentRun.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def allocation(db: Session, child: AgentRun) -> RunAllocation:
    item = db.scalar(select(RunAllocation).where(RunAllocation.child_run_id == child.id))
    if item is None or item.parent_run_id != child.parent_run_id or item.owner_id != child.owner_id:
        raise BusinessError(409, "TASK_ALLOCATION_MISSING", "子任务预算关系不可用。")
    return item


def parent_guard(db: Session, child: AgentRun) -> AgentRun:
    if not child.parent_run_id:
        raise ValueError("Expected a child run")
    parent = db.get(AgentRun, child.parent_run_id)
    if not parent or parent.owner_id != child.owner_id or parent.status in TERMINAL:
        raise LeaseLost("Parent stopped; child cannot continue or publish")
    if allocation(db, child).settled_at:
        raise LeaseLost("Child allocation already settled")
    return parent


def create_child(
    db: Session, parent: AgentRun, task_key: str, manifest: Json, calls: int, token_cap: int
) -> AgentRun:
    from careerforge.application.run_service import create_run, event

    if (
        parent.parent_run_id
        or parent.workflow_version != BATCH_VERSION
        or parent.status != "running"
    ):
        raise BusinessError(409, "DELEGATION_FORBIDDEN", "仅有界审核父任务可分发一层子任务。")
    previous = db.scalar(
        select(RunAllocation).where(
            RunAllocation.parent_run_id == parent.id, RunAllocation.task_key == task_key
        )
    )
    if previous:
        child = db.get(AgentRun, previous.child_run_id)
        if (
            not child
            or child.input_manifest_hash != fingerprint(manifest)
            or previous.allocated_calls != calls
            or previous.output_token_limit != token_cap
        ):
            raise BusinessError(409, "TASK_CONFLICT", "相同子任务键已有不同范围或预算。")
        return child
    existing = list(
        db.scalars(select(RunAllocation).where(RunAllocation.parent_run_id == parent.id))
    )
    if len(existing) >= 2 or not 1 <= calls <= 3 or not 512 <= token_cap <= 8192:
        raise BusinessError(422, "CHILD_TASK_LIMIT", "最多两个子任务，每个最多三次调用。")
    if parent.calls_reserved + parent.held_calls + calls > parent.call_budget:
        raise BusinessError(409, "ROOT_BUDGET_UNAVAILABLE", "父任务剩余额度不足，未创建子任务。")
    if token_cap > parent.input_manifest["max_output_tokens"]:
        raise BusinessError(422, "CHILD_TOKEN_LIMIT", "子任务不能提高父任务输出上限。")
    child = create_run(
        db,
        parent.owner_id,
        "project_research",
        CHILD_VERSION,
        manifest,
        call_budget=calls,
        parent_run_id=parent.id,
        deadline_at=parent.deadline_at,
    )
    parent.held_calls += calls
    db.add(
        RunAllocation(
            owner_id=parent.owner_id,
            parent_run_id=parent.id,
            child_run_id=child.id,
            task_key=task_key,
            allocated_calls=calls,
            output_token_limit=token_cap,
        )
    )
    event(db, parent, "task.created", {"child_run_id": str(child.id), "task_key": task_key})
    event(
        db,
        parent,
        "budget.reserved",
        {"child_run_id": str(child.id), "calls": calls, "max_output_tokens": token_cap},
    )
    db.flush()
    return child


def charge_child(db: Session, child: AgentRun, request: Json) -> None:
    parent = parent_guard(db, child)
    item = allocation(db, child)
    if request["max_output_tokens"] > item.output_token_limit:
        raise BusinessError(422, "CHILD_TOKEN_LIMIT", "模型请求超出已预留的子任务输出上限。")
    if child.calls_reserved >= item.allocated_calls or parent.held_calls <= 0:
        raise BusinessError(409, "CHILD_BUDGET_UNAVAILABLE", "子任务没有未使用的预留额度。")
    parent.held_calls -= 1
    parent.calls_reserved += 1
    from careerforge.application.run_service import event

    event(
        db,
        parent,
        "budget.consumed",
        {
            "child_run_id": str(child.id),
            "consumed_calls": parent.calls_reserved,
            "held_calls": parent.held_calls,
        },
    )


def terminal(db: Session, run: AgentRun) -> None:
    from careerforge.application.run_service import db_now, event, transition

    if run.parent_run_id:
        parent = db.get(AgentRun, run.parent_run_id)
        item = allocation(db, run)
        if item.settled_at:
            return
        unused = item.allocated_calls - run.calls_reserved
        if not parent or unused < 0 or parent.held_calls < unused:
            raise BusinessError(409, "BUDGET_LEDGER_CONFLICT", "子任务预算账本不一致。")
        parent.held_calls -= unused
        item.released_calls = unused
        item.settled_at = db_now(db)
        for receipt in db.scalars(
            select(ModelCallReceipt).where(
                ModelCallReceipt.run_id == run.id, ModelCallReceipt.status == "reserved"
            )
        ):
            receipt.status = "unknown"
        event(
            db,
            parent,
            "budget.settled",
            {
                "child_run_id": str(run.id),
                "consumed": run.calls_reserved,
                "released": unused,
                "status": run.status,
            },
        )
        db.flush()
        pending = db.scalar(
            select(AgentRun.id)
            .where(AgentRun.parent_run_id == parent.id, AgentRun.status.not_in(TERMINAL))
            .limit(1)
        )
        if not pending and parent.status == "waiting_children":
            transition(db, parent, "queued", event_kind="run.children_ready")
            db.add(OutboxMessage(run_id=parent.id))
        return
    # The parent lock is held first. Siblings use this same lock order.
    for child_id in db.scalars(
        select(AgentRun.id).where(AgentRun.parent_run_id == run.id).order_by(AgentRun.id)
    ).all():
        child = lock_run(db, child_id)
        if child and child.status not in TERMINAL:
            child.epoch += 1
            event(
                db,
                child,
                "task.parent_stopped",
                {"parent_run_id": str(run.id), "status": run.status},
            )
            transition(db, child, "cancelled")


def expire_roots(db: Session) -> None:
    from careerforge.application.run_service import db_now, event, transition

    candidates = db.scalars(
        select(AgentRun.id)
        .where(
            AgentRun.parent_run_id.is_(None),
            AgentRun.deadline_at <= db_now(db),
            AgentRun.status.not_in(TERMINAL),
        )
        .limit(100)
    ).all()
    for identifier in candidates:
        row = lock_run(db, identifier)
        if row and row.status not in TERMINAL and row.deadline_at and row.deadline_at <= db_now(db):
            row.epoch += 1
            event(db, row, "task.deadline_exceeded", {"deadline": row.deadline_at.isoformat()})
            transition(db, row, "failed")


def family_state(db: Session, run: AgentRun) -> Json:
    root_id = run.parent_run_id or run.id
    root = db.get(AgentRun, root_id)
    if not root or root.owner_id != run.owner_id:
        raise BusinessError(404, "NOT_FOUND", "父任务不存在。")
    rows = db.scalars(
        select(RunAllocation)
        .where(RunAllocation.parent_run_id == root_id, RunAllocation.owner_id == run.owner_id)
        .order_by(RunAllocation.created_at, RunAllocation.child_run_id)
    ).all()
    children = []
    for item in rows:
        child = db.get(AgentRun, item.child_run_id)
        if not child:
            continue
        receipts = list(
            db.scalars(select(ModelCallReceipt).where(ModelCallReceipt.run_id == child.id))
        )
        children.append(
            {
                "run_id": str(child.id),
                "task_key": item.task_key,
                "status": child.status,
                "allocated_calls": item.allocated_calls,
                "consumed_calls": child.calls_reserved,
                "released_calls": item.released_calls,
                "held_calls": 0 if item.settled_at else item.allocated_calls - child.calls_reserved,
                "settled": item.settled_at is not None,
                "usage": [{"status": r.status, "usage": r.usage} for r in receipts],
            }
        )
    return {
        "root_run_id": str(root_id),
        "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
        "call_limit": root.call_budget,
        "consumed_calls": root.calls_reserved,
        "held_calls": root.held_calls,
        "available_calls": root.call_budget - root.calls_reserved - root.held_calls,
        "deadline": root.deadline_at.isoformat() if root.deadline_at else None,
        "children": children,
        "accounting_note": (
            "已发出或结果未知的调用不退额度；只释放未用预留。供应商用量单列，金额未定价。"
        ),
    }
