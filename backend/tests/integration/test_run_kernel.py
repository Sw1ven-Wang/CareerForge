from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from careerforge.application.run_service import (
    artifact,
    cancel,
    claim,
    commit_stage,
    create_run,
    db_now,
    heartbeat,
    reap,
    reserve_call,
    resume,
    wait_for_choice,
)
from careerforge.domain.errors import BusinessError
from careerforge.domain.runs import LeaseLost
from careerforge.infrastructure.db.models import (
    AgentRun,
    OutboxMessage,
    ResumeDecision,
    RunEvent,
    StageArtifact,
    User,
    WaitRequest,
)
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.queue.outbox_dispatcher import dispatch_once
from careerforge.workflows.demo import VERSION, execute
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.infrastructure


@pytest.fixture
def factory():
    return sessionmaker(get_engine(), expire_on_commit=False)


@pytest.fixture
def run_case(factory):
    with factory.begin() as db:
        owner = User(email=f"{uuid4()}@example.test", password_hash="synthetic-not-login")
        db.add(owner)
        db.flush()
        run = create_run(db, owner.id, "market_research", VERSION, {"synthetic": True})
        return owner.id, run.id


def test_duplicate_deliveries_and_stable_resume(factory, run_case):
    owner, run_id = run_case
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda w: execute(run_id, w), ["worker-a", "worker-b"]))
    with factory.begin() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "waiting_user" and run.worker_id is None
        assert run.calls_reserved == 1
        wait = db.scalar(select(WaitRequest).where(WaitRequest.run_id == run_id))
        version, manifest = run.version, run.input_manifest_hash
        resume(db, owner, run_id, version, wait.id, manifest, {"choice": "accept"})
    with factory.begin() as db:
        # Retry uses the same decision even though expected_version is now old.
        resume(db, owner, run_id, version, wait.id, manifest, {"choice": "accept"})
    with factory.begin() as db, pytest.raises(BusinessError, match="DECISION_CONFLICT"):
        resume(db, owner, run_id, version, wait.id, manifest, {"choice": "reject"})
    execute(run_id, "worker-c")
    execute(run_id, "worker-d")
    with factory.begin() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "succeeded" and run.calls_reserved == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(StageArtifact)
                .where(StageArtifact.run_id == run_id)
            )
            == 2
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(ResumeDecision)
                .where(ResumeDecision.wait_request_id == wait.id)
            )
            == 1
        )
        events = db.scalars(
            select(RunEvent.seq).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        ).all()
        assert events == list(range(1, run.event_seq + 1))


def test_crash_after_stage_commit_replays_without_duplicate(factory, run_case):
    _, run_id = run_case
    with factory.begin() as db:
        epoch = claim(db, run_id, "old")
        commit_stage(db, run_id, epoch, "old", "draft", "1", {"draft": "committed"})
    with factory.begin() as db:
        db.get(AgentRun, run_id).lease_until = db_now(db) - timedelta(seconds=1)
    with factory.begin() as db:
        assert reap(db) >= 1
    execute(run_id, "replacement")
    with factory.begin() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "waiting_user" and run.calls_reserved == 0
        assert artifact(db, run, "draft", "1").payload == {"draft": "committed"}
    with factory.begin() as db, pytest.raises(LeaseLost):
        commit_stage(db, run_id, epoch, "old", "late", "1", {})


def test_expired_heartbeat_cannot_revive_and_reaper_rechecks(factory, run_case):
    _, run_id = run_case
    with factory.begin() as db:
        epoch = claim(db, run_id, "worker")
        heartbeat(db, run_id, epoch, "worker")
    with factory.begin() as db:
        reap(db)
        assert db.get(AgentRun, run_id).status == "running"
        db.get(AgentRun, run_id).lease_until = db_now(db) - timedelta(seconds=1)
    with factory.begin() as db, pytest.raises(LeaseLost):
        heartbeat(db, run_id, epoch, "worker")
    with factory.begin() as db:
        reap(db)
        next_epoch = claim(db, run_id, "new")
        assert next_epoch > epoch


def test_cancel_prevents_late_side_effect(factory, run_case):
    owner, run_id = run_case
    with factory.begin() as db:
        epoch = claim(db, run_id, "old")
        version = db.get(AgentRun, run_id).version
        cancel(db, owner, run_id, version)
    with factory.begin() as db, pytest.raises(LeaseLost):
        commit_stage(db, run_id, epoch, "old", "result", "1", {})


def test_budget_survives_crash_and_takeover(factory, run_case):
    _, run_id = run_case
    with factory.begin() as db:
        run = db.get(AgentRun, run_id)
        run.call_budget = 1
        epoch = claim(db, run_id, "old")
        assert reserve_call(db, run_id, epoch, "old", {"request": 1}) is not None
        run.lease_until = db_now(db) - timedelta(seconds=1)
    with factory.begin() as db:
        reap(db)
    execute(run_id, "new")
    with factory.begin() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "budget_stopped" and run.calls_reserved == 1


