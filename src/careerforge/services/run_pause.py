"""Cooperative safe points for durable runtime execution and provider settlement."""

from sqlalchemy import select, update

from ..db import aware, clock
from ..errors import DomainError, PauseBoundary
from ..models import AgentRun, ExternalRequest, FundingAgreement, ModelReceipt, Outbox


def pause_requested(db, run):
    parent = db.get(AgentRun, run.parent_id) if run.parent_id else None
    return run.pause_requested or bool(parent and parent.pause_requested)


def ensure_unpaused(db, run):
    if pause_requested(db, run):
        raise PauseBoundary()


def _uncertain(db, run):
    return any(
        db.scalar(
            select(row.id)
            .where(row.run_id == run.id, row.status.in_(("attempted", "unknown")))
            .limit(1)
        )
        for row in (ModelReceipt, ExternalRequest)
    )


def park(db, run):
    from .runtime import event

    unresolved = _uncertain(db, run)
    run.status = "pause_pending_inflight" if unresolved else "paused"
    run.pause_requested = True
    run.worker_id = None
    run.lease_until = None
    run.revision += 1
    if not unresolved:
        db.add(
            Outbox(
                kind="workflow.pause_checkpoint", aggregate_id=run.parent_id or run.id, payload={}
            )
        )
    event(
        db,
        run,
        "run.pause_pending_inflight" if unresolved else "run.paused",
        {
            "status": run.status,
        },
    )


def request_pause_in(db, root):
    from .runtime import TERMINAL, event

    family = [
        root,
        *db.scalars(
            select(AgentRun)
            .where(AgentRun.parent_id == root.id)
            .order_by(AgentRun.id)
            .with_for_update()
        ),
    ]
    for run in family:
        if run.status in TERMINAL or run.pause_requested:
            continue
        run.pause_requested = True
        run.revision += 1
        event(db, run, "run.pause_requested", {"status": run.status})
        if run.status in {"queued", "waiting_children"}:
            park(db, run)
    return family


def resume_precheck(db, root):
    from .dependencies import check_closure
    from .runtime import TERMINAL, active_funding, declared_manifest_dependencies

    family = [
        root,
        *db.scalars(
            select(AgentRun)
            .where(AgentRun.parent_id == root.id)
            .order_by(AgentRun.id)
            .with_for_update()
        ),
    ]
    if root.status in TERMINAL:
        raise DomainError("TERMINAL_RUN", "任务已结束，必须新建任务。", 409)
    if any(run.status not in TERMINAL | {"paused"} for run in family):
        if any(run.status == "pause_pending_inflight" for run in family):
            raise DomainError("RECEIPT_RECONCILIATION", "外部调用结果待核对，暂不可恢复。", 409)
        raise DomainError("PAUSE_NOT_ACKNOWLEDGED", "后台还未到达安全暂停点。", 409)
    for run in family:
        if run.status in TERMINAL:
            continue
        active_funding(db, run)
        if aware(run.deadline_at) <= clock(db):
            raise DomainError("WAITING_REAUTHORIZATION", "原任务期限已过，请授权新任务。", 409)
        check_closure(db, run.principal_id, declared_manifest_dependencies(run.input_manifest))
        if run.input_manifest.get("source_id"):
            from .sources import SourceService

            SourceService.get(db, run.principal_id, run.input_manifest["source_id"])
    return family


def resume_family(db, family):
    from .runtime import TERMINAL, event

    root, *children = family
    for run in family:
        if run.status != "paused":
            continue
        run.pause_requested = False
        run.revision += 1
        run.status = (
            "waiting_children"
            if run is root and any(child.status not in TERMINAL for child in children)
            else "queued"
        )
        if run.status == "queued":
            db.add(Outbox(kind="run", aggregate_id=run.id, payload={}))
        event(db, run, "run.resumed", {"status": run.status})


def acknowledge_in(db, run):
    if run.status != "running" or not pause_requested(db, run):
        return False
    park(db, run)
    return True


def retract_undispatched_model(db, run, receipt_id, epoch):
    """Reverse only a reservation which this worker provably never dispatched."""
    from .runtime import event

    receipt = db.get(ModelReceipt, receipt_id)
    if (
        run.status != "running"
        or run.epoch != epoch
        or receipt is None
        or receipt.status != "attempted"
        or receipt.epoch != epoch
    ):
        return
    funding = db.get(FundingAgreement, run.funding_id)
    run.calls_consumed -= 1
    funding.calls_consumed -= 1
    funding.calls_held += 1
    if run.parent_id:
        parent = db.get(AgentRun, run.parent_id)
        parent.calls_consumed -= 1
        parent.held_calls += 1
    receipt.status = "not_dispatched"
    event(db, run, "model.not_dispatched", {"receipt_id": receipt.id})


def stop_expired_worker(db, run):
    for row in (ModelReceipt, ExternalRequest):
        db.execute(
            update(row)
            .where(row.run_id == run.id, row.status == "attempted")
            .values(status="unknown")
        )
    run.epoch += 1
    park(db, run)
