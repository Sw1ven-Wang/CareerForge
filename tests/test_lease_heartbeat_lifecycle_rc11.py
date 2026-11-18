"""Synthetic lifecycle/fencing tests, not model-quality or production-DB evidence.

Fast cases explicitly accelerate only heartbeat scheduling. The unchanged
65s pause / 60s lease / 10s cadence comparison is separate release evidence.
"""

import asyncio
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from support import FixtureModel, FixtureRepositories

from careerforge.application import Application
from careerforge.contracts import QueryPlan
from careerforge.db import aware, clock
from careerforge.errors import DomainError, LeaseLost
from careerforge.models import AgentRun, FundingAgreement, ModelReceipt, Principal
from careerforge.services import runtime as runtime_module


def running(env, *, key="lease-lifecycle", workflow="research", claim=True):
    app = env["app"]
    identifier = app.runtime.create(env["actor"], env["ns"], env["funding"], workflow, {}, 4, key)
    return identifier, app.runtime.claim(identifier, "worker") if claim else None, "worker"


def keepers():
    return [t for t in threading.enumerate() if t.name.startswith("careerforge-lease-")]


def assert_closed(runtime):
    assert not runtime._heartbeats._active
    assert not keepers()


def test_nested_and_concurrent_scopes_share_one_joined_keeper(env):
    rt = env["app"].runtime
    run = running(env)
    acquired, release = threading.Event(), threading.Event()
    seen = []

    def other_scope():
        with rt.maintain_lease(*run) as heartbeat:
            seen.append(heartbeat)
            acquired.set()
            assert release.wait(3)

    with rt.maintain_lease(*run) as first:
        with rt.maintain_lease(*run) as nested:
            assert nested is first and first.references == 2
        other = threading.Thread(target=other_scope)
        other.start()
        try:
            assert acquired.wait(3)
            assert seen == [first] and first.references == 2
            assert len(keepers()) == 1 and not first.thread.daemon
        finally:
            release.set()
            other.join(3)
        assert not other.is_alive() and first.references == 1
        assert first.thread.is_alive()
    assert not first.thread.is_alive()
    assert_closed(rt)


def test_different_ownership_tuples_have_distinct_scopes(env):
    rt = env["app"].runtime
    first, second = running(env), running(env, key="other")
    with rt.maintain_lease(*first) as a, rt.maintain_lease(*second) as b:
        assert a is not b and len(keepers()) == 2
    assert_closed(rt)


@pytest.mark.parametrize("via_application", [False, True])
@pytest.mark.asyncio
async def test_real_gateway_preparation_and_application_share_keeper(
    env, monkeypatch, via_application
):
    app = env["app"]
    run = running(env, workflow="knowledge_build", claim=not via_application)
    monkeypatch.setattr(app.runtime, "heartbeat_interval", 0.01)
    renewed = threading.Event()
    original_heartbeat = app.runtime.heartbeat
    original_transaction = app.db.transaction
    original_complete = env["model"].complete
    injection = False
    provider_scopes = []

    def tick(*args):
        original_heartbeat(*args)
        renewed.set()

    @contextmanager
    def transaction():
        nonlocal injection
        reserved = False
        with original_transaction() as db:
            yield db
            reserved = any(isinstance(row, ModelReceipt) for row in db.new)
        if reserved and not injection:
            injection = True
            # Synchronous wait blocks this asyncio loop, not the product keeper.
            assert renewed.wait(3)

    async def complete(request):
        active = list(app.runtime._heartbeats._active.values())
        provider_scopes.append((len(active), active[0].references))
        return await original_complete(request)

    async def root(row, epoch, worker):
        await app.gateway.call(row.id, epoch, worker, "plan", "planner", "p", {}, QueryPlan)
        app.runtime.save_stage(row.id, epoch, worker, "result", {"gaps": []})
        return True

    monkeypatch.setattr(app.runtime, "heartbeat", tick)
    monkeypatch.setattr(app.db, "transaction", transaction)
    monkeypatch.setattr(env["model"], "complete", complete)
    monkeypatch.setattr(app.build, "root", root)
    if via_application:
        await app.execute(run[0], run[2])
    else:
        await app.gateway.call(*run, "plan", "planner", "p", {}, QueryPlan)
    assert injection and provider_scopes == [(1, 2 if via_application else 1)]
    assert len(env["model"].calls) == 1
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt))
        assert receipt.status == "completed" and receipt.response is not None
        row = db.get(AgentRun, run[0])
        assert row.calls_consumed == 1 and row.epoch == 1
        assert row.status == ("succeeded" if via_application else "running")
    assert_closed(app.runtime)