def test_wait_identity_conflict_and_decision_replay_after_crash(factory, run_case):
    owner, run_id = run_case
    execute(run_id, "initial")
    with factory.begin() as db:
        run = db.get(AgentRun, run_id)
        wait = db.scalar(select(WaitRequest).where(WaitRequest.run_id == run_id))
        resume(
            db, owner, run_id, run.version, wait.id, run.input_manifest_hash, {"choice": "accept"}
        )
        epoch = claim(db, run_id, "crashed")
    with factory.begin() as db, pytest.raises(BusinessError, match="WAIT_CONFLICT"):
        wait_for_choice(
            db,
            run_id,
            epoch,
            "crashed",
            "review",
            {"text": "changed"},
            ["accept", "reject"],
            question_version="2",
        )
    with factory.begin() as db:
        db.get(AgentRun, run_id).lease_until = db_now(db) - timedelta(seconds=1)
    with factory.begin() as db:
        reap(db)
    execute(run_id, "recovery")
    with factory.begin() as db:
        assert db.get(AgentRun, run_id).status == "succeeded"
        assert (
            db.scalar(
                select(func.count()).select_from(WaitRequest).where(WaitRequest.run_id == run_id)
            )
            == 1
        )


def test_outbox_send_failure_retains_durable_notification(factory, run_case):
    _, run_id = run_case

    def fail(_):
        raise OSError("injected broker failure")

    with pytest.raises(OSError):
        dispatch_once(fail, only_run_id=run_id)
    with factory.begin() as db:
        assert (
            db.scalar(
                select(OutboxMessage).where(
                    OutboxMessage.run_id == run_id, OutboxMessage.delivered_at.is_(None)
                )
            )
            is not None
        )
    sent = []
    dispatch_once(sent.append, only_run_id=run_id)
    assert str(run_id) in sent


def test_late_model_usage_accounted_after_cancellation(factory, run_case):
    from careerforge.application.model_call_service import settle_receipt
    from careerforge.infrastructure.db.models import ModelCallReceipt

    owner, run_id = run_case
    with factory.begin() as db:
        epoch = claim(db, run_id, "old")
        receipt_id = reserve_call(db, run_id, epoch, "old", {"synthetic": True})
        cancel(db, owner, run_id, db.get(AgentRun, run_id).version)
    settle_receipt(receipt_id, {"status": "known", "input_tokens": 3, "output_tokens": 2})
    with factory.begin() as db:
        assert db.get(AgentRun, run_id).status == "cancelled"
        assert db.get(ModelCallReceipt, receipt_id).usage["input_tokens"] == 3
    with factory.begin() as db, pytest.raises(LeaseLost):
        commit_stage(db, run_id, epoch, "old", "result", "1", {})


def test_bounded_transient_retry_and_unknown_usage(factory, run_case):
    import asyncio

    from careerforge.application.model_call_service import invoke_model
    from careerforge.infrastructure.db.models import ModelCallReceipt
    from careerforge.infrastructure.llm.provider import ModelError
    from careerforge.schemas.model_io import ModelRequest, ModelResult

    _, run_id = run_case
    with factory.begin() as db:
        epoch = claim(db, run_id, "model-test")

    class FlakyProvider:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            if self.calls == 1:
                raise ModelError("timeout", retryable=True)
            return ModelResult(provider="fake", finish_reason="stop", synthetic=True)

    provider = FlakyProvider()
    result = asyncio.run(
        invoke_model(
            run_id,
            epoch,
            "model-test",
            provider,
            ModelRequest(model="fake", prompt="synthetic retry"),
        )
    )
    assert result.synthetic and provider.calls == 2
    with factory.begin() as db:
        assert db.get(AgentRun, run_id).calls_reserved == 2
        receipts = db.scalars(
            select(ModelCallReceipt).where(ModelCallReceipt.run_id == run_id)
        ).all()
        assert sorted(r.status for r in receipts) == ["completed", "unknown"]


def test_invalid_model_structure_fails_stage_without_success(factory, run_case, monkeypatch):
    from careerforge.infrastructure.llm.fake import FakeModel
    from careerforge.infrastructure.llm.provider import ModelError

    _, run_id = run_case

    async def invalid(self, request):
        raise ModelError("invalid_response", retryable=False)

    monkeypatch.setattr(FakeModel, "complete", invalid)
    execute(run_id, "invalid-test")
    with factory.begin() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "failed" and run.calls_reserved == 1
        assert artifact(db, run, "draft", "1") is None


def test_database_disconnect_rolls_back_run_and_outbox(factory, run_case):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    owner, _ = run_case
    with pytest.raises(DBAPIError):
        with factory.begin() as db:
            run = create_run(db, owner, "market_research", VERSION, {"disconnect": True})
            run_id = run.id
            db.flush()
            pid = db.scalar(select(func.pg_backend_pid()))
            with get_engine().connect() as killer:
                killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    with factory.begin() as db:
        assert db.get(AgentRun, run_id) is None
        assert db.scalar(select(OutboxMessage).where(OutboxMessage.run_id == run_id)) is None


def test_two_reapers_only_revoke_once(factory, run_case):
    _, run_id = run_case
    with factory.begin() as db:
        epoch = claim(db, run_id, "expired")
        db.get(AgentRun, run_id).lease_until = db_now(db) - timedelta(seconds=1)

    def recover(_):
        with factory.begin() as db:
            return reap(db)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(recover, range(2)))
    with factory.begin() as db:
        assert db.get(AgentRun, run_id).epoch == epoch + 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.run_id == run_id)
            )
            == 2
        )


def test_reaper_does_not_steal_row_locked_renewal(factory, run_case):
    _, run_id = run_case
    with factory.begin() as db:
        epoch = claim(db, run_id, "renewing")
    with factory.begin() as renewal:
        heartbeat(renewal, run_id, epoch, "renewing")
        with factory.begin() as recovery:
            reap(recovery)
    with factory.begin() as db:
        run = db.get(AgentRun, run_id)
        assert run.epoch == epoch and run.status == "running"
