"""Actual product claim, PostgreSQL locks/clock/RI, and original domain controls.

Opt-in production tests. Supply a fresh disposable *_validation_* domain DB per
selected node and CAREERFORGE_TEST_GRAPH_POSTGRES_DSN, as in the existing PG
suite. No service is started here, no release script/overlay is imported, and all
model fixtures are synthetic. Never treat the SQLite clock observer as RI proof.
"""

import asyncio
import os
import time
from contextvars import ContextVar
from dataclasses import replace
from datetime import timedelta
from threading import Event, Thread

import pytest
from sqlalchemy import event, select, text
from test_dialogue_actions import api_client
from test_langgraph_postgres_m1 import checkpoint_postgres  # noqa: F401
from test_learning_graph_postgresql_m4 import domain_env
from test_runtime_postgresql import pg_runtime  # noqa: F401
from test_session_claim_m6 import FAMILIES, admitted, execution_state, paid_state

from careerforge.db import aware, clock, locked
from careerforge.errors import LeaseLost
from careerforge.models import AgentRun, Outbox, Principal
from careerforge.models_dialogue import WorkflowEvolutionRequest, WorkflowExecution, WorkflowSession
from careerforge.workflows.checkpointer import Checkpointers

pytestmark = pytest.mark.production


@pytest.fixture
async def pg_claim_env(pg_runtime, checkpoint_postgres, monkeypatch):  # noqa: F811
    _, saver_settings = checkpoint_postgres
    original = Checkpointers.__init__

    def persistent(self, settings):
        original(
            self,
            replace(
                settings,
                mode="development",
                graph_checkpoint_url=saver_settings.graph_checkpoint_url,
            ),
        )

    monkeypatch.setattr(Checkpointers, "__init__", persistent)
    env = domain_env(pg_runtime)
    app = env["app"]
    app.settings = replace(app.settings, sandbox_image="sha256:" + "1" * 64)
    app.db.settings = app.settings
    return env