@pytest.mark.parametrize(
    "fence",
    [
        "lease",
        "deadline",
        "epoch",
        "worker",
        "cancelled",
        "succeeded",
        "waiting_children",
        "funding",
        "principal",
    ],
)
def test_failed_tick_never_renews_or_retries(env, monkeypatch, fence):
    app, rt = env["app"], env["app"].runtime
    run = running(env)
    with app.db.transaction() as db:
        row = db.get(AgentRun, run[0])
        if fence in {"lease", "deadline"}:
            setattr(
                row,
                "lease_until" if fence == "lease" else "deadline_at",
                clock(db) - timedelta(seconds=1),
            )
        elif fence == "epoch":
            row.epoch += 1
        elif fence == "worker":
            row.worker_id = "replacement"
        elif fence in {"cancelled", "succeeded", "waiting_children"}:
            row.status = fence
        elif fence == "funding":
            db.get(FundingAgreement, env["funding"]).status = "revoked"
        else:
            db.get(Principal, env["actor"]).active = False
        before = row.lease_until
    original = rt.heartbeat
    ticks = []

    def record(*args):
        ticks.append(args)
        original(*args)

    monkeypatch.setattr(rt, "heartbeat_interval", 0.01)
    monkeypatch.setattr(rt, "heartbeat", record)
    with rt.maintain_lease(*run) as heartbeat:
        heartbeat.thread.join(3)
        assert not heartbeat.thread.is_alive()
        with pytest.raises(DomainError):
            heartbeat.check()
        # A nested request cannot restart a failed monitor for this owner.
        with pytest.raises(DomainError), rt.maintain_lease(*run):
            pytest.fail("failed heartbeat was restarted")
        assert len(ticks) == 1
    with app.db.read() as db:
        row = db.get(AgentRun, run[0])
        assert aware(row.lease_until) == aware(before) and row.calls_consumed == 0
    assert_closed(rt)


def test_takeover_and_parent_cancellation_fence_old_keepers(env, monkeypatch):
    app, rt = env["app"], env["app"].runtime
    run = running(env)
    child = rt.create_child(*run, "child", "analysis", {}, 2)
    child_run = child, rt.claim(child, "child-worker"), "child-worker"
    with app.db.transaction() as db:
        db.get(AgentRun, run[0]).lease_until = clock(db) - timedelta(seconds=1)
    assert rt.reap() == 1
    new_epoch = rt.claim(run[0], "replacement")
    assert new_epoch > run[1]
    with app.db.read() as db:
        revision = db.get(AgentRun, run[0]).revision
        replacement_lease = db.get(AgentRun, run[0]).lease_until
    rt.cancel(env["actor"], run[0], revision)
    monkeypatch.setattr(rt, "heartbeat_interval", 0.01)
    for ownership in [run, child_run]:
        with rt.maintain_lease(*ownership) as heartbeat:
            heartbeat.thread.join(3)
            with pytest.raises(LeaseLost):
                heartbeat.check()
    with app.db.read() as db:
        parent = db.get(AgentRun, run[0])
        assert parent.status == "cancelled" and parent.lease_until is None
        assert parent.epoch == new_epoch + 1
        assert replacement_lease is not None
        assert db.get(AgentRun, child).status == "cancelled"
    assert_closed(rt)


