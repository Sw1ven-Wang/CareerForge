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


from careerforge.application.market_report_service import VERSION as MARKET_VERSION  # noqa: E402
from careerforge.workflows.market_research.workflow import execute as execute_market  # noqa: E402

WORKFLOWS[("market_research", MARKET_VERSION)] = execute_market

from careerforge.application.planning_service import VERSION as ALIGNMENT_VERSION  # noqa: E402
from careerforge.workflows.alignment.workflow import execute as execute_alignment  # noqa: E402

WORKFLOWS[("alignment", ALIGNMENT_VERSION)] = execute_alignment

from careerforge.application.repository_service import VERSION as PROJECTS_VERSION  # noqa: E402
from careerforge.workflows.project_research.workflow import execute as execute_projects  # noqa: E402

WORKFLOWS[("project_research", PROJECTS_VERSION)] = execute_projects

from careerforge.application.learning_service import VERSION as LEARNING_VERSION  # noqa: E402
from careerforge.workflows.learning_path.workflow import execute as execute_learning  # noqa: E402

WORKFLOWS[("learning_path", LEARNING_VERSION)] = execute_learning

from careerforge.application.job_contract_service import VERSION as CONTRACT_VERSION  # noqa: E402
from careerforge.workflows.job_contract import execute as execute_contract  # noqa: E402

WORKFLOWS[("market_research", CONTRACT_VERSION)] = execute_contract

from careerforge.application.job_agent_service import VERSION as JOB_AGENT_VERSION  # noqa: E402
from careerforge.workflows.job_agent import execute as execute_job_agent  # noqa: E402

WORKFLOWS[("market_research", JOB_AGENT_VERSION)] = execute_job_agent


from careerforge.application.collection_service import VERSION as COLLECTION_VERSION  # noqa: E402
from careerforge.workflows.job_collection import execute as execute_collection  # noqa: E402

WORKFLOWS[("market_research", COLLECTION_VERSION)] = execute_collection

from careerforge.application.market_agent_service import VERSION as MARKET_AGENT_VERSION  # noqa: E402
from careerforge.workflows.market_agent import execute as execute_market_agent  # noqa: E402

WORKFLOWS[("market_research", MARKET_AGENT_VERSION)] = execute_market_agent
