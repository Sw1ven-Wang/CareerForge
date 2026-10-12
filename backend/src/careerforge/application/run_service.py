from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.application.common import check_revision, fingerprint
from careerforge.domain.errors import BusinessError
from careerforge.domain.runs import TERMINAL, TRANSITIONS, LeaseLost
from careerforge.infrastructure.db.models import (
    AgentRun,
    ModelCallReceipt,
    OutboxMessage,
    ResumeDecision,
    RunAttempt,
    RunEvent,
    StageArtifact,
    WaitRequest,
)


def db_now(db: Session) -> datetime:
    # clock_timestamp advances even in a long transaction; now() does not.
    from sqlalchemy import func

    return cast(datetime, db.scalar(select(func.clock_timestamp())))


def owned_run(db: Session, owner: UUID, run_id: UUID) -> AgentRun:
    run = db.scalar(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.owner_id == owner).with_for_update()
    )
    if run is None:
        raise BusinessError(404, "NOT_FOUND", "运行不存在。")
    return run


def event(db: Session, run: AgentRun, kind: str, payload: dict[str, Any]) -> None:
    run.event_seq += 1
    db.add(RunEvent(run_id=run.id, seq=run.event_seq, type=kind, payload=payload))


def transition(db: Session, run: AgentRun, status: str, event_kind: str | None = None) -> None:
    if status not in TRANSITIONS[run.status]:
        raise BusinessError(409, "INVALID_RUN_STATE", "当前运行状态不允许此操作。")
    run.status = status
    run.version += 1
    if status != "running":
        run.worker_id = None
        run.lease_until = None
    kind = {"running": "run.started", "queued": "run.recovering"}.get(status, "run." + status)
    if status in TERMINAL and status != "cancelled":
        kind = "run.finished"
    event(db, run, event_kind or kind, {"status": status, "version": run.version})


def create_run(
    db: Session,
    owner: UUID,
    workflow_type: str,
    workflow_version: str,
    manifest: dict[str, Any],
    call_budget: int = 3,
    goal_version_id: UUID | None = None,
) -> AgentRun:
    if (
        workflow_type
        not in {"market_research", "alignment", "project_research", "learning_path", "assessment"}
        or call_budget < 0
    ):
        raise ValueError("Invalid workflow or budget")
    run = AgentRun(
        owner_id=owner,
        goal_version_id=goal_version_id,
        workflow_type=workflow_type,
        workflow_version=workflow_version,
        input_manifest=manifest,
        input_manifest_hash=fingerprint(manifest),
        call_budget=call_budget,
    )
    db.add(run)
    db.flush()
    event(db, run, "run.created", {"status": "queued", "version": run.version})
    db.add(OutboxMessage(run_id=run.id))
    return run


def claim(db: Session, run_id: UUID, worker: str, lease_seconds: int = 60) -> int | None:
    run = db.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
    if run is None or run.status != "queued":
        return None
    run.epoch += 1
    transition(db, run, "running")
    run.worker_id = worker
    run.lease_until = db_now(db) + timedelta(seconds=lease_seconds)
    db.add(RunAttempt(run_id=run.id, epoch=run.epoch, worker_id=worker))
    return run.epoch


def leased(db: Session, run_id: UUID, epoch: int, worker: str) -> AgentRun:
    run = db.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
    if (
        run is None
        or run.status != "running"
        or run.epoch != epoch
        or run.worker_id != worker
        or run.lease_until is None
        or run.lease_until <= db_now(db)
    ):
        raise LeaseLost("Expired or replaced execution lease")
    return run


def heartbeat(db: Session, run_id: UUID, epoch: int, worker: str) -> None:
    run = leased(db, run_id, epoch, worker)
    run.lease_until = db_now(db) + timedelta(seconds=60)