def test_expiration_during_authorization_is_rechecked_before_renewal(env, monkeypatch):
    app, rt = env["app"], env["app"].runtime
    run = running(env)
    with app.db.read() as db:
        original_lease = db.get(AgentRun, run[0]).lease_until
    original_leased, original_clock = runtime_module.leased, runtime_module.clock
    after_authorization = False

    def delayed_authorization(*args):
        nonlocal after_authorization
        result = original_leased(*args)
        after_authorization = True
        return result

    def controlled_clock(db):
        return (
            aware(original_lease) + timedelta(seconds=1)
            if after_authorization
            else original_clock(db)
        )

    monkeypatch.setattr(runtime_module, "leased", delayed_authorization)
    monkeypatch.setattr(runtime_module, "clock", controlled_clock)
    with pytest.raises(LeaseLost):
        rt.heartbeat(*run)
    with app.db.read() as db:
        assert db.get(AgentRun, run[0]).lease_until == original_lease
    assert_closed(rt)


def test_cancel_committed_while_heartbeat_waits_is_not_revived(env, monkeypatch):
    app, rt = env["app"], env["app"].runtime
    run = running(env)
    entered, release = threading.Event(), threading.Event()
    original = app.db.transaction

    @contextmanager
    def delayed_transaction():
        if threading.current_thread().name.startswith("careerforge-lease-"):
            entered.set()
            assert release.wait(3)
        with original() as db:
            yield db

    monkeypatch.setattr(rt, "heartbeat_interval", 0.01)
    monkeypatch.setattr(app.db, "transaction", delayed_transaction)
    with rt.maintain_lease(*run) as heartbeat:
        try:
            assert entered.wait(3)
            with app.db.read() as db:
                revision = db.get(AgentRun, run[0]).revision
            rt.cancel(env["actor"], run[0], revision)
        finally:
            release.set()
        heartbeat.thread.join(3)
        with pytest.raises(LeaseLost):
            heartbeat.check()
    with app.db.read() as db:
        row = db.get(AgentRun, run[0])
        assert row.status == "cancelled" and row.lease_until is None
    assert_closed(rt)


def test_database_write_lock_cannot_resurrect_expired_lease(env, monkeypatch):
    app, rt = env["app"], env["app"].runtime
    run = running(env)
    entered = threading.Event()
    original = rt.heartbeat

    def tick(*args):
        entered.set()
        original(*args)

    monkeypatch.setattr(rt, "heartbeat_interval", 0.01)
    monkeypatch.setattr(rt, "heartbeat", tick)
    with rt.maintain_lease(*run) as heartbeat:
        with app.db.transaction() as db:
            row = db.get(AgentRun, run[0])
            row.lease_until = clock(db) - timedelta(seconds=1)
            expired = row.lease_until
            assert entered.wait(3)  # Tick is blocked on BEGIN IMMEDIATE.
        heartbeat.thread.join(3)
        with pytest.raises(LeaseLost):
            heartbeat.check()
    with app.db.read() as db:
        assert aware(db.get(AgentRun, run[0]).lease_until) == aware(expired)
    assert_closed(rt)


