"""Real database state-machine tests; SQLite is an explicit isolated test backend."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from careerforge.contracts import ModelOutput, QueryPlan
from careerforge.db import clock
from careerforge.errors import DomainError, LeaseLost
from careerforge.models import AgentRun, FundingAgreement, ModelReceipt


def new_run(e, cap=6, key="runtime"):
    r = e["app"].runtime
    identifier = r.create(
        e["actor"], e["ns"], e["funding"], "research", {"question": "test"}, cap, key
    )
    epoch = r.claim(identifier, "worker")
    return identifier, epoch, "worker"


def test_idempotency_stages_and_lease(env):
    rt = env["app"].runtime
    r, epoch, w = new_run(env)
    assert (
        rt.create(
            env["actor"], env["ns"], env["funding"], "research", {"question": "test"}, 6, "runtime"
        )
        == r
    )
    with pytest.raises(DomainError):
        rt.create(
            env["actor"],
            env["ns"],
            env["funding"],
            "research",
            {"question": "changed"},
            6,
            "runtime",
        )
    rt.save_stage(r, epoch, w, "step", {"answer": 1})
    rt.save_stage(r, epoch, w, "step", {"answer": 1})
    with pytest.raises(DomainError):
        rt.save_stage(r, epoch, w, "step", {"answer": 2})
    with env["app"].db.read() as db:
        version = db.get(AgentRun, r).revision
    rt.cancel(env["actor"], r, version)
    with pytest.raises(LeaseLost):
        rt.save_stage(r, epoch, w, "late", {"answer": 3})
    with env["app"].db.read() as db:
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0


@pytest.mark.asyncio
async def test_exact_response_replay_no_new_billing(env):
    app = env["app"]
    run = new_run(env)
    args = (*run, "plan", "planner", "Plan", {"question": "q"}, QueryPlan)
    a = await app.gateway.call(*args, data_classes=["public_source"])
    b = await app.gateway.call(*args, data_classes=["public_source"])
    assert a == b and len(env["model"].calls) == 1
    with app.db.read() as db:
        assert db.get(AgentRun, run[0]).calls_consumed == 1
        assert len(db.scalars(select(ModelReceipt)).all()) == 1
    with pytest.raises(DomainError):
        await app.gateway.call(
            *run,
            "plan",
            "planner",
            "Changed",
            {"question": "q"},
            QueryPlan,
            data_classes=["public_source"],
        )


@pytest.mark.asyncio
async def test_unknown_request_never_retried(env):
    env["model"].overrides["planner"] = lambda r: TimeoutError("synthetic socket loss")
    app = env["app"]
    run = new_run(env)
    with pytest.raises(TimeoutError):
        await app.gateway.call(
            *run, "plan", "planner", "p", {}, QueryPlan, data_classes=["public_source"]
        )
    with pytest.raises(DomainError):
        await app.gateway.call(
            *run, "plan", "planner", "p", {}, QueryPlan, data_classes=["public_source"]
        )
    assert len(env["model"].calls) == 1
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt))
        assert receipt.status == "unknown"
        assert db.get(AgentRun, run[0]).calls_consumed == 1


@pytest.mark.asyncio
async def test_invalid_saved_response_does_not_bypass_validation(env):
    env["model"].overrides["planner"] = lambda r: ModelOutput(
        structured={"queries": [], "stop_reason": None},
        synthetic=True,
        finish_reason="length",
        usage={"completion_tokens": 99999},
    )
    app = env["app"]
    run = new_run(env)
    for _ in range(2):
        with pytest.raises(DomainError):
            await app.gateway.call(
                *run, "bad", "planner", "p", {}, QueryPlan, data_classes=["public_source"]
            )
    assert len(env["model"].calls) == 1


@pytest.mark.asyncio
async def test_parent_children_shared_budget_and_total_limit(env):
    app = env["app"]
    rt = app.runtime
    parent = new_run(env, 6)
    child = rt.create_child(*parent, "analysis", "analysis", {}, 2)
    ce = rt.claim(child, "child")
    await app.gateway.call(
        child, ce, "child", "call", "planner", "p", {}, QueryPlan, data_classes=["public_source"]
    )
    rt.finish(child, ce, "child", "succeeded")
    rt.wait_children(*parent)  # Child finished before wait: must requeue, not deadlock.
    pe = rt.claim(parent[0], "worker2")
    assert pe is not None
    second = rt.create_child(parent[0], pe, "worker2", "review", "review", {}, 2)
    with pytest.raises(DomainError):
        rt.create_child(parent[0], pe, "worker2", "third", "analysis", {}, 1)
    se = rt.claim(second, "child2")
    with pytest.raises(DomainError):
        rt.create_child(second, se, "child2", "nested", "analysis", {}, 1)
    with app.db.read() as db:
        revision = db.get(AgentRun, parent[0]).revision
    rt.cancel(env["actor"], parent[0], revision)
    with app.db.read() as db:
        p = db.get(AgentRun, parent[0])
        f = db.get(FundingAgreement, env["funding"])
        assert p.calls_consumed == 1 and p.held_calls == 0
        assert f.calls_consumed == 1 and f.calls_held == 0
        assert db.get(AgentRun, second).status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_inflight_records_cost_but_blocks_effects(env):
    app = env["app"]
    run = new_run(env)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = env["model"].complete

    async def slow(request):
        entered.set()
        await release.wait()
        return await original(request)

    env["model"].complete = slow
    task = asyncio.create_task(
        app.gateway.call(
            *run, "call", "planner", "p", {}, QueryPlan, data_classes=["public_source"]
        )
    )
    await entered.wait()
    with app.db.read() as db:
        revision = db.get(AgentRun, run[0]).revision
    app.runtime.cancel(env["actor"], run[0], revision)
    release.set()
    with pytest.raises(LeaseLost):
        await task
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt))
        assert receipt.status == "completed"
        funding = db.get(FundingAgreement, env["funding"])
        assert funding.calls_consumed == 1 and funding.calls_held == 0


def test_expired_lease_cannot_commit_after_takeover(env):
    app = env["app"]
    run = new_run(env)
    with app.db.transaction() as db:
        db.get(AgentRun, run[0]).lease_until = clock(db) - timedelta(seconds=1)
    assert app.runtime.reap() == 1
    newepoch = app.runtime.claim(run[0], "replacement")
    assert newepoch > run[1]
    with pytest.raises(LeaseLost):
        app.runtime.save_stage(*run, "late", {})
    app.runtime.save_stage(run[0], newepoch, "replacement", "actual", {})


@pytest.mark.asyncio
async def test_egress_and_roles_before_charge(env):
    app = env["app"]
    fund = app.runtime.create_funding(
        env["actor"], env["ns"], 5, roles=["planner"], data_classes=["public_source"]
    )
    r = app.runtime.create(env["actor"], env["ns"], fund, "research", {}, 3, "egress")
    e = app.runtime.claim(r, "w")
    with pytest.raises(DomainError):
        await app.gateway.call(
            r, e, "w", "x", "planner", "p", {}, QueryPlan, data_classes=["private_jd"]
        )
    with pytest.raises(DomainError):
        await app.gateway.call(
            r, e, "w", "y", "reviewer", "p", {}, QueryPlan, data_classes=["public_source"]
        )
    assert not env["model"].calls


def test_revoking_funding_cancels_waiting_family(env):
    app = env["app"]
    run = new_run(env)
    c = app.runtime.create_child(*run, "child", "analysis", {}, 2)
    app.runtime.wait_children(*run)
    app.runtime.revoke_funding(env["actor"], env["funding"])
    with app.db.read() as db:
        assert db.get(AgentRun, run[0]).status == "cancelled"
        assert db.get(AgentRun, c).status == "cancelled"
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0


@pytest.mark.asyncio
async def test_terminal_cancellation_marks_dispatched_receipt_unknown_and_settles_funding(
    env, monkeypatch
):
    app = env["app"]
    run_id, epoch, worker = new_run(env)
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.provider.complete

    async def delayed(request):
        entered.set()
        await release.wait()
        return await original(request)

    monkeypatch.setattr(app.provider, "complete", delayed)
    pending = asyncio.create_task(
        app.gateway.call(
            run_id,
            epoch,
            worker,
            "plan",
            "planner",
            "p",
            {},
            QueryPlan,
            data_classes=["public_source"],
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        with app.db.read() as db:
            revision = db.get(AgentRun, run_id).revision
            assert db.scalar(select(ModelReceipt)).status == "attempted"
        app.runtime.cancel(env["actor"], run_id, revision)
        with app.db.read() as db:
            assert db.scalar(select(ModelReceipt)).status == "unknown"
            assert db.get(AgentRun, run_id).calls_consumed == 1
            assert db.get(FundingAgreement, env["funding"]).calls_held == 0
    finally:
        release.set()
        with pytest.raises(LeaseLost):
            await pending
