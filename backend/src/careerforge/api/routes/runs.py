import asyncio
import base64
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.application.common import fingerprint, idempotency
from careerforge.application.run_service import cancel, owned_run, resume
from careerforge.domain.errors import BusinessError
from careerforge.domain.runs import TERMINAL
from careerforge.infrastructure.db.models import (
    AgentRun,
    ResumeDecision,
    RunEvent,
    StageArtifact,
    UserSession,
    WaitRequest,
)
from careerforge.infrastructure.db.session import get_engine

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


class ResumeInput(BaseModel):
    expected_version: int = Field(ge=1)
    wait_request_id: UUID
    input_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response: dict[str, Any]


class CancelInput(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=1000)


def result(db: Session, run: AgentRun) -> dict[str, Any]:
    waits = db.scalars(select(WaitRequest).where(WaitRequest.run_id == run.id)).all()
    stages = db.scalars(
        select(StageArtifact)
        .where(StageArtifact.run_id == run.id)
        .order_by(StageArtifact.created_at)
    ).all()
    waiting = None
    if run.status == "waiting_user":
        pending = [w for w in waits if db.get(ResumeDecision, w.id) is None]
        if pending:
            w = pending[-1]
            waiting = {
                "wait_request_id": str(w.id),
                "question_version": w.question_version,
                "question": w.question,
                "response_schema": w.response_schema,
                "schema_hash": fingerprint(w.response_schema),
                "input_manifest_hash": run.input_manifest_hash,
                "expires_at": None,
            }
    return {
        "run_id": str(run.id),
        "run_type": run.workflow_type,
        "workflow_version": run.workflow_version,
        "status": run.status,
        "version": run.version,
        "input_manifest_hash": run.input_manifest_hash,
        "goal_version_id": str(run.goal_version_id) if run.goal_version_id else None,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "last_event_seq": run.event_seq,
        "waiting_input": waiting,
        "stage_summary": [{"stage_key": a.stage_key, "status": "completed"} for a in stages],
        "output_refs": [
            {
                "stage_artifact_id": str(a.id),
                **(
                    {"market_report_id": a.payload["market_report_id"]}
                    if "market_report_id" in a.payload
                    else {}
                ),
            }
            for a in stages
            if a.stage_key == "result"
        ],
        "usage": {"call_limit": run.call_budget, "calls_reserved": run.calls_reserved},
        "error_summary": None,
    }


@router.get("")
def list_runs(
    request: Request,
    db: Database,
    user: Authenticated,
    status: Annotated[list[str] | None, Query()] = None,
    run_type: str | None = None,
    goal_version_id: UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    stmt = select(AgentRun).where(AgentRun.owner_id == user.id)
    if status:
        stmt = stmt.where(AgentRun.status.in_(status))
    if run_type:
        stmt = stmt.where(AgentRun.workflow_type == run_type)
    if goal_version_id:
        stmt = stmt.where(AgentRun.goal_version_id == goal_version_id)
    if cursor:
        try:
            date, identity = json.loads(base64.urlsafe_b64decode(cursor).decode())
            boundary = datetime.fromisoformat(date)
            if boundary.tzinfo is None:
                raise ValueError("Timezone missing")
            stmt = stmt.where(tuple_(AgentRun.created_at, AgentRun.id) < (boundary, UUID(identity)))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise BusinessError(400, "INVALID_CURSOR", "运行游标无效。") from exc
    rows = list(
        db.scalars(stmt.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit + 1))
    )
    next_cursor = None
    if len(rows) > limit:
        row = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([row.created_at.isoformat(), str(row.id)]).encode()
        ).decode()
    return {
        "data": [result(db, r) for r in rows[:limit]],
        "next_cursor": next_cursor,
        "request_id": request.state.request_id,
    }


@router.get("/{run_id}")
def detail(run_id: UUID, request: Request, db: Database, user: Authenticated) -> dict[str, Any]:
    return {
        "data": result(db, owned_run(db, user.id, run_id)),
        "request_id": request.state.request_id,
    }


@router.post("/{run_id}/cancel")
def cancel_run(
    run_id: UUID,
    body: CancelInput,
    request: Request,
    db: Database,
    user: Authenticated,
) -> dict[str, Any]:
    run = cancel(db, user.id, run_id, body.expected_version)
    return {"data": result(db, run), "request_id": request.state.request_id}


