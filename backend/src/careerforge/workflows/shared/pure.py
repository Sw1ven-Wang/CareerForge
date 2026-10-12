"""Durable bounded local stages. Callbacks perform DB/text work, never external I/O."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from careerforge.application.run_service import (
    artifact,
    claim,
    commit_stage,
    event,
    leased,
    transition,
)
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.models import AgentRun
from careerforge.infrastructure.db.session import get_engine
from sqlalchemy.orm import Session, sessionmaker

Stage = Callable[[Session, AgentRun], dict[str, Any]]


def execute_pure(run_id: UUID, worker: str, version: str, validate: Stage, produce: Stage) -> None:
    factory = sessionmaker(get_engine(), expire_on_commit=False)
    with factory.begin() as db:
        epoch = claim(db, run_id, worker)
    if epoch is None:
        return
    try:
        with factory.begin() as db:
            run = leased(db, run_id, epoch, worker)
            checked = validate(db, run)
            if artifact(db, run, "validate_inputs", version) is None:
                commit_stage(db, run_id, epoch, worker, "validate_inputs", version, checked)
        with factory.begin() as db:
            run = leased(db, run_id, epoch, worker)
            validate(db, run)
            output = produce(db, run)
            # Recheck DB-time lease before committing business rows and the result together.
            leased(db, run_id, epoch, worker)
            status = output.pop("_run_status", "succeeded")
            commit_stage(db, run_id, epoch, worker, "result", version, output)
            transition(db, run, status)
    except BusinessError as error:
        with factory.begin() as db:
            run = leased(db, run_id, epoch, worker)
            event(
                db,
                run,
                "input.revoked" if error.status == 404 else "stage.failed",
                {"error": error.code},
            )
            transition(db, run, "failed")
