"""Funded counterpart: the run already owns durable retrieval before answer plan."""

from copy import deepcopy

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_answer_completion_v166 import completion_model, submit

from careerforge.models import AgentRun, FundingAgreement, ModelReceipt, Turn


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["before_plan", "after_plan"])
async def test_funded_plan_crash_reuses_durable_selection_without_early_charges(
    env, monkeypatch, position
):
    app, actor = env["app"], env["actor"]
    await publish_fixture(env)
    completion_model(env)
    provider_calls_before = len(env["model"].calls)
    with app.db.read() as db:
        funding = db.get(FundingAgreement, env["funding"])
        funding_before = funding.calls_consumed, funding.calls_held
    turn = submit(env)
    run = (
        turn["run_id"],
        app.runtime.claim(turn["run_id"], "plan-recovery"),
        "plan-recovery",
    )
    plan_key = "tutor.plan:atomic-answer-1.6.1"
    save = app.runtime.save_stage

    def interrupted(run_id, epoch, worker, key, payload):
        if key == plan_key:
            if position == "after_plan":
                save(run_id, epoch, worker, key, payload)
            raise RuntimeError("funded answer plan persistence boundary")
        return save(run_id, epoch, worker, key, payload)

    with monkeypatch.context() as patch:
        patch.setattr(app.runtime, "save_stage", interrupted)
        with pytest.raises(RuntimeError):
            await app.conversations.process(turn["turn_id"], run)
    retrieval = deepcopy(app.runtime.stage(run[0], "retrieval"))
    assert retrieval and retrieval["retrieval_manifest_id"]
    prior_plan = deepcopy(app.runtime.stage(run[0], plan_key))
    assert bool(prior_plan) is (position == "after_plan")
    with app.db.read() as db:
        row = db.get(Turn, turn["turn_id"])
        manifest = row.manifest_id
        assert manifest == retrieval["retrieval_manifest_id"]
        receipts = {
            r.id: (r.request_hash, r.status, deepcopy(r.response))
            for r in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run[0]))
        }
        # Both answer-plan boundaries precede the first chat provider dispatch.
        # Publishing the fixture already used funding, so compare its baseline.
        assert receipts == {}
        active = db.get(AgentRun, run[0])
        funding = db.get(FundingAgreement, env["funding"])
        assert active.calls_consumed == 0 and active.call_cap == 8
        assert not active.budget_settled
        assert (funding.calls_consumed, funding.calls_held) == (
            funding_before[0],
            funding_before[1] + 8,
        )
    assert len(env["model"].calls) == provider_calls_before

    async def no_new_retrieval(*args, **kwargs):
        raise AssertionError("A persisted funded retrieval stage must be reused")

    monkeypatch.setattr(app.retrieval, "retrieve", no_new_retrieval)
    await app.conversations.process(turn["turn_id"], run)
    assert app.runtime.stage(run[0], "retrieval") == retrieval
    if prior_plan is not None:
        assert app.runtime.stage(run[0], plan_key) == prior_plan
    with app.db.read() as db:
        assert db.get(Turn, turn["turn_id"]).manifest_id == manifest
        assert db.get(Turn, turn["turn_id"]).status == "completed"
        current = list(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run[0])))
        assert len({r.stage_key for r in current}) == len(current)
        assert db.get(AgentRun, run[0]).calls_consumed == len(current)
        assert current and all(row.status == "completed" for row in current)
        completed_receipts = {
            row.id: (
                row.stage_key,
                row.request_hash,
                row.status,
                deepcopy(row.response),
            )
            for row in current
        }
        funding = db.get(FundingAgreement, env["funding"])
        completed_funding = funding.calls_consumed, funding.calls_held
        assert completed_funding == (
            funding_before[0] + len(current),
            funding_before[1] + 8 - len(current),
        )
    # Mirror Application's successful workflow finish, including settlement.
    result = app.runtime.stage(run[0], "result") or {}
    status = "partially_succeeded" if result.get("gaps") else "succeeded"
    assert app.runtime.finish(*run, status)
    with app.db.read() as db:
        active = db.get(AgentRun, run[0])
        assert active.status == status and active.budget_settled
        funding = db.get(FundingAgreement, env["funding"])
        completed_funding = funding.calls_consumed, funding.calls_held
        assert completed_funding == (funding_before[0] + len(completed_receipts), funding_before[1])
    calls = len(env["model"].calls)
    assert calls - provider_calls_before == len(completed_receipts)
    packet = app.answers.events(actor, turn["turn_id"])
    await app.execute(turn["run_id"], "completed-plan-replay")
    assert app.answers.events(actor, turn["turn_id"]) == packet
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        assert {
            row.id: (row.stage_key, row.request_hash, row.status, row.response)
            for row in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run[0]))
        } == completed_receipts
        assert db.get(AgentRun, run[0]).calls_consumed == len(completed_receipts)
        funding = db.get(FundingAgreement, env["funding"])
        assert (funding.calls_consumed, funding.calls_held) == completed_funding


@pytest.mark.asyncio
async def test_redaction_does_not_copy_arbitrary_old_event_fields(env):
    from careerforge.models import StreamEvent

    app, actor = env["app"], env["actor"]
    await publish_fixture(env)
    completion_model(env)
    turn = submit(env)
    await app.execute(turn["run_id"], "event-redaction")
    with app.db.transaction() as db:
        prior = list(db.scalars(select(StreamEvent).where(StreamEvent.turn_id == turn["turn_id"])))
        for event in prior:
            event.payload = {
                **event.payload,
                "private_message": "OLD_EVENT_SECRET_DO_NOT_COPY",
            }
        db.get(Turn, turn["turn_id"]).question = "changed question"
        before_status = db.get(Turn, turn["turn_id"]).status
        receipts = [
            (r.id, r.request_hash, r.response)
            for r in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == turn["run_id"]))
        ]
        consumed = db.get(AgentRun, turn["run_id"]).calls_consumed
    calls = len(env["model"].calls)
    packet = app.answers.events(actor, turn["turn_id"])
    assert packet["status"] == "blocked" and "OLD_EVENT_SECRET_DO_NOT_COPY" not in str(packet)
    assert all(set(e["payload"]) <= {"block_id", "code", "redacted"} for e in packet["events"])
    assert app.answers.events(actor, turn["turn_id"]) == packet
    with app.db.read() as db:
        assert db.get(Turn, turn["turn_id"]).status == before_status == "completed"
        assert db.get(AgentRun, turn["run_id"]).calls_consumed == consumed
        assert [
            (r.id, r.request_hash, r.response)
            for r in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == turn["run_id"]))
        ] == receipts
    assert len(env["model"].calls) == calls
