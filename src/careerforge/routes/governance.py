"""Explicit authenticated governance endpoints; installation is owned by create_app."""

from fastapi import Depends, FastAPI, Query

from ..contracts_governance import (
    MaintenanceInput,
    MaintenanceToggle,
    TaxonomyDecisionInput,
    TaxonomyProposalInput,
    UpdateExecutionInput,
    UpdatePlanInput,
)


def register_routes(app: FastAPI, service, actor_dependency) -> None:
    @app.get("/api/v1/taxonomy/status")
    def status(namespace_id: str, actor=Depends(actor_dependency)):
        return service.status(actor, namespace_id)

    @app.post("/api/v1/taxonomy/proposals")
    def propose(body: TaxonomyProposalInput, actor=Depends(actor_dependency)):
        return service.propose(actor, body)

    @app.get("/api/v1/taxonomy/proposals/{proposal_id}")
    def proposal(proposal_id: str, actor=Depends(actor_dependency)):
        return service.detail(actor, proposal_id)

    @app.post("/api/v1/taxonomy/proposals/{proposal_id}/decisions")
    def decide(proposal_id: str, body: TaxonomyDecisionInput, actor=Depends(actor_dependency)):
        return service.decide(actor, proposal_id, body)

    @app.get("/api/v1/taxonomy/releases/{release_id}")
    def release(release_id: str, actor=Depends(actor_dependency)):
        return service.release(actor, release_id)

    @app.get("/api/v1/taxonomy/mapping/{publication_id}")
    def mapping(
        publication_id: str,
        topic_key: str,
        schema_version: int | None = Query(default=None, ge=1),
        actor=Depends(actor_dependency),
    ):
        return service.map_facets(actor, publication_id, topic_key, schema_version)

    @app.post("/api/v1/maintenance/plans")
    def create_plan(body: UpdatePlanInput, actor=Depends(actor_dependency)):
        return service.app.lifecycle.create_update_plan(actor, body)

    @app.get("/api/v1/maintenance/plans/{plan_id}")
    def plan(plan_id: str, actor=Depends(actor_dependency)):
        return service.app.lifecycle.update_plan(actor, plan_id)

    @app.post("/api/v1/maintenance/plans/{plan_id}/execute")
    def execute(plan_id: str, body: UpdateExecutionInput, actor=Depends(actor_dependency)):
        return service.app.lifecycle.execute_update(actor, plan_id, body)

    @app.post("/api/v1/maintenance/schedules")
    def schedule(body: MaintenanceInput, actor=Depends(actor_dependency)):
        return service.app.lifecycle.create_maintenance(actor, body)

    @app.patch("/api/v1/maintenance/schedules/{schedule_id}")
    def toggle(schedule_id: str, body: MaintenanceToggle, actor=Depends(actor_dependency)):
        return service.app.lifecycle.set_maintenance(actor, schedule_id, body)

    @app.post("/api/v1/maintenance/schedules/{schedule_id}/tick")
    def tick(schedule_id: str, actor=Depends(actor_dependency)):
        return service.app.lifecycle.tick_maintenance(actor, schedule_id)
