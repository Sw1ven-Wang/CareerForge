"""Opt-in tests against a disposable, real PostgreSQL/pgvector database.

Set CAREERFORGE_TEST_POSTGRES_URL, or CAREERFORGE_TEST_POSTGRES=1 plus
CAREERFORGE_DATABASE_URL. No provider network requests occur. Database names must
contain `_validation_`; this suite never drops existing tables or databases.
"""

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import make_url
from support import FixtureModel, FixtureRepositories

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.contracts import QueryPlan
from careerforge.errors import DomainError, LeaseLost
from careerforge.models import AgentRun, FundingAgreement, Idempotency, ModelReceipt, Outbox
from careerforge.services.security import create_principal

pytestmark = pytest.mark.production


@pytest.fixture
def pg_runtime(tmp_path):
    url = os.getenv("CAREERFORGE_TEST_POSTGRES_URL")
    if not url and os.getenv("CAREERFORGE_TEST_POSTGRES") == "1":
        url = os.getenv("CAREERFORGE_DATABASE_URL")
    if not url:
        pytest.skip("requires explicit disposable PostgreSQL test database")
    parsed = make_url(url)
    assert parsed.get_backend_name() == "postgresql"
    assert "_validation_" in (parsed.database or ""), "refusing non-validation database"
    settings = Settings(mode="test", database_url=url, embedding_dimensions=8, state_dir=tmp_path)
    app = Application(settings, FixtureModel(), FixtureRepositories())

    @event.listens_for(app.db.engine, "connect")
    def bounded_locks(connection, _record):
        # Session settings must survive the pool's transaction rollbacks.
        previous = connection.autocommit
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '10s'")
        finally:
            connection.autocommit = previous

    app.db.create_schema()
    with app.db.transaction() as db:
        owner, private, _token = create_principal(db, "pg-runtime-" + uuid4().hex)
        actor, namespace = owner.id, private.id
    funding = app.runtime.create_funding(actor, namespace, 100)
    yield app, actor, namespace, funding
    app.db.engine.dispose()


def test_concurrent_identical_run_creation_is_one_reservation(pg_runtime):
    app, actor, namespace, funding = pg_runtime
    barrier = threading.Barrier(6)

    def create(_):
        barrier.wait(timeout=5)
        return app.runtime.create(actor, namespace, funding, "research", {}, 4, "same-key")

    with ThreadPoolExecutor(max_workers=6) as pool:
        identifiers = list(pool.map(create, range(6)))
    assert len(set(identifiers)) == 1
    with app.db.read() as db:
        assert db.get(FundingAgreement, funding).calls_held == 4
        assert (
            db.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.principal_id == actor)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(Idempotency)
                .where(Idempotency.principal_id == actor)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(Outbox)
                .where(Outbox.aggregate_id == identifiers[0])
            )
            == 1
        )


def test_concurrent_same_key_different_funding_conflicts_without_orphan(pg_runtime):
    app, actor, namespace, first = pg_runtime
    second = app.runtime.create_funding(actor, namespace, 100)
    barrier = threading.Barrier(2)

    def create(funding):
        barrier.wait(timeout=5)
        try:
            return "created", app.runtime.create(
                actor, namespace, funding, "research", {}, 4, "conflicting-key"
            )
        except DomainError as exc:
            return "rejected", exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, [first, second]))
    assert sorted(kind for kind, _ in results) == ["created", "rejected"]
    assert ("rejected", "IDEMPOTENCY_CONFLICT") in results
    with app.db.read() as db:
        assert (
            sum(db.get(FundingAgreement, identifier).calls_held for identifier in [first, second])
            == 4
        )
        assert (
            db.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.principal_id == actor)
            )
            == 1
        )


def test_funding_revocation_and_worker_validation_have_no_lock_inversion(pg_runtime, monkeypatch):
    from careerforge.services import runtime as module

    app, actor, namespace, funding = pg_runtime
    identifier = app.runtime.create(actor, namespace, funding, "research", {}, 4, "locking")
    epoch = app.runtime.claim(identifier, "worker")
    revoke_holds_funding = threading.Event()
    validate_waits_for_funding = threading.Event()
    original = module.locked

    def instrument(db, cls, row_id):
        name = threading.current_thread().name
        if name == "validate" and cls is FundingAgreement:
            # In the old run->funding order, this thread already holds the run;
            # in the corrected order, it has not locked the run yet.
            validate_waits_for_funding.set()
        row = original(db, cls, row_id)
        if name == "revoke" and cls is FundingAgreement:
            revoke_holds_funding.set()
            assert validate_waits_for_funding.wait(5)
        return row

    monkeypatch.setattr(module, "locked", instrument)

    def revoke():
        threading.current_thread().name = "revoke"
        app.runtime.revoke_funding(actor, funding)

    def validate():
        threading.current_thread().name = "validate"
        assert revoke_holds_funding.wait(5)
        with pytest.raises(LeaseLost):
            app.runtime.validate(identifier, epoch, "worker")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(revoke), pool.submit(validate)]
        for future in futures:
            future.result(timeout=12)
    with app.db.read() as db:
        assert db.get(AgentRun, identifier).status == "cancelled"
        assert db.get(FundingAgreement, funding).calls_held == 0


