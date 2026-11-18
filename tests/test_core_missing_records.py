"""Missing authoritative records fail closed instead of becoming None dereferences."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from sqlalchemy.orm import Session

from careerforge.contracts import QueryPlan
from careerforge.db import clock
from careerforge.errors import DomainError, LeaseLost
from careerforge.models import (
    AgentRun,
    FundingAgreement,
    Idempotency,
    ModelReceipt,
    Namespace,
    Outbox,
)


@pytest.mark.parametrize("timestamp", [None, "not-a-database-timestamp"])
def test_missing_postgresql_clock_never_falls_back_to_host_time(timestamp):
    session = Mock(spec=Session)
    session.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    session.scalar.return_value = timestamp
    with pytest.raises(DomainError) as error:
        clock(session)
    assert error.value.code == "DATABASE_CLOCK"


def test_valid_postgresql_clock_is_returned_unchanged():
    timestamp = datetime(2026, 11, 2, tzinfo=UTC)
    session = Mock(spec=Session)
    session.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    session.scalar.return_value = timestamp
    assert clock(session) is timestamp


def test_dangling_idempotency_result_cannot_reserve_again(env):
    app = env["app"]
    args = (env["actor"], env["ns"], env["funding"], "research", {}, 2, "dangling-result")
    identifier = app.runtime.create(*args)
    with app.db.transaction() as db:
        record = db.scalar(select(Idempotency).where(Idempotency.key == "dangling-result"))
        record.result = {"run_id": "missing-run"}
    with pytest.raises(DomainError) as error:
        app.runtime.create(*args)
    assert error.value.code == "IDEMPOTENCY_RESULT_MISSING"
    with app.db.read() as db:
        assert db.get(FundingAgreement, env["funding"]).calls_held == 2
        assert [run.id for run in db.scalars(select(AgentRun))] == [identifier]


def test_missing_parent_fences_child_before_any_model_charge(env, monkeypatch):
    app = env["app"]
    identifier = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "research", {}, 4, "missing-parent"
    )
    epoch = app.runtime.claim(identifier, "parent")
    child = app.runtime.create_child(identifier, epoch, "parent", "child", "analysis", {}, 2)
    child_epoch = app.runtime.claim(child, "child")
    original_get = Session.get

    def missing_parent(session, model, key, *args, **kwargs):
        if model is AgentRun and key == identifier:
            return None
        return original_get(session, model, key, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "get", missing_parent)
        with pytest.raises(LeaseLost):
            app.runtime.validate(child, child_epoch, "child")
    with app.db.read() as db:
        assert db.get(FundingAgreement, env["funding"]).calls_consumed == 0
        assert db.get(AgentRun, identifier).held_calls == 2


def test_missing_child_replay_does_not_allocate_a_replacement(env, monkeypatch):
    app = env["app"]
    identifier = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "research", {}, 4, "missing-child"
    )
    epoch = app.runtime.claim(identifier, "parent")
    child = app.runtime.create_child(identifier, epoch, "parent", "child", "analysis", {}, 2)
    original_get = Session.get

    def missing_child(session, model, key, *args, **kwargs):
        if model is AgentRun and key == child:
            return None
        return original_get(session, model, key, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "get", missing_child)
        with pytest.raises(DomainError) as error:
            app.runtime.create_child(identifier, epoch, "parent", "child", "analysis", {}, 2)
    assert error.value.code == "DELEGATION_STATE"
    with app.db.read() as db:
        assert db.get(AgentRun, identifier).held_calls == 2
        assert len(db.scalars(select(AgentRun)).all()) == 2


@pytest.mark.parametrize("provider_failure", [False, True])
@pytest.mark.asyncio
async def test_disappearing_receipt_does_not_mask_error_or_accept_unrecorded_result(
    env, provider_failure
):
    app = env["app"]
    identifier = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "research", {}, 2, "missing-receipt"
    )
    epoch = app.runtime.claim(identifier, "worker")
    original = app.provider.complete
    calls = 0

    async def receipt_disappears(request):
        nonlocal calls
        calls += 1
        with app.db.transaction() as db:
            receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == identifier))
            db.delete(receipt)
        if provider_failure:
            raise TimeoutError("synthetic provider timeout")
        return await original(request)

    app.provider.complete = receipt_disappears
    if provider_failure:
        with pytest.raises(TimeoutError, match="synthetic provider timeout"):
            await app.gateway.call(
                identifier, epoch, "worker", "request", "planner", "p", {}, QueryPlan
            )
    else:
        with pytest.raises(DomainError) as error:
            await app.gateway.call(
                identifier, epoch, "worker", "request", "planner", "p", {}, QueryPlan
            )
        assert error.value.code == "MODEL_RECEIPT_MISSING"
    assert calls == 1
    with app.db.read() as db:
        assert db.get(FundingAgreement, env["funding"]).calls_consumed == 1
        assert db.get(AgentRun, identifier).calls_consumed == 1


@pytest.mark.asyncio
async def test_claimed_but_missing_run_does_not_start_provider(env, monkeypatch):
    app = env["app"]
    monkeypatch.setattr(app.runtime, "claim", lambda *_args: 1)
    with pytest.raises(DomainError) as error:
        await app.execute("missing-run", "worker")
    assert error.value.code == "RUN_STATE"
    assert not env["model"].calls


@pytest.mark.asyncio
async def test_missing_publication_namespace_leaves_projection_pending(env, monkeypatch):
    app = env["app"]
    _, publication, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        message = Outbox(kind="index", aggregate_id=publication, payload={})
        db.add(message)
        db.flush()
        message_id = message.id
    original_get = Session.get
    indexed = []

    def missing_namespace(session, model, key, *args, **kwargs):
        if model is Namespace and key == env["ns"]:
            return None
        return original_get(session, model, key, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "get", missing_namespace)
        patch.setattr(app.retrieval, "index_lexical", lambda *_args: indexed.append(True))
        with pytest.raises(DomainError) as error:
            app.process_projections()
    assert error.value.code == "PROJECTION_SCOPE"
    assert not indexed
    with app.db.read() as db:
        assert not db.get(Outbox, message_id).delivered


@pytest.mark.asyncio
async def test_projection_message_removed_before_ack_is_safe(env, monkeypatch):
    app = env["app"]
    _, publication, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        message = Outbox(kind="index", aggregate_id=publication, payload={})
        db.add(message)
        db.flush()
        message_id = message.id

    def projected_then_removed(*_args):
        with app.db.transaction() as db:
            db.delete(db.get(Outbox, message_id))

    monkeypatch.setattr(app.retrieval, "index_lexical", projected_then_removed)
    assert app.process_projections() >= 1
    with app.db.read() as db:
        assert db.get(Outbox, message_id) is None
