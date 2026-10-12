from uuid import UUID

from sqlalchemy.orm import sessionmaker

from careerforge.application.run_service import claim, event, leased, transition
from careerforge.infrastructure.db.models import AgentRun
from careerforge.infrastructure.db.session import get_engine
from careerforge.workflows.demo import SUPPORTED_VERSIONS, execute

# Only reviewed, versioned Python functions are executable. No client-defined stages.
WORKFLOWS = {("market_research", version): execute for version in SUPPORTED_VERSIONS}


def execute_registered(run_id: UUID, worker: str) -> None:
    factory = sessionmaker(get_engine(), expire_on_commit=False)
    with factory.begin() as db:
        run = db.get(AgentRun, run_id)
        if run is None:
            return
        executor = WORKFLOWS.get((run.workflow_type, run.workflow_version))
    if executor is not None:
        executor(run_id, worker)
        return
    with factory.begin() as db:
        epoch = claim(db, run_id, worker)
        if epoch is None:
            return
        run = leased(db, run_id, epoch, worker)
        event(db, run, "workflow.incompatible", {"workflow_version": run.workflow_version})
        transition(db, run, "failed")
