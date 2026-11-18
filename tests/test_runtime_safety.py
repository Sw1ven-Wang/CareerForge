"""Regressions for authorization, fencing and immutable run contracts.

All providers are explicit fixtures. SQLite coverage does not claim PostgreSQL
cross-process locking validation; lock ordering is additionally checked directly.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from careerforge.contracts import QueryPlan
from careerforge.db import clock
from careerforge.errors import DomainError
from careerforge.models import AgentRun, FundingAgreement, Principal


def running(env, cap=4, key="safety"):
    runtime = env["app"].runtime
    identifier = runtime.create(env["actor"], env["ns"], env["funding"], "research", {}, cap, key)
    return identifier, runtime.claim(identifier, "worker"), "worker"


@pytest.mark.parametrize("scope", ["roles", "data_classes"])
@pytest.mark.asyncio
async def test_explicit_empty_funding_scope_never_becomes_full_authorization(env, scope):
    app = env["app"]
    funding = app.runtime.create_funding(env["actor"], env["ns"], 4, **{scope: []})
    identifier = app.runtime.create(env["actor"], env["ns"], funding, "research", {}, 1, "deny-all")
    epoch = app.runtime.claim(identifier, "worker")
    with pytest.raises(DomainError, match="未授权"):
        await app.gateway.call(
            identifier,
            epoch,
            "worker",
            "plan",
            "planner",
            "p",
            {},
            QueryPlan,
            data_classes=["public_source"],
        )
    with app.db.read() as db:
        row = db.get(FundingAgreement, funding)
        assert getattr(row, "allowed_roles" if scope == "roles" else "allowed_data_classes") == []
        assert row.calls_consumed == 0
    assert not env["model"].calls


def test_unknown_funding_data_class_is_rejected(env):
    with pytest.raises(DomainError) as error:
        env["app"].runtime.create_funding(
            env["actor"], env["ns"], 2, data_classes=["typo_private_source"]
        )
    assert error.value.code == "DATA_CLASS"


def test_public_funding_does_not_expand_explicit_empty_egress(env):
    app = env["app"]
    identifier = app.runtime.create_funding(
        env["actor"], env["owner"]["public_namespace_id"], 2, data_classes=[]
    )
    with app.db.read() as db:
        assert db.get(FundingAgreement, identifier).allowed_data_classes == []


@pytest.mark.parametrize("field", ["lease_until", "deadline_at"])
def test_expired_worker_cannot_finish_before_reaper_or_takeover(env, field):
    app = env["app"]
    run = running(env)
    with app.db.transaction() as db:
        setattr(db.get(AgentRun, run[0]), field, clock(db) - timedelta(seconds=1))
    app.runtime.finish(*run, "succeeded")
    with app.db.read() as db:
        row = db.get(AgentRun, run[0])
        assert row.status == "running"
        assert not row.budget_settled
        assert db.get(FundingAgreement, env["funding"]).calls_held == 4


@pytest.mark.parametrize("restriction", ["funding", "principal"])
def test_authorization_loss_cannot_be_recorded_as_success(env, restriction):
    app = env["app"]
    run = running(env)
    with app.db.transaction() as db:
        if restriction == "funding":
            db.get(FundingAgreement, env["funding"]).status = "revoked"
        else:
            db.get(Principal, env["actor"]).active = False
    app.runtime.finish(*run, "succeeded")
    with app.db.read() as db:
        assert db.get(AgentRun, run[0]).status != "succeeded"


def test_child_replay_includes_workflow(env):
    rt = env["app"].runtime
    run = running(env)
    rt.create_child(*run, "child", "analysis", {}, 2)
    with pytest.raises(DomainError) as error:
        rt.create_child(*run, "child", "review", {}, 2)
    assert error.value.code == "DELEGATION_CONFLICT"


def test_run_replay_includes_deadline_contract(env):
    rt = env["app"].runtime
    args = (env["actor"], env["ns"], env["funding"], "research", {}, 2, "deadline")
    rt.create(*args, minutes=10)
    with pytest.raises(DomainError) as error:
        rt.create(*args, minutes=20)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("minutes", [0, -1])
def test_nonpositive_run_deadline_rejected_without_reservation(env, minutes):
    app = env["app"]
    with pytest.raises(DomainError) as error:
        app.runtime.create(
            env["actor"],
            env["ns"],
            env["funding"],
            "research",
            {},
            2,
            "bad-deadline",
            minutes=minutes,
        )
    assert error.value.code == "RUN_CONTRACT"
    with app.db.read() as db:
        assert not db.scalars(select(AgentRun)).all()
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0


def test_wait_without_children_is_rejected(env):
    rt = env["app"].runtime
    run = running(env)
    with pytest.raises(DomainError) as error:
        rt.wait_children(*run)
    assert error.value.code == "DELEGATION_SCOPE"
    with env["app"].db.read() as db:
        assert db.get(AgentRun, run[0]).status == "running"


def test_claim_and_recovery_advance_optimistic_revision(env):
    app = env["app"]
    identifier = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "research", {}, 2, "revision"
    )
    with app.db.read() as db:
        before = db.get(AgentRun, identifier).revision
    app.runtime.claim(identifier, "worker")
    with app.db.transaction() as db:
        row = db.get(AgentRun, identifier)
        claimed = row.revision
        assert claimed > before
        row.lease_until = clock(db) - timedelta(seconds=1)
    app.runtime.reap()
    with app.db.read() as db:
        assert db.get(AgentRun, identifier).revision > claimed


def test_lock_run_takes_funding_before_parent_and_child(env, monkeypatch):
    from careerforge.services import runtime as module

    rt = env["app"].runtime
    run = running(env)
    child = rt.create_child(*run, "analysis", "analysis", {}, 2)
    seen = []
    original = module.locked

    def record(db, cls, identifier):
        seen.append((cls, identifier))
        return original(db, cls, identifier)

    monkeypatch.setattr(module, "locked", record)
    with rt.db.transaction() as db:
        module.lock_run(db, child)
    assert seen[:3] == [(FundingAgreement, env["funding"]), (AgentRun, run[0]), (AgentRun, child)]


@pytest.mark.asyncio
async def test_worker_heartbeats_during_non_model_async_work(env, monkeypatch):
    import asyncio

    app = env["app"]
    identifier = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "knowledge_build", {}, 2, "whole-worker-heartbeat"
    )
    monkeypatch.setattr(app.runtime, "heartbeat_interval", 0.005)
    renewed = asyncio.Event()
    original = app.runtime.heartbeat

    def record_heartbeat(*args):
        original(*args)
        renewed.set()

    async def non_model_work(run, epoch, worker):
        with app.db.transaction() as db:
            db.get(AgentRun, run.id).lease_until = clock(db) + timedelta(seconds=1)
        # Synchronize on an actual renewal instead of making a timing assertion.
        await asyncio.wait_for(renewed.wait(), timeout=2)
        app.runtime.save_stage(run.id, epoch, worker, "result", {"gaps": []})
        return True

    monkeypatch.setattr(app.runtime, "heartbeat", record_heartbeat)
    monkeypatch.setattr(app.build, "root", non_model_work)
    await app.execute(identifier, "worker")
    with app.db.read() as db:
        assert db.get(AgentRun, identifier).status == "succeeded"
    assert not env["model"].calls


def test_claim_after_authorization_loss_settles_instead_of_stalling(env):
    app = env["app"]
    identifier = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "research", {}, 2, "claim-revoked"
    )
    with app.db.transaction() as db:
        db.get(FundingAgreement, env["funding"]).status = "revoked"
    assert app.runtime.claim(identifier, "worker") is None
    with app.db.read() as db:
        assert db.get(AgentRun, identifier).status == "failed"
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0
