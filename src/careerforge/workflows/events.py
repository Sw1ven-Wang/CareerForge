from sqlalchemy import select

from ..models import StageArtifact
from ..services.runtime import event, leased


def project_event(app, run_id, epoch, worker, stage, phase):
    """Emit an allowlisted business event once, inside the existing lease transaction."""
    key = f"graph-event:{stage}:{phase}"
    with app.db.transaction() as db:
        run = leased(db, run_id, epoch, worker)
        old = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id, StageArtifact.stage_key == key
            )
        )
        if old is not None:
            return
        payload = {"workflow_run_id": run.id, "stage": stage, "graph_node": stage}
        db.add(
            StageArtifact(run_id=run.id, stage_key=key, input_hash=run.input_hash, payload=payload)
        )
        event(db, run, f"workflow.stage.{phase}", payload)