class NativePair:
    """Pause first only AFTER its actual Execution lock, prove second's wait.

    Original calls retain every lock. No DB-clock patch or retry. The existing
    pg_runtime connection timeouts bound SQL; finally releases the observation
    barrier and cancels only this pair's own DB calls if they fail to finish.
    """

    def __init__(self, app, tmp_path):
        self.app, self.path = app, tmp_path / "native-claim-wait.json"
        self.role = ContextVar("claim_regression_role", default=None)
        self.locked, self.release = Event(), Event()
        self.pids, self.connections, self.sql, self.result, self.errors = (
            {},
            {},
            {"first": [], "second": []},
            {},
            {},
        )
        self.wait_evidence = None
        self.release_clock = None

    def before(self, conn, _cursor, statement, _params, _context, _many):
        role = self.role.get()
        if role:
            raw = conn.connection.driver_connection
            self.pids[role] = raw.info.backend_pid
            self.connections[role] = raw
            self.sql[role].append(statement)  # SQL shapes only, no bound parameters

    def after(self, _conn, _cursor, statement, _params, _context, _many):
        if (
            self.role.get() == "first"
            and "workflow_executions" in statement
            and "FOR UPDATE" in statement
            and not self.locked.is_set()
        ):
            self.locked.set()
            assert self.release.wait(6), "observer did not prove native second->first wait"

    def call(self, role, function):
        token = self.role.set(role)
        try:
            self.result[role] = function()
        except BaseException as exc:
            self.errors[role] = exc
        finally:
            self.role.reset(token)

    def save_evidence(self, path, value):
        import json

        with path.open("w") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())

    def run(self, first, second):

        event.listen(self.app.db.engine, "before_cursor_execute", self.before)
        event.listen(self.app.db.engine, "after_cursor_execute", self.after)
        threads = []
        try:
            a = Thread(target=self.call, args=("first", first))
            threads.append(a)
            a.start()
            assert self.locked.wait(5)
            b = Thread(target=self.call, args=("second", second))
            threads.append(b)
            b.start()
            until = time.monotonic() + 3
            with self.app.db.engine.connect() as observer:
                while time.monotonic() < until:
                    if "second" in self.pids:
                        row = observer.execute(
                            text(
                                "SELECT pid, wait_event_type, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": self.pids["second"]},
                        ).one()
                        # Refresh activity on each new observation transaction.
                        observer.commit()
                        if row[1] == "Lock" and self.pids["first"] in row[2]:
                            self.wait_evidence = list(row)
                            break
                    time.sleep(0.01)
                assert self.wait_evidence, "thread Events alone are not proof of a DB lock wait"
                self.release_clock = observer.scalar(text("SELECT clock_timestamp()"))
            self.save_evidence(
                self.path.with_suffix(".observed.json"),
                {
                    "phase": "native_wait_before_release",
                    "pids": dict(self.pids),
                    "native_wait": self.wait_evidence,
                    "release_clock": str(self.release_clock),
                    "sql": {role: list(items) for role, items in self.sql.items()},
                },
            )
            self.release.set()
        finally:
            self.release.set()
            for thread in threads:
                thread.join(timeout=12)
            for role, thread in zip(("first", "second"), threads, strict=False):
                if thread.is_alive() and role in self.connections:
                    self.connections[role].cancel()
                    thread.join(timeout=5)
            event.remove(self.app.db.engine, "before_cursor_execute", self.before)
            event.remove(self.app.db.engine, "after_cursor_execute", self.after)
            evidence = dict(
                pids=self.pids,
                native_wait=self.wait_evidence,
                release_clock=str(self.release_clock),
                sql=self.sql,
                errors={
                    k: {
                        "class": type(v).__name__,
                        "sqlstate": getattr(getattr(v, "orig", v), "sqlstate", None),
                    }
                    for k, v in self.errors.items()
                },
                alive=[t.name for t in threads if t.is_alive()],
            )
            self.save_evidence(self.path, evidence)
            assert not evidence["alive"], evidence
        assert len(set(self.pids.values())) == 2
        assert not self.errors, self.errors  # any 40P01 is fatal, never accepted/retried
        return self.result

    def updates(self, role):
        return [s for s in self.sql[role] if s.startswith("UPDATE workflow_executions SET")]


@pytest.mark.parametrize("family", FAMILIES)
async def test_pg_claim_single_winner_after_native_execution_wait(pg_claim_env, tmp_path, family):
    env, app = pg_claim_env, pg_claim_env["app"]
    service, _, eid = await admitted(env, family)
    before, paid, calls = execution_state(app, eid), paid_state(app), len(env["model"].calls)
    race = NativePair(app, tmp_path)
    results = race.run(lambda: service.claim(eid, "winner"), lambda: service.claim(eid, "loser"))
    assert results == {"first": (before["epoch"] + 1, before["wake_generation"]), "second": None}
    after = execution_state(app, eid)
    assert (after["epoch"], after["revision"], after["worker_id"], after["status"]) == (
        before["epoch"] + 1,
        before["revision"] + 1,
        "winner",
        "running",
    )
    assert len(race.updates("first")) == 1 and not race.updates("second")
    assert paid_state(app) == paid and len(env["model"].calls) == calls


async def test_pg_claim_lease_uses_database_clock_after_native_lock_wait(pg_claim_env, tmp_path):
    app = pg_claim_env["app"]
    service, _, eid = await admitted(pg_claim_env, "knowledge_v1")

    def holder():
        with app.db.transaction() as db:
            locked(db, WorkflowExecution, eid)

    race = NativePair(app, tmp_path)
    result = race.run(holder, lambda: service.claim(eid, "after-wait"))
    assert result["second"] is not None
    after = execution_state(app, eid)
    with app.db.read() as db:
        observed_after = clock(db)
    assert (
        race.release_clock <= aware(after["lease_until"]) - timedelta(seconds=60) <= observed_after
    )
    statements = race.sql["second"]
    lock_index = next(
        i for i, s in enumerate(statements) if "workflow_executions" in s and "FOR UPDATE" in s
    )
    clock_index = next(i for i, s in enumerate(statements) if "clock_timestamp()" in s)
    update_index = next(
        i for i, s in enumerate(statements) if s.startswith("UPDATE workflow_executions SET")
    )
    assert lock_index < clock_index < update_index and len(race.updates("second")) == 1


