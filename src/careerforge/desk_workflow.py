"""Read-only desk projections and explicitly bounded workflow execution."""

from sqlalchemy import select

from .models import AgentRun, Outbox
from .models_dialogue import WorkflowRunLink
from .services.runtime import TERMINAL


async def execute_desk_job(service, job, *, status_key="status"):
    # Only this newly granted request and descendants may execute, never drain old jobs.
    seeds = set(job.get("run_refs", [])) | ({job["run_id"]} if job.get("run_id") else set())
    for _ in range(250):
        with service.db.read() as db:
            ids = (
                set(
                    db.scalars(
                        select(AgentRun.id).where(
                            AgentRun.principal_id == job["actor"],
                            AgentRun.funding_id == job["funding_id"],
                        )
                    )
                )
                if job.get("funding_id")
                else seeds
            )
            messages = list(
                db.scalars(
                    select(Outbox).where(
                        Outbox.kind == "workflow.milestone",
                        Outbox.aggregate_id.in_(ids),
                        Outbox.delivered.is_(False),
                    )
                )
            )
            linked = set(
                db.scalars(select(WorkflowRunLink.run_id).where(WorkflowRunLink.run_id.in_(ids)))
            )
        for m in messages:
            if m.aggregate_id in linked:
                service.continuation.consume(m.id, m.aggregate_id)
        with service.db.read() as db:
            # Continuation can enqueue a plan under the SAME bounded grant.
            rows = list(
                db.scalars(
                    select(AgentRun).where(
                        AgentRun.principal_id == job["actor"],
                        (AgentRun.funding_id == job["funding_id"])
                        if job.get("funding_id")
                        else AgentRun.id.in_(seeds),
                    )
                )
            )
            pending = next((r for r in rows if r.status == "queued"), None)
        job["run_refs"] = [r.id for r in rows]
        if pending:
            await service.execute(pending.id)
            continue
        if any(r.status not in TERMINAL for r in rows):
            job[status_key] = "interrupted"
            job["error"] = "WAITING_FOR_EXTERNAL_WORKER"
        else:
            job[status_key] = (
                "failed"
                if any(r.status in {"failed", "budget_stopped"} for r in rows)
                else "completed"
            )
        return
    job[status_key] = "interrupted"
    job["error"] = "DESK_EXECUTION_LIMIT"
