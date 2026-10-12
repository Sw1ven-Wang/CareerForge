import os
import socket
from uuid import UUID, uuid4

from careerforge.domain.runs import LeaseLost
from careerforge.infrastructure.queue.celery_app import app
from careerforge.workflows.registry import execute_registered


@app.task(name="careerforge.execute_run")  # type: ignore[untyped-decorator]
def execute_run(run_id: str) -> None:
    try:
        execute_registered(UUID(run_id), f"{socket.gethostname()}:{os.getpid()}:{uuid4()}")
    except LeaseLost:
        # Another epoch or cancellation owns the run now.
        return
