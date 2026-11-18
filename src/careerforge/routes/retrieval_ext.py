"""Explicit funded, frozen-publication indexing; the general run API tracks completion."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from fastapi import Depends, FastAPI

from ..contracts_retrieval_ext import IndexJobInput

if TYPE_CHECKING:
    from ..application import Application


def register_routes(
    app: FastAPI, service: Application, actor_dependency: Callable[..., str]
) -> None:
    @app.post("/api/v1/retrieval/index-jobs", status_code=202)
    def create_index(body: IndexJobInput, actor: str = Depends(actor_dependency)):
        return {
            "run_id": service.retrieval.start_index(actor, body),
            "queued": True,
            "semantic_quality_validated": False,
        }