@pytest.mark.asyncio
async def test_task_cancel_waits_for_blocked_tick_and_leaves_no_writer(env, monkeypatch):
    app, rt = env["app"], env["app"].runtime
    run = running(env)
    entered, release = threading.Event(), threading.Event()
    provider_entered = asyncio.Event()
    original_tick = rt.heartbeat

    def blocked_tick(*args):
        entered.set()
        assert release.wait(3)
        original_tick(*args)

    async def provider(_request):
        provider_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(rt, "heartbeat_interval", 0.01)
    monkeypatch.setattr(rt, "heartbeat", blocked_tick)
    monkeypatch.setattr(env["model"], "complete", provider)
    task = asyncio.create_task(app.gateway.call(*run, "call", "planner", "p", {}, QueryPlan))
    await provider_entered.wait()
    assert await asyncio.to_thread(entered.wait, 3)
    heartbeat = next(iter(rt._heartbeats._active.values()))
    with app.db.read() as db:
        before = db.get(AgentRun, run[0]).lease_until

    def unblock_after_stop():
        assert heartbeat._stop.wait(3)
        release.set()

    unblocker = threading.Thread(target=unblock_after_stop)
    unblocker.start()
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        unblocker.join(3)
    assert not unblocker.is_alive() and not heartbeat.thread.is_alive()
    with app.db.read() as db:
        assert db.get(AgentRun, run[0]).lease_until == before
        receipt = db.scalar(select(ModelReceipt))
        assert receipt.status == "unknown" and receipt.response is None
    assert_closed(rt)


@pytest.mark.asyncio
async def test_heartbeat_exception_is_fail_closed_and_cleanup_does_not_hide_it(env, monkeypatch):
    app, rt = env["app"], env["app"].runtime
    run = running(env, workflow="knowledge_build", claim=False)
    failed = threading.Event()

    def failed_tick(*_args):
        failed.set()
        raise RuntimeError("synthetic heartbeat failure")

    async def root(_run, _epoch, _worker):
        assert await asyncio.to_thread(failed.wait, 3)
        return True

    monkeypatch.setattr(rt, "heartbeat_interval", 0.01)
    monkeypatch.setattr(rt, "heartbeat", failed_tick)
    monkeypatch.setattr(app.build, "root", root)
    with pytest.raises(RuntimeError, match="synthetic heartbeat failure"):
        await app.execute(run[0], run[2])
    with app.db.read() as db:
        assert db.get(AgentRun, run[0]).status == "failed"
    assert_closed(rt)


@pytest.mark.asyncio
async def test_thread_start_failure_leaves_no_scope_or_reserved_call(env, monkeypatch):
    app, rt = env["app"], env["app"].runtime
    run = running(env)

    def cannot_start(_thread):
        raise RuntimeError("synthetic thread limit")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    with pytest.raises(RuntimeError, match="synthetic thread limit"):
        await app.gateway.call(*run, "call", "planner", "p", {}, QueryPlan)
    with app.db.read() as db:
        assert db.get(AgentRun, run[0]).calls_consumed == 0
        assert db.scalar(select(ModelReceipt)) is None
    assert_closed(rt)


def test_asyncio_shutdown_joins_application_and_gateway_scopes(env, monkeypatch):
    app, rt = env["app"], env["app"].runtime
    run = running(env, workflow="knowledge_build", claim=False)

    async def main():
        entered = asyncio.Event()

        async def provider(_request):
            entered.set()
            await asyncio.Event().wait()

        async def root(row, epoch, worker):
            await app.gateway.call(row.id, epoch, worker, "call", "planner", "p", {}, QueryPlan)
            return True

        monkeypatch.setattr(env["model"], "complete", provider)
        monkeypatch.setattr(app.build, "root", root)
        asyncio.create_task(app.execute(run[0], run[2]))
        await entered.wait()
        assert len(keepers()) == 1
        # asyncio.run cancels this pending task, exercising both actual finally paths.

    asyncio.run(main())
    with app.db.read() as db:
        assert db.scalar(select(ModelReceipt)).status == "unknown"
    assert_closed(rt)


