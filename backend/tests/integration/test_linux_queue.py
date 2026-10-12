"""Requires a separately running Docker/Linux Celery worker, never eager mode."""

import os
import subprocess
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from careerforge.application.run_service import create_run, resume
from careerforge.infrastructure.db.models import (
    AgentRun,
    OutboxMessage,
    RunAttempt,
    StageArtifact,
    User,
    WaitRequest,
)
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.queue.celery_app import app
from careerforge.infrastructure.queue.outbox_dispatcher import dispatch_once
from careerforge.workflows.demo import VERSION
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.getenv("RUN_LINUX_QUEUE_TESTS") != "1", reason="Linux Celery worker not requested"
    ),
]


def wait_status(run_id, expected):
    for _ in range(100):
        with Session(get_engine()) as db:
            run = db.get(AgentRun, run_id)
            if run.status == expected:
                return
        time.sleep(0.1)
    raise AssertionError(f"Worker did not reach {expected}")


def test_real_queue_duplicate_delivery_wait_resume():
    assert not app.conf.task_always_eager
    with Session(get_engine()) as db, db.begin():
        owner = User(email=f"queue-{uuid4()}@example.test", password_hash="synthetic-not-login")
        db.add(owner)
        db.flush()
        run = create_run(db, owner.id, "market_research", VERSION, {"synthetic": True})
        run_id, owner_id = run.id, owner.id
    assert dispatch_once(only_run_id=run_id) == 1
    app.send_task("careerforge.execute_run", args=[str(run_id)])
    wait_status(run_id, "waiting_user")
    with Session(get_engine()) as db, db.begin():
        run = db.get(AgentRun, run_id)
        assert run.worker_id is None and run.lease_until is None
        wait = db.scalar(select(WaitRequest).where(WaitRequest.run_id == run_id))
        assert (
            db.scalar(
                select(func.count()).select_from(RunAttempt).where(RunAttempt.run_id == run_id)
            )
            == 1
        )
        resume(
            db,
            owner_id,
            run_id,
            run.version,
            wait.id,
            run.input_manifest_hash,
            {"choice": "accept"},
        )
    assert dispatch_once(only_run_id=run_id) == 1
    wait_status(run_id, "succeeded")
    with Session(get_engine()) as db:
        run = db.get(AgentRun, run_id)
        assert run.calls_reserved == 1
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
                .select_from(OutboxMessage)
                .where(OutboxMessage.run_id == run_id, OutboxMessage.delivered_at.is_(None))
            )
            == 0
        )


def test_killed_linux_worker_replays_committed_stage():
    from careerforge.application.run_service import db_now, reap

    with Session(get_engine()) as db, db.begin():
        owner = User(email=f"kill-{uuid4()}@example.test", password_hash="synthetic-not-login")
        db.add(owner)
        db.flush()
        run = create_run(
            db,
            owner.id,
            "market_research",
            VERSION,
            {"synthetic": True, "test_pause_after_draft_seconds": 30},
        )
        run_id = run.id
    dispatch_once(only_run_id=run_id)
    for _ in range(100):
        with Session(get_engine()) as db:
            run = db.get(AgentRun, run_id)
            committed = db.scalar(
                select(func.count())
                .select_from(StageArtifact)
                .where(StageArtifact.run_id == run_id)
            )
            if committed:
                worker_id, old_epoch = run.worker_id, run.epoch
                break
        time.sleep(0.1)
    else:
        raise AssertionError("Worker did not commit draft")
    _, pid, _ = worker_id.split(":")
    assert pid.isdecimal()
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "worker",
            "python",
            "-c",
            "import os, signal, sys; os.kill(int(sys.argv[1]), signal.SIGKILL)",
            pid,
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    with Session(get_engine()) as db, db.begin():
        # Deterministic clock injection after actual OS process death.
        db.get(AgentRun, run_id).lease_until = db_now(db) - timedelta(seconds=1)
    with Session(get_engine()) as db, db.begin():
        reap(db)
    dispatch_once(only_run_id=run_id)
    wait_status(run_id, "waiting_user")
    with Session(get_engine()) as db:
        run = db.get(AgentRun, run_id)
        assert run.epoch > old_epoch and run.calls_reserved == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(StageArtifact)
                .where(StageArtifact.run_id == run_id)
            )
            == 1
        )


def test_publish_success_mark_failure_redelivers_without_duplicate(monkeypatch):
    import careerforge.infrastructure.queue.outbox_dispatcher as dispatcher

    with Session(get_engine()) as db, db.begin():
        owner = User(email=f"dispatch-{uuid4()}@example.test", password_hash="synthetic-not-login")
        db.add(owner)
        db.flush()
        run = create_run(db, owner.id, "market_research", VERSION, {"synthetic": True})
        run_id = run.id
    original = dispatcher.mark_delivered

    def crash(_):
        raise OSError("injected failure after broker accepted message")

    monkeypatch.setattr(dispatcher, "mark_delivered", crash)
    with pytest.raises(OSError):
        dispatch_once(only_run_id=run_id)
    wait_status(run_id, "waiting_user")
    monkeypatch.setattr(dispatcher, "mark_delivered", original)
    assert dispatch_once(only_run_id=run_id) == 1
    with Session(get_engine()) as db:
        assert db.get(AgentRun, run_id).calls_reserved == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(StageArtifact)
                .where(StageArtifact.run_id == run_id)
            )
            == 1
        )


def test_s03_real_queue_builds_one_report_after_duplicate_delivery():
    from uuid import UUID

    from careerforge.application.goal_service import save_goal
    from careerforge.application.market_report_service import start
    from careerforge.application.source_service import import_source
    from careerforge.infrastructure.db.models import MarketReport
    from careerforge.schemas.job_extraction import MarketRunInput
    from careerforge.schemas.resources import GoalInput, SourceImport

    with Session(get_engine()) as db, db.begin():
        owner = User(
            email=f"market-queue-{uuid4()}@example.test", password_hash="synthetic-not-login"
        )
        db.add(owner)
        db.flush()
        goal = save_goal(
            db, owner.id, GoalInput(role_direction="Agent开发", job_type="internship"), str(uuid4())
        )
        source = import_source(
            db,
            owner.id,
            SourceImport(
                source_type="job_posting",
                title="Queue JD",
                content="实习 北京\n任职要求：\n- Python 开发",
            ),
            str(uuid4()),
        )
        result = start(
            db,
            owner.id,
            MarketRunInput(
                goal_version_id=goal["goal_version_id"],
                source_snapshot_ids=[source["source_snapshot_id"]],
            ),
            str(uuid4()),
        )
        run_id = UUID(result["run_id"])
    dispatch_once(only_run_id=run_id)
    app.send_task("careerforge.execute_run", args=[str(run_id)])
    wait_status(run_id, "succeeded")
    with Session(get_engine()) as db:
        report = db.scalar(select(MarketReport).where(MarketReport.run_id == run_id))
        assert report.payload["skill_statistics"][0]["numerator"] == 1
        assert db.get(AgentRun, run_id).calls_reserved == 1
        assert (
            db.scalar(
                select(func.count()).select_from(MarketReport).where(MarketReport.run_id == run_id)
            )
            == 1
        )
