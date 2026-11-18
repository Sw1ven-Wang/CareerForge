"""No aliases over incompatible old endpoints: migration is explicit and private."""

from fastapi import Depends, Query

from ..contracts_compat import LegacyImportInput, LegacySourceInput


def register_routes(app, service, actor_dependency):
    @app.post("/api/v1/compat/legacy/imports", status_code=201)
    def create(body: LegacyImportInput, actor=Depends(actor_dependency)):
        return service.legacy.import_bundle(actor, body)

    @app.get("/api/v1/compat/legacy/imports/{batch_id}/records")
    def records(
        batch_id: str,
        after: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        actor=Depends(actor_dependency),
    ):
        return service.legacy.records(actor, batch_id, after, limit)

    @app.post("/api/v1/compat/legacy/imports/{batch_id}/sources")
    def source(batch_id: str, body: LegacySourceInput, actor=Depends(actor_dependency)):
        return service.legacy.import_source(actor, batch_id, body)