def test_parallel_children_meter_and_settle_one_shared_budget(pg_runtime):
    app, actor, namespace, funding = pg_runtime
    parent = app.runtime.create(actor, namespace, funding, "knowledge_build", {}, 4, "family")
    epoch = app.runtime.claim(parent, "parent")
    children = [
        app.runtime.create_child(parent, epoch, "parent", role, role, {}, 2)
        for role in ["analysis", "review"]
    ]
    app.runtime.wait_children(parent, epoch, "parent")
    barrier = threading.Barrier(2)

    def work(child):
        worker = "worker-" + child
        child_epoch = app.runtime.claim(child, worker)
        barrier.wait(timeout=5)
        asyncio.run(
            app.gateway.call(
                child,
                child_epoch,
                worker,
                "paid",
                "planner",
                "p",
                {},
                QueryPlan,
                data_classes=["public_source"],
            )
        )
        app.runtime.finish(child, child_epoch, worker, "succeeded")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(work, children))
    new_epoch = app.runtime.claim(parent, "parent-2")
    assert new_epoch is not None
    app.runtime.finish(parent, new_epoch, "parent-2", "succeeded")
    with app.db.read() as db:
        row = db.get(AgentRun, parent)
        agreement = db.get(FundingAgreement, funding)
        assert (row.calls_consumed, row.held_calls, row.budget_settled) == (2, 0, True)
        assert (agreement.calls_consumed, agreement.calls_held) == (2, 0)
        receipts = db.scalars(select(ModelReceipt).where(ModelReceipt.run_id.in_(children))).all()
        assert len(receipts) == 2 and all(r.status == "completed" for r in receipts)
        assert all(db.get(AgentRun, child).status == "succeeded" for child in children)


@pytest.mark.asyncio
async def test_late_complete_receipt_can_replay_after_takeover_without_rebilling(pg_runtime):
    from datetime import timedelta

    from careerforge.db import clock

    app, actor, namespace, funding = pg_runtime
    identifier = app.runtime.create(
        actor, namespace, funding, "research", {}, 3, "receipt-recovery"
    )
    epoch = app.runtime.claim(identifier, "old-worker")
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.provider.complete

    async def delayed(request):
        entered.set()
        await release.wait()
        return await original(request)

    app.provider.complete = delayed
    call = asyncio.create_task(
        app.gateway.call(
            identifier,
            epoch,
            "old-worker",
            "request",
            "planner",
            "p",
            {},
            QueryPlan,
            data_classes=["public_source"],
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        with app.db.transaction() as db:
            db.get(AgentRun, identifier).lease_until = clock(db) - timedelta(seconds=1)
        assert app.runtime.reap() >= 1
        replacement = app.runtime.claim(identifier, "replacement")
        with pytest.raises(DomainError) as uncertain:
            await app.gateway.call(
                identifier,
                replacement,
                "replacement",
                "request",
                "planner",
                "p",
                {},
                QueryPlan,
                data_classes=["public_source"],
            )
        assert uncertain.value.code == "MODEL_RESULT_UNCERTAIN"
        release.set()
        with pytest.raises(LeaseLost):
            await call
        result = await app.gateway.call(
            identifier,
            replacement,
            "replacement",
            "request",
            "planner",
            "p",
            {},
            QueryPlan,
            data_classes=["public_source"],
        )
        assert result.structured["queries"]
        assert len(app.provider.calls) == 1
        with app.db.read() as db:
            assert db.get(AgentRun, identifier).calls_consumed == 1
            assert db.get(FundingAgreement, funding).calls_consumed == 1
            receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == identifier))
            assert receipt.status == "completed" and receipt.epoch == epoch
    finally:
        release.set()
        if not call.done():
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
