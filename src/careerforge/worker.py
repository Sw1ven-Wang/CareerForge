"""Celery delivery adapter. PostgreSQL remains the source of truth."""

import asyncio
from functools import lru_cache

from sqlalchemy import select

from .application import Application
from .config import Settings
from .models import Outbox

try:
    # Celery 5.x ships neither a py.typed marker nor bundled typing stubs.
    # Only this external import is exempt; project errors remain checked.
    from celery import Celery  # type: ignore[import-untyped]
except ImportError as exc:
    raise RuntimeError(
        "Install careerforge[production] to run Celery; no in-memory production fallback"
    ) from exc

settings = Settings.from_env()
celery_app = Celery("careerforge", broker=settings.redis_broker_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_routes={
        "careerforge.execute": {"queue": "careerforge"},
        "careerforge.execute_workflow": {"queue": "careerforge"},
    },
)


@lru_cache
def services():
    return Application(settings)


@celery_app.task(name="careerforge.execute")
def execute(run_id):
    # redis.asyncio clients are event-loop-bound. Never reuse one across asyncio.run calls.
    async def one():
        service = Application(settings)
        try:
            await service.execute(run_id)
        finally:
            client = getattr(service.cache, "redis", None)
            if client is not None:
                await client.aclose()
            service.db.engine.dispose()

    asyncio.run(one())


@celery_app.task(name="careerforge.execute_workflow")
def execute_workflow(execution_id):
    async def one():
        service = Application(settings)
        try:
            await service.execute_workflow(execution_id)
        finally:
            client = getattr(service.cache, "redis", None)
            if client is not None:
                await client.aclose()
            service.db.engine.dispose()

    asyncio.run(one())


def dispatch_once(queue: str = "careerforge"):
    """Dispatch outbox messages; validation may select its own isolated broker queue."""
    service = services()
    service.runtime.reap()
    service.session_workflows.reap()
    service.lifecycle.run_due_maintenance()
    service.process_projections()
    with service.db.read() as db:
        pending = [
            (m.id, m.kind, m.aggregate_id)
            for m in db.scalars(
                select(Outbox)
                .where(Outbox.kind.in_(["run", "workflow.execute"]), Outbox.delivered.is_(False))
                .order_by(Outbox.created_at)
                .limit(100)
            )
        ]
    count = 0
    for identifier, kind, run_id in pending:
        task = (
            "careerforge.execute_workflow" if kind == "workflow.execute" else "careerforge.execute"
        )
        celery_app.send_task(task, args=[run_id], queue=queue)
        with service.db.transaction() as db:
            row = db.get(Outbox, identifier)
            if row:
                row.delivered = True
        count += 1
    return count