@router.post("/{run_id}/resume", status_code=202)
def resume_run(
    run_id: UUID,
    body: ResumeInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    run = owned_run(db, user.id, run_id)
    record = idempotency(db, user.id, f"resume:{run_id}", key, body.model_dump(mode="json"))
    if record.response_summary is not None:
        return {"data": result(db, run), "request_id": request.state.request_id}
    wait = db.scalar(
        select(WaitRequest).where(
            WaitRequest.id == body.wait_request_id, WaitRequest.run_id == run.id
        )
    )
    if wait is None:
        raise BusinessError(404, "NOT_FOUND", "等待项不存在。")
    if db.get(ResumeDecision, body.wait_request_id) is not None:
        raise BusinessError(409, "RESUME_CONFLICT", "此等待项已有决定。")
    run = resume(
        db,
        user.id,
        run_id,
        body.expected_version,
        body.wait_request_id,
        body.input_manifest_hash,
        body.response,
    )
    record.response_summary = {"run_id": str(run.id)}
    db.flush()
    return {"data": result(db, run), "request_id": request.state.request_id}


def check_cursor(run: AgentRun, after: int | None, last_id: str | None) -> int:
    parsed = None
    if last_id is not None:
        try:
            identity, seq = last_id.split(":")
            if UUID(identity) != run.id or not seq.isdecimal():
                raise ValueError("Invalid event identity")
            parsed = int(seq)
        except (ValueError, TypeError) as exc:
            raise BusinessError(400, "INVALID_EVENT_CURSOR", "事件游标无效。") from exc
    if after is not None and parsed is not None and after != parsed:
        raise BusinessError(400, "EVENT_CURSOR_CONFLICT", "事件游标不一致。")
    cursor = after if after is not None else parsed if parsed is not None else 0
    if cursor < 0 or cursor > run.event_seq:
        raise BusinessError(400, "INVALID_EVENT_CURSOR", "事件游标超出范围。")
    if cursor + 1 < run.min_event_seq:
        raise BusinessError(410, "EVENT_HISTORY_EXPIRED", "历史事件已清理，请重新读取运行状态。")
    return cursor


@router.get("/{run_id}/events")
def events(
    run_id: UUID,
    request: Request,
    db: Database,
    user: Authenticated,
    after_seq: int | None = Query(default=None),
    last_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> Any:
    run = owned_run(db, user.id, run_id)
    cursor = check_cursor(run, after_seq, last_id)
    if "text/event-stream" not in request.headers.get("accept", ""):
        rows = list(
            db.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.seq > cursor)
                .order_by(RunEvent.seq)
                .limit(limit + 1)
            )
        )
        values = [
            {
                "run_id": str(run_id),
                "seq": r.seq,
                "schema_version": r.schema_version,
                "type": r.type,
                "payload": r.payload,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows[:limit]
        ]
        return {
            "data": values,
            "next_cursor": rows[limit - 1].seq if len(rows) > limit else None,
            "request_id": request.state.request_id,
        }
    owner, session_hash = user.id, user.session.token_hash

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        # Bounded connections reconnect using durable IDs; no database held during sleeps.
        for _ in range(50):
            if await request.is_disconnected():
                return
            with Session(get_engine()) as session:
                active = session.scalar(
                    select(UserSession.token_hash).where(
                        UserSession.token_hash == session_hash,
                        UserSession.revoked_at.is_(None),
                        UserSession.expires_at > func.now(),
                    )
                )
                current = session.get(AgentRun, run_id)
                if active is None or current is None or current.owner_id != owner:
                    return
                batch = session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.seq > cursor)
                    .order_by(RunEvent.seq)
                    .limit(100)
                ).all()
                history_expired = cursor + 1 < current.min_event_seq
                terminal = current.status in TERMINAL or current.status == "waiting_user"
                frames = [
                    (r.seq, r.type, r.schema_version, r.payload, r.created_at.isoformat())
                    for r in batch
                ]
            if history_expired:
                yield 'event: history_expired\ndata: {"code":"EVENT_HISTORY_EXPIRED"}\n\n'
                return
            for seq, kind, version, payload, created_at in frames:
                cursor = seq
                data = json.dumps(
                    {
                        "run_id": str(run_id),
                        "seq": seq,
                        "type": kind,
                        "created_at": created_at,
                        "schema_version": version,
                        "payload": payload,
                    },
                    ensure_ascii=False,
                )
                yield f"id: {run_id}:{seq}\nevent: run_event\ndata: {data}\n\n"
            if terminal and len(frames) < 100:
                return
            yield ": heartbeat\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
