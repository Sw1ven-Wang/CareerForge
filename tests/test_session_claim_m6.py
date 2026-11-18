"""Shared lease ordering regression with real family admissions, synthetic only.

The SQL clock observer exercises ORM autoflush on SQLite; native locking and RI
are separately tested in test_session_claim_postgresql_m6.py.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, literal, select
from test_dialogue_actions import api_client

from careerforge.db import aware
from careerforge.errors import LeaseLost
from careerforge.models import AgentRun, FundingAgreement, ModelReceipt, Outbox
from careerforge.models_dialogue import WorkflowExecution, WorkflowSession
from careerforge.services import session_execution as module
from careerforge.services.session_execution import SessionExecutions

FAMILIES = ["knowledge_v1", "knowledge_v2", "learning", "practice", "evolution"]


async def admitted(env, family):
    """Original public/domain admissions, never change workflow_type by hand."""
    app = env["app"]
    if family.startswith("knowledge"):
        from test_session_graph_m2 import prepare

        app.settings = replace(
            app.settings,
            session_graph_version=(
                "knowledge-session-v2" if family == "knowledge_v2" else "knowledge-session-v1"
            ),
        )
        async with api_client(env) as api:
            _, wid, eid = await prepare(env, api, drain=False)
        service = app.session_workflows
    elif family == "learning":
        from test_learning_graph_admission_m4 import direct

        _, _, _, response = await direct(env)
        wid = response["workflow_id"]
        with app.db.read() as db:
            eid = db.get(WorkflowSession, wid).current_execution_id
        service = app.learning_workflows
    elif family == "practice":
        from test_practice_graph_m5 import command, session

        conv, wid = await session(env)
        with app.db.read() as db:
            row = db.get(WorkflowExecution, db.get(WorkflowSession, wid).current_execution_id)
            eid = row.id
            profile = app.learning.profile(env["actor"])
            active = profile["active_module_id"]
            intent = dict(
                type="start_practice",
                activity=dict(module_id=active, kind="understanding_check"),
                assistance_mode="assisted",
                expected_profile_revision=profile["revision"],
                module_hash=row.binding_payload["accepted"]["module_hashes"][active],
            )
        response, _ = command(env, conv, wid, intent, "claim-regression-practice")
        assert response["action_receipts"][0]["status"] == "applied"
        service = app.learning_workflows
    else:
        from test_evolution_graph_m6 import candidate_session

        _, _, _, response = await candidate_session(env)
        wid, eid = response["workflow_id"], response["execution_id"]
        service = app.evolution_workflows
    with app.db.read() as db:
        row = db.get(WorkflowExecution, eid)
        assert row.status == "queued"
        service.validate_in(db, db.get(WorkflowSession, wid), row)
    assert service.claim.__func__ is SessionExecutions.claim
    return service, wid, eid


def execution_state(app, identifier):
    with app.db.read() as db:
        row = db.get(WorkflowExecution, identifier)
        return {
            column.name: getattr(row, column.name) for column in WorkflowExecution.__table__.columns
        }


def paid_state(app):
    with app.db.read() as db:
        return {
            cls.__tablename__: [
                {c.name: getattr(row, c.name) for c in cls.__table__.columns}
                for row in db.scalars(select(cls).order_by(cls.id))
            ]
            for cls in (AgentRun, FundingAgreement, ModelReceipt, Outbox)
        }


@pytest.mark.parametrize("family", FAMILIES)
async def test_claim_clock_after_lock_before_dirty_and_exact_transition(env, monkeypatch, family):
    app = env["app"]
    service, _, eid = await admitted(env, family)
    before, paid, calls = execution_state(app, eid), paid_state(app), len(env["model"].calls)
    sampled = datetime(2040, 1, 2, tzinfo=timezone.utc)
    original_locked = module.locked
    locked_rows, statements = [], []

    def observed_locked(db, cls, identifier):
        row = original_locked(db, cls, identifier)
        assert cls is WorkflowExecution and identifier == eid
        locked_rows.append(row)
        return row

    def query_clock(db):
        assert len(locked_rows) == 1, "DB clock must be sampled after the original row lock returns"
        row = locked_rows[0]
        assert row not in db.dirty and not db.is_modified(row), (
            "claim dirtied row before DB clock SELECT"
        )
        assert (row.status, row.epoch, row.revision, row.worker_id) == (
            before["status"],
            before["epoch"],
            before["revision"],
            before["worker_id"],
        )
        # A real ORM query, with autoflush enabled, not a constant-returning stub.
        return aware(db.scalar(select(literal(sampled))))

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    monkeypatch.setattr(module, "locked", observed_locked)
    monkeypatch.setattr(module, "clock", query_clock)
    event.listen(app.db.engine, "before_cursor_execute", record)
    try:
        result = service.claim(eid, "claim-owner")
    finally:
        event.remove(app.db.engine, "before_cursor_execute", record)
    after = execution_state(app, eid)
    assert result == (before["epoch"] + 1, before["wake_generation"])
    assert after["status"] == "running" and after["worker_id"] == "claim-owner"
    assert after["epoch"] == before["epoch"] + 1 and after["revision"] == before["revision"] + 1
    assert aware(after["lease_until"]) == sampled + timedelta(seconds=60)
    mutable = {"status", "epoch", "revision", "worker_id", "lease_until", "updated_at"}
    assert {k: v for k, v in after.items() if k not in mutable} == {
        k: v for k, v in before.items() if k not in mutable
    }
    updates = [s for s in statements if s.startswith("UPDATE workflow_executions SET")]
    assert len(updates) == 1, updates
    assert paid_state(app) == paid and len(env["model"].calls) == calls


def lease_fixture(env, status="queued"):
    """Primitive-only synthetic row; not claimed as a valid business admission."""
    from test_session_graph_schema import execution, session

    wid, conv = session(env, "langgraph")
    with env["app"].db.transaction() as db:
        row = execution(
            env,
            wid,
            conv,
            status=status,
            epoch=3,
            revision=4,
            wake_generation=5,
            handled_generation=2,
        )
        db.add(row)
        db.flush()
        return row.id


@pytest.mark.parametrize(
    "status",
    ["missing", "running", "waiting", "blocked", "paused", "completed", "failed", "cancelled"],
)
def test_missing_or_nonqueued_never_samples_clock_or_mutates(env, monkeypatch, status):
    app = env["app"]
    eid = "not-present" if status == "missing" else lease_fixture(env, status)
    before = None if status == "missing" else execution_state(app, eid)
    paid = paid_state(app)

    def forbidden(_):
        pytest.fail("nonqueued/missing claim must not sample clock")

    monkeypatch.setattr(module, "clock", forbidden)
    assert app.session_workflows.claim(eid, "not-owner") is None
    assert paid_state(app) == paid
    if before:
        assert execution_state(app, eid) == before


def test_clock_failure_rolls_back_and_duplicate_claim_is_inert(env, monkeypatch):
    app = env["app"]
    eid = lease_fixture(env)
    before = execution_state(app, eid)
    original_clock = module.clock

    def unavailable(_):
        raise RuntimeError("synthetic unavailable DB clock")

    monkeypatch.setattr(module, "clock", unavailable)
    with pytest.raises(RuntimeError, match="DB clock"):
        app.session_workflows.claim(eid, "failed-owner")
    assert execution_state(app, eid) == before
    monkeypatch.setattr(module, "clock", original_clock)
    claimed = app.session_workflows.claim(eid, "winner")
    after = execution_state(app, eid)
    monkeypatch.setattr(module, "clock", unavailable)
    assert app.session_workflows.claim(eid, "loser") is None
    assert claimed == (before["epoch"] + 1, before["wake_generation"])
    assert execution_state(app, eid) == after


def test_wrong_worker_and_stale_epoch_cannot_heartbeat(env):
    app = env["app"]
    eid = lease_fixture(env)
    epoch, _ = app.session_workflows.claim(eid, "winner")
    before, paid = execution_state(app, eid), paid_state(app)
    for candidate_epoch, worker in ((epoch - 1, "winner"), (epoch, "loser")):
        with pytest.raises(LeaseLost):
            app.session_workflows.heartbeat(eid, candidate_epoch, worker)
        assert execution_state(app, eid) == before
    app.session_workflows.heartbeat(eid, epoch, "winner")
    after = execution_state(app, eid)
    assert aware(after["lease_until"]) >= aware(before["lease_until"])
    assert paid_state(app) == paid