async def completed_candidate_pending_milestone(env):
    app = env["app"]
    service, wid, eid = await admitted(env, "evolution")
    await app.execute_workflow(eid, "prepare-session")
    with app.db.read() as db:
        request = db.scalar(
            select(WorkflowEvolutionRequest).where(WorkflowEvolutionRequest.execution_id == eid)
        )
        rid = request.run_id
        assert rid and request.status == "running"
    await app.execute(rid, "prepare-paid")
    with app.db.read() as db:
        run = db.get(AgentRun, rid)
        assert run.status == "succeeded" and run.budget_settled
        messages = list(
            db.scalars(
                select(Outbox).where(
                    Outbox.kind == "workflow.milestone",
                    Outbox.aggregate_id == rid,
                    Outbox.delivered.is_(False),
                )
            )
        )
        assert len(messages) == 1
        message_id = messages[0].id
    # An original legitimate domain wake, not a manually changed status/type.
    with app.db.transaction() as db:
        locked(db, Principal, env["actor"])
        identity = db.get(WorkflowSession, wid)
        conv = app.workflows.conversation(db, env["actor"], identity.conversation_id, lock=True)
        workflow = app.workflows.owned_in(db, conv, wid)
        service.wake_in(db, workflow, kind="claim-regression-ready")
    return service, wid, eid, rid, message_id


async def test_pg_claim_and_original_consume_wake_do_not_deadlock(pg_claim_env, tmp_path):
    app = pg_claim_env["app"]
    service, _, eid, rid, message_id = await completed_candidate_pending_milestone(pg_claim_env)
    before = execution_state(app, eid)
    paid = paid_state(app)
    calls = len(pg_claim_env["model"].calls)
    race = NativePair(app, tmp_path)
    results = race.run(
        lambda: service.claim(eid, "claim-worker"),
        lambda: app.continuation.consume(message_id, rid),
    )
    after = execution_state(app, eid)
    assert results["first"] == (before["epoch"] + 1, before["wake_generation"])
    assert (after["epoch"], after["wake_generation"], after["revision"]) == (
        before["epoch"] + 1,
        before["wake_generation"] + 1,
        before["revision"] + 2,
    )
    assert after["status"] == "running" and after["worker_id"] == "claim-worker"
    assert len(race.updates("first")) == 1
    assert any("conversations" in s and "FOR UPDATE" in s for s in race.sql["second"])
    assert any("principals" in s and "FOR NO KEY UPDATE" in s for s in race.sql["second"])
    # consume legitimately marks milestone delivered and adds a workflow.execute
    # message; every paid ledger (not that outbox projection) must stay unchanged.
    after_paid = paid_state(app)
    assert {k: v for k, v in paid.items() if k != Outbox.__tablename__} == {
        k: v for k, v in after_paid.items() if k != Outbox.__tablename__
    }
    assert len(pg_claim_env["model"].calls) == calls
    with app.db.read() as db:
        assert db.get(Outbox, message_id).delivered