def test_static_pool_serializes_sessions_and_shutdown_interrupts_lock_wait(env, monkeypatch):
    app = Application(
        replace(env["app"].settings, database_url="sqlite+pysqlite:///:memory:"),
        FixtureModel(),
        FixtureRepositories(),
    )
    owner = app.bootstrap()
    rt = app.runtime
    fund = rt.create_funding(owner["principal_id"], owner["namespace_id"], 4)
    identifier = rt.create(
        owner["principal_id"], owner["namespace_id"], fund, "research", {}, 2, "memory"
    )
    run = identifier, rt.claim(identifier, "worker"), "worker"
    entered = threading.Event()
    original_tick = rt.heartbeat

    def tick(*args):
        entered.set()
        original_tick(*args)

    monkeypatch.setattr(rt, "heartbeat_interval", 0.01)
    monkeypatch.setattr(rt, "heartbeat", tick)
    try:
        # The owning thread deliberately closes the keeper while still holding
        # StaticPool's sole connection. Shutdown must interrupt its lock wait.
        with app.db.transaction() as db:
            row = db.get(AgentRun, identifier)
            before = row.lease_until
            with rt.maintain_lease(*run) as heartbeat:
                assert entered.wait(3)
                time.sleep(0.02)
            assert not heartbeat.thread.is_alive() and row.lease_until == before
        renewed = threading.Event()

        def real_tick(*args):
            original_tick(*args)
            renewed.set()

        monkeypatch.setattr(rt, "heartbeat", real_tick)
        with rt.maintain_lease(*run):
            assert renewed.wait(3)
        with app.db.read() as db:
            assert aware(db.get(AgentRun, identifier).lease_until) > aware(before)
        assert_closed(rt)
    finally:
        app.db.engine.dispose()


@pytest.mark.parametrize("fence", ["cancel", "lease", "deadline", "takeover", "worker", "funding"])
@pytest.mark.asyncio
async def test_ownership_lost_during_last_authorization_read_never_dispatches(
    env, monkeypatch, fence
):
    app, rt = env["app"], env["app"].runtime
    run = running(env)
    original_read = app.db.read
    changed = False

    @contextmanager
    def read_then_fence():
        nonlocal changed
        with original_read() as db:
            yield db
        # The actual gateway's funding/source/privacy pre-dispatch read has
        # finished. Model the authority loss while this synchronous work was
        # blocked, after its earlier Runtime.validate had already succeeded.
        if not changed and threading.current_thread() is threading.main_thread():
            changed = True
            if fence == "cancel":
                with original_read() as db:
                    revision = db.get(AgentRun, run[0]).revision
                rt.cancel(env["actor"], run[0], revision)
            else:
                with app.db.transaction() as db:
                    row = db.get(AgentRun, run[0])
                    if fence in {"lease", "takeover", "deadline"}:
                        field = "deadline_at" if fence == "deadline" else "lease_until"
                        setattr(row, field, clock(db) - timedelta(seconds=1))
                    elif fence == "worker":
                        row.worker_id = "replacement"
                    else:
                        db.get(FundingAgreement, env["funding"]).status = "revoked"
                if fence == "takeover":
                    assert rt.reap() == 1
                    assert rt.claim(run[0], "replacement") > run[1]

    monkeypatch.setattr(app.db, "read", read_then_fence)
    with pytest.raises(DomainError):
        await app.gateway.call(*run, "call", "planner", "p", {}, QueryPlan)
    assert changed and len(env["model"].calls) == 0
    with original_read() as db:
        receipt = db.scalar(select(ModelReceipt))
        assert receipt.status == "unknown" and receipt.response is None
        assert db.get(AgentRun, run[0]).calls_consumed == 1
    assert_closed(rt)


def test_keeper_failure_does_not_replace_the_scope_body_exception(env, monkeypatch):
    rt = env["app"].runtime
    run = running(env)

    def tick(*_args):
        raise RuntimeError("synthetic secondary heartbeat error")

    monkeypatch.setattr(rt, "heartbeat_interval", 0.01)
    monkeypatch.setattr(rt, "heartbeat", tick)
    with pytest.raises(ValueError, match="original scope failure"):
        with rt.maintain_lease(*run) as heartbeat:
            heartbeat.thread.join(3)
            assert not heartbeat.thread.is_alive()
            raise ValueError("original scope failure")
    assert_closed(rt)


