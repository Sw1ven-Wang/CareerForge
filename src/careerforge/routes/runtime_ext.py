"""HTTP integration for real public build/subscription service operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from fastapi import Depends, FastAPI

from ..contracts_runtime_ext import SharedBuildInput, SubscribeInput, UnsubscribeInput

if TYPE_CHECKING:
    from ..application import Application


def register_routes(
    app: FastAPI, service: Application, actor_dependency: Callable[..., str]
) -> None:
    @app.post("/api/v1/shared-builds")
    def create_shared(body: SharedBuildInput, actor: str = Depends(actor_dependency)):
        return service.shared_builds.create(actor, body)

    @app.post("/api/v1/shared-builds/{run_id}/subscriptions")
    def subscribe(run_id: str, body: SubscribeInput, actor: str = Depends(actor_dependency)):
        return service.shared_builds.subscribe(actor, run_id, body)

    @app.post("/api/v1/shared-builds/{run_id}/unsubscribe")
    def unsubscribe(run_id: str, body: UnsubscribeInput, actor: str = Depends(actor_dependency)):
        return service.shared_builds.unsubscribe(actor, run_id, body)

    @app.get("/api/v1/shared-builds/{run_id}")
    def detail(run_id: str, actor: str = Depends(actor_dependency)):
        return service.shared_builds.detail(actor, run_id)
