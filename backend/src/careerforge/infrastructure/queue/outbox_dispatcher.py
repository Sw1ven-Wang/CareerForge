import logging
import time
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from careerforge.application.run_service import db_now, reap
from careerforge.infrastructure.db.models import OutboxMessage
from careerforge.infrastructure.db.session import get_engine
from careerforge.infrastructure.queue.celery_app import app

log = logging.getLogger(__name__)


def dispatch_once(
    send: Callable[[str], None] | None = None, only_run_id: UUID | None = None
) -> int:
    factory = sessionmaker(get_engine(), expire_on_commit=False)
    with factory.begin() as db:
        reap(db)
        stmt = select(OutboxMessage.id, OutboxMessage.run_id).where(
            OutboxMessage.delivered_at.is_(None)
        )
        if only_run_id is not None:
            stmt = stmt.where(OutboxMessage.run_id == only_run_id)
        pending = list(db.execute(stmt.order_by(OutboxMessage.created_at).limit(100)))
    count = 0
    for message_id, run_id in pending:
        # Publishing outside DB transactions may duplicate; claim/run stages deduplicate.
        if send is None:
            app.send_task("careerforge.execute_run", args=[str(run_id)])
        else:
            send(str(run_id))
        mark_delivered(message_id)
        count += 1
    return count


def mark_delivered(message_id: UUID) -> None:
    factory = sessionmaker(get_engine())
    with factory.begin() as db:
        row = db.get(OutboxMessage, message_id)
        if row is not None and row.delivered_at is None:
            row.delivered_at = db_now(db)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    sweep_due = 0.0
    while True:
        try:
            dispatch_once()
            from careerforge.config.settings import get_settings

            if get_settings().maintenance_enabled and time.monotonic() >= sweep_due:
                with sessionmaker(get_engine()).begin() as db:
                    from careerforge.application.maintenance_service import clean_once

                    clean_once(db)
                sweep_due = time.monotonic() + 60
        except Exception:
            log.exception("Dispatch failed; pending messages retained")
        time.sleep(1)


if __name__ == "__main__":
    main()