@pytest.mark.parametrize("fence", ["lease", "deadline", "funding", "epoch", "worker", "status"])
@pytest.mark.asyncio
async def test_final_validate_authorization_tail_rechecks_locked_authority(env, monkeypatch, fence):
    """Controlled clock/record mutation inside final leased() authorization.

    This is not the 65-second original-boundary test. Row mutations deliberately
    model an invalidated locked record; real external takeover remains serialized
    by the existing database locks and is separately tested through reap/claim.
    """
    app, rt = env["app"], env["app"].runtime
    run = running(env)
    with app.db.transaction() as db:
        row = db.get(AgentRun, run[0])
        if fence == "deadline":
            row.deadline_at = clock(db) + timedelta(seconds=5)
            expired_at = aware(row.deadline_at) + timedelta(seconds=1)
        elif fence == "funding":
            funding = db.get(FundingAgreement, env["funding"])
            funding.expires_at = clock(db) + timedelta(seconds=5)
            expired_at = aware(funding.expires_at) + timedelta(seconds=1)
        else:
            expired_at = aware(row.lease_until) + timedelta(seconds=1)
    original_validate, original_funding, original_clock = (
        rt.validate,
        runtime_module.active_funding,
        runtime_module.clock,
    )
    state = {"validations": 0, "injected": False, "advanced": False}

    def validate(*args):
        state["validations"] += 1
        return original_validate(*args)

    def funding_then_expire(db, row):
        result = original_funding(db, row)
        if state["validations"] == 2 and not state["injected"]:
            state["injected"] = True
            if fence in {"lease", "deadline", "funding"}:
                state["advanced"] = True
            elif fence == "epoch":
                row.epoch += 1
            elif fence == "worker":
                row.worker_id = "replacement"
            else:
                row.status = "cancelled"
        return result

    def controlled_clock(db):
        return expired_at if state["advanced"] else original_clock(db)

    monkeypatch.setattr(rt, "validate", validate)
    monkeypatch.setattr(runtime_module, "active_funding", funding_then_expire)
    monkeypatch.setattr(runtime_module, "clock", controlled_clock)
    with pytest.raises(DomainError):
        await app.gateway.call(*run, "call", "planner", "p", {}, QueryPlan)
    assert state["injected"] and len(env["model"].calls) == 0
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt))
        assert receipt.status == "unknown" and receipt.response is None
        assert db.get(AgentRun, run[0]).calls_consumed == 1
    assert_closed(rt)


@pytest.mark.asyncio
async def test_keeper_error_inside_final_validate_prevents_dispatch(env, monkeypatch):
    app, rt = env["app"], env["app"].runtime
    run = running(env)
    armed = threading.Event()
    original_validate, original_funding = rt.validate, runtime_module.active_funding
    validations = 0

    def validate(*args):
        nonlocal validations
        validations += 1
        return original_validate(*args)

    def failed_keeper(*_args):
        assert armed.wait(3)
        raise RuntimeError("synthetic keeper loss during final authorization")

    def funding_then_wait(db, row):
        result = original_funding(db, row)
        if validations == 2:
            armed.set()
            heartbeat = next(iter(rt._heartbeats._active.values()))
            heartbeat.thread.join(3)
            assert not heartbeat.thread.is_alive()
        return result

    monkeypatch.setattr(rt, "heartbeat_interval", 0.01)
    monkeypatch.setattr(rt, "heartbeat", failed_keeper)
    monkeypatch.setattr(rt, "validate", validate)
    monkeypatch.setattr(runtime_module, "active_funding", funding_then_wait)
    with pytest.raises(RuntimeError, match="synthetic keeper loss during final authorization"):
        await app.gateway.call(*run, "call", "planner", "p", {}, QueryPlan)
    assert armed.is_set() and len(env["model"].calls) == 0
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt))
        assert receipt.status == "unknown" and receipt.response is None
    assert_closed(rt)
