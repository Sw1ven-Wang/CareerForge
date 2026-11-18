"""Chat stream and task terminal state must commit atomically and exactly once."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from careerforge.contracts import TurnInput
from careerforge.db import clock
from careerforge.models import AgentRun, FundingAgreement, StreamEvent, Turn


def submit_chat(env):
    app = env["app"]
    conversation = app.conversations.create(env["actor"])
    result = app.conversations.submit(
        env["actor"],
        conversation,
        TurnInput(
            question="什么是 Tool Calling",
            expected_revision=1,
            funding_id=env["funding"],
            idempotency_key="terminal-chat",
        ),
    )
    return conversation, result["turn_id"], result["run_id"]


@pytest.mark.parametrize(
    "cause",
    [
        "deadline_before_claim",
        "funding_expired_before_claim",
        "reap_deadline",
        "reap_funding",
        "revoke_queued",
        "revoke_running",
        "cancel_queued",
        "cancel_running",
        "worker_failed",
        "worker_budget_stopped",
    ],
)
@pytest.mark.asyncio
async def test_unsuccessful_chat_terminal_unblocks_stream_and_next_turn(env, cause):
    app = env["app"]
    conversation, turn_id, run_id = submit_chat(env)
    claimed = cause in {
        "reap_deadline",
        "reap_funding",
        "revoke_running",
        "cancel_running",
        "worker_failed",
        "worker_budget_stopped",
    }
    epoch = app.runtime.claim(run_id, "worker") if claimed else None
    if claimed:
        with app.db.transaction() as db:
            db.get(Turn, turn_id).status = "running"
    if cause in {"deadline_before_claim", "reap_deadline"}:
        with app.db.transaction() as db:
            db.get(AgentRun, run_id).deadline_at = clock(db) - timedelta(seconds=1)
    if cause == "funding_expired_before_claim":
        with app.db.transaction() as db:
            db.get(FundingAgreement, env["funding"]).expires_at = clock(db) - timedelta(seconds=1)
    if cause == "reap_funding":
        with app.db.transaction() as db:
            db.get(FundingAgreement, env["funding"]).status = "revoked"
    if cause.endswith("before_claim"):
        await app.execute(run_id, "worker")
    elif cause.startswith("reap_"):
        app.runtime.reap()
    elif cause.startswith("revoke_"):
        app.runtime.revoke_funding(env["actor"], env["funding"])
    elif cause.startswith("cancel_"):
        with app.db.read() as db:
            revision = db.get(AgentRun, run_id).revision
        app.runtime.cancel(env["actor"], run_id, revision)
    else:
        app.runtime.finish(
            run_id,
            epoch,
            "worker",
            "budget_stopped" if cause.endswith("budget_stopped") else "failed",
            "TEST_FAILURE",
        )

    packet = app.answers.events(env["actor"], turn_id)
    assert packet["status"] == "interrupted"
    assert sum(event["type"] == "answer.interrupted" for event in packet["events"]) == 1
    if cause.startswith("cancel_"):
        assert (
            next(event for event in packet["events"] if event["type"] == "answer.interrupted")[
                "payload"
            ]["code"]
            == "USER_CANCELLED"
        )
    # HTTP cancellation and worker exception handlers may retry this callback.
    app.conversations.mark_failed(turn_id, "LATE_DUPLICATE")
    app.conversations.mark_failed(turn_id, "LATE_DUPLICATE")
    app.runtime.reap()
    with app.db.read() as db:
        assert (
            len(
                db.scalars(
                    select(StreamEvent).where(
                        StreamEvent.turn_id == turn_id,
                        StreamEvent.event_type == "answer.interrupted",
                    )
                ).all()
            )
            == 1
        )
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0
    revision = app.conversations.detail(env["actor"], conversation)["revision"]
    second = app.conversations.submit(
        env["actor"],
        conversation,
        TurnInput(
            question="继续学习",
            expected_revision=revision,
            idempotency_key="next-turn",
        ),
    )
    assert second["status"] == "queued"


def test_completed_turn_is_not_rewritten_when_run_is_cancelled(env):
    app = env["app"]
    _, turn_id, run_id = submit_chat(env)
    epoch = app.runtime.claim(run_id, "worker")
    app.answers.commit(
        env["actor"], turn_id, [app.answers.action("clarify")], None, (run_id, epoch, "worker")
    )
    with app.db.read() as db:
        revision = db.get(AgentRun, run_id).revision
    app.runtime.cancel(env["actor"], run_id, revision)
    app.conversations.mark_failed(turn_id, "LATE_FAILURE")
    packet = app.answers.events(env["actor"], turn_id)
    assert packet["status"] == "completed"
    assert not any(event["type"] == "answer.interrupted" for event in packet["events"])


def test_chat_interruption_rolls_back_with_run_transaction(env, monkeypatch):
    from careerforge.services import runtime as module

    app = env["app"]
    _, turn_id, run_id = submit_chat(env)
    with app.db.read() as db:
        revision = db.get(AgentRun, run_id).revision

    def fail_settlement(*_args):
        raise RuntimeError("synthetic transaction failure")

    monkeypatch.setattr(module, "settle", fail_settlement)
    with pytest.raises(RuntimeError, match="synthetic transaction failure"):
        app.runtime.cancel(env["actor"], run_id, revision)
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "queued"
        assert db.get(Turn, turn_id).status == "queued"
        assert not db.scalars(
            select(StreamEvent).where(
                StreamEvent.turn_id == turn_id, StreamEvent.event_type == "answer.interrupted"
            )
        ).all()
        assert db.get(FundingAgreement, env["funding"]).calls_held == 8


@pytest.mark.parametrize("unexpected", [False, True])
@pytest.mark.asyncio
async def test_stale_worker_error_cannot_interrupt_replacement_turn(env, monkeypatch, unexpected):
    from careerforge.errors import DomainError

    app = env["app"]
    _, turn_id, run_id = submit_chat(env)

    async def replaced_while_processing(_turn_id, lease):
        with app.db.transaction() as db:
            db.get(AgentRun, run_id).lease_until = clock(db) - timedelta(seconds=1)
        app.runtime.reap()
        assert app.runtime.claim(run_id, "replacement") > lease[1]
        with app.db.transaction() as db:
            db.get(Turn, turn_id).status = "running"
        if unexpected:
            raise RuntimeError("synthetic stale worker error")
        raise DomainError("MODEL_RESULT_UNCERTAIN", "synthetic late provider error")

    monkeypatch.setattr(app.conversations, "process", replaced_while_processing)
    if unexpected:
        with pytest.raises(RuntimeError, match="synthetic stale worker error"):
            await app.execute(run_id, "old-worker")
    else:
        await app.execute(run_id, "old-worker")
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).worker_id == "replacement"
        assert db.get(AgentRun, run_id).status == "running"
        assert db.get(Turn, turn_id).status == "running"
        assert not db.scalars(
            select(StreamEvent).where(
                StreamEvent.turn_id == turn_id, StreamEvent.event_type == "answer.interrupted"
            )
        ).all()