def reap(db: Session, limit: int = 100) -> int:
    from sqlalchemy import func

    runs = db.scalars(
        select(AgentRun)
        .where(AgentRun.status == "running", AgentRun.lease_until <= func.clock_timestamp())
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    count = 0
    for run in runs:
        # Recheck under row lock: the candidate scan grants no execution rights.
        if run.status != "running" or run.lease_until is None or run.lease_until > db_now(db):
            continue
        receipts = db.scalars(
            select(ModelCallReceipt).where(
                ModelCallReceipt.run_id == run.id, ModelCallReceipt.status == "reserved"
            )
        ).all()
        for receipt in receipts:
            receipt.status = "unknown"
        run.epoch += 1
        transition(db, run, "queued")
        db.add(OutboxMessage(run_id=run.id))
        count += 1
    return count


def artifact(db: Session, run: AgentRun, key: str, version: str) -> StageArtifact | None:
    return db.scalar(
        select(StageArtifact).where(
            StageArtifact.run_id == run.id,
            StageArtifact.stage_key == key,
            StageArtifact.input_fingerprint == run.input_manifest_hash,
            StageArtifact.stage_version == version,
        )
    )


def commit_stage(
    db: Session,
    run_id: UUID,
    epoch: int,
    worker: str,
    key: str,
    version: str,
    payload: dict[str, Any],
) -> StageArtifact:
    run = leased(db, run_id, epoch, worker)
    old = artifact(db, run, key, version)
    if old is not None:
        if fingerprint(old.payload) != fingerprint(payload):
            raise BusinessError(409, "STAGE_CONFLICT", "阶段已保存不同内容。")
        return old
    row = StageArtifact(
        run_id=run.id,
        stage_key=key,
        stage_version=version,
        input_fingerprint=run.input_manifest_hash,
        payload=payload,
    )
    db.add(row)
    event(db, run, "stage.completed", {"stage_key": key, "stage_version": version})
    db.flush()
    return row


def wait_for_choice(
    db: Session,
    run_id: UUID,
    epoch: int,
    worker: str,
    key: str,
    question: dict[str, Any],
    choices: list[str],
    question_version: str = "1",
    wait_sequence: int = 1,
) -> dict[str, Any] | None:
    run = leased(db, run_id, epoch, worker)
    schema = {
        "type": "object",
        "properties": {"choice": {"enum": choices}},
        "required": ["choice"],
        "additionalProperties": False,
    }
    wait = db.scalar(
        select(WaitRequest).where(
            WaitRequest.run_id == run.id,
            WaitRequest.stage_key == key,
            WaitRequest.input_fingerprint == run.input_manifest_hash,
            WaitRequest.question_version == question_version,
            WaitRequest.wait_sequence == wait_sequence,
        )
    )
    if wait is None:
        wait = WaitRequest(
            run_id=run.id,
            stage_key=key,
            input_fingerprint=run.input_manifest_hash,
            question_version=question_version,
            wait_sequence=wait_sequence,
            question=question,
            response_schema=schema,
        )
        db.add(wait)
        db.flush()
    elif wait.question != question or wait.response_schema != schema:
        raise BusinessError(409, "WAIT_CONFLICT", "同一等待身份的问题不可更改。")
    decision = db.get(ResumeDecision, wait.id)
    if decision is not None:
        return decision.response
    transition(db, run, "waiting_user")
    event(db, run, "wait.requested", {"wait_request_id": str(wait.id)})
    return None


def resume(
    db: Session,
    owner: UUID,
    run_id: UUID,
    expected_version: int,
    wait_id: UUID,
    manifest_hash: str,
    response: dict[str, Any],
) -> AgentRun:
    run = owned_run(db, owner, run_id)
    wait = db.scalar(
        select(WaitRequest).where(WaitRequest.id == wait_id, WaitRequest.run_id == run.id)
    )
    if wait is None:
        raise BusinessError(404, "NOT_FOUND", "等待项不存在。")
    if manifest_hash != run.input_manifest_hash:
        raise BusinessError(409, "INPUT_MANIFEST_CONFLICT", "输入版本已不匹配。")
    old = db.get(ResumeDecision, wait.id)
    if old is not None:
        if old.response_hash != fingerprint(response):
            raise BusinessError(409, "DECISION_CONFLICT", "此等待项已有不同决定。")
        return run
    check_revision(run.version, expected_version)
    if (
        set(response) != {"choice"}
        or response["choice"] not in wait.response_schema["properties"]["choice"]["enum"]
    ):
        raise BusinessError(422, "INVALID_WAIT_RESPONSE", "请选择一个有效选项。")
    if run.status != "waiting_user":
        raise BusinessError(409, "INVALID_RUN_STATE", "运行当前不在等待决定。")
    db.add(
        ResumeDecision(
            wait_request_id=wait.id, response=response, response_hash=fingerprint(response)
        )
    )
    transition(db, run, "queued", event_kind="run.resumed")
    db.add(OutboxMessage(run_id=run.id))
    return run


def cancel(db: Session, owner: UUID, run_id: UUID, expected_version: int) -> AgentRun:
    run = owned_run(db, owner, run_id)
    if run.status == "cancelled":
        return run
    check_revision(run.version, expected_version)
    if run.status in TERMINAL:
        raise BusinessError(409, "INVALID_RUN_STATE", "运行已结束。")
    run.epoch += 1
    transition(db, run, "cancelled")
    return run


def reserve_call(
    db: Session, run_id: UUID, epoch: int, worker: str, request: dict[str, Any]
) -> UUID | None:
    run = leased(db, run_id, epoch, worker)
    if run.calls_reserved >= run.call_budget:
        transition(db, run, "budget_stopped")
        return None
    run.calls_reserved += 1
    row = ModelCallReceipt(run_id=run.id, epoch=epoch, request_hash=fingerprint(request))
    db.add(row)
    db.flush()
    return row.id