@pytest.mark.parametrize("cancel_first", [True, False])
async def test_pg_claim_and_real_http_cancel_linearize(pg_claim_env, tmp_path, cancel_first):
    env, app = pg_claim_env, pg_claim_env["app"]
    service, wid, eid = await admitted(env, "evolution")
    before, paid, calls = execution_state(app, eid), paid_state(app), len(env["model"].calls)
    with app.db.read() as db:
        revision = db.get(WorkflowSession, wid).revision

    async def cancel():
        async with api_client(env) as api:
            response = await api.post(
                f"/workflows/{wid}/cancel",
                json={
                    "expected_revision": revision,
                    "idempotency_key": "claim-regression-real-cancel",
                },
            )
        assert response.status_code == 202
        assert response.json()["action_receipts"][0]["status"] == "applied"
        return "cancelled"

    def claim():
        return service.claim(eid, "contender")

    def real_cancel():
        return asyncio.run(cancel())

    race = NativePair(app, tmp_path)
    result = race.run(
        real_cancel if cancel_first else claim, claim if cancel_first else real_cancel
    )
    assert result["second" if cancel_first else "first"] == (
        None if cancel_first else (before["epoch"] + 1, before["wake_generation"])
    )
    after = execution_state(app, eid)
    assert after["status"] == "cancelled"
    assert service.claim(eid, "late") is None
    with pytest.raises(LeaseLost):
        service.heartbeat(eid, before["epoch"] + 1, "contender")
    with pytest.raises(LeaseLost):
        with service.transaction(eid, before["epoch"] + 1, "contender"):
            pytest.fail("cancelled claim must not reenter a domain mutation")
    cancelled_paid = paid_state(app)
    assert {k: v for k, v in paid.items() if k != Outbox.__tablename__} == {
        k: v for k, v in cancelled_paid.items() if k != Outbox.__tablename__
    }
    old_messages = {row["id"]: row for row in paid[Outbox.__tablename__]}
    current_messages = {row["id"]: row for row in cancelled_paid[Outbox.__tablename__]}
    assert set(old_messages) == set(current_messages)
    for identifier, old in old_messages.items():
        current = current_messages[identifier]
        if old["kind"] == "workflow.execute" and old["aggregate_id"] == eid:
            assert current["delivered"] is True
            assert {k: v for k, v in current.items() if k not in {"delivered", "updated_at"}} == {
                k: v for k, v in old.items() if k not in {"delivered", "updated_at"}
            }
        else:
            assert current == old
    assert len(env["model"].calls) == calls
    stable = paid_state(app)
    with app.db.transaction() as db:
        service.wake_in(db, db.get(WorkflowSession, wid), kind="late-cancelled-wake")
    assert paid_state(app) == stable


async def test_pg_explicit_expired_fixture_reaps_without_reusing_epoch(pg_claim_env):
    env, app = pg_claim_env, pg_claim_env["app"]
    service, _, eid = await admitted(env, "evolution")
    with app.db.read() as db:
        before_clock = clock(db)
    old_epoch, generation = service.claim(eid, "old-owner")
    before = execution_state(app, eid)
    with app.db.read() as db:
        after_clock = clock(db)
    assert before_clock <= aware(before["lease_until"]) - timedelta(seconds=60) <= after_clock
    paid, calls = paid_state(app), len(env["model"].calls)
    with app.db.transaction() as db:
        now = clock(db)
        assert aware(db.get(WorkflowExecution, eid).lease_until) > now
        # Explicit expired-lease negative fixture, not a product lease reduction.
        db.get(WorkflowExecution, eid).lease_until = now - timedelta(seconds=1)
    service.reap()
    reaped = execution_state(app, eid)
    assert (
        reaped["status"],
        reaped["worker_id"],
        reaped["lease_until"],
        reaped["epoch"],
        reaped["revision"],
    ) == ("queued", None, None, old_epoch, before["revision"] + 1)
    assert service.claim(eid, "new-owner") == (old_epoch + 1, generation)
    with pytest.raises(LeaseLost):
        service.heartbeat(eid, old_epoch, "old-owner")
    after = paid_state(app)
    assert {k: v for k, v in paid.items() if k != Outbox.__tablename__} == {
        k: v for k, v in after.items() if k != Outbox.__tablename__
    }
    assert len(env["model"].calls) == calls
