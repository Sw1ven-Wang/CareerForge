"""Explicit personal-data control plane, separate from ordinary blocked content reads."""

from fastapi import Depends

from ..contracts_privacy_ext import PrivacyConfirmInput, PrivacyProposalInput, RetentionHoldInput
from ..services.privacy import retention_policy


def register_routes(app, service, actor_dependency):
    @app.get("/api/v1/privacy/policy")
    def policy(user=Depends(actor_dependency)):
        return retention_policy()

    @app.get("/api/v1/privacy/export")
    def export(user=Depends(actor_dependency)):
        return service.privacy.export(user)

    @app.post("/api/v1/privacy/deletion-proposals", status_code=201)
    def propose(body: PrivacyProposalInput, user=Depends(actor_dependency)):
        return service.privacy.propose(user, body.scope)

    @app.get("/api/v1/privacy/deletion-proposals/{identifier}")
    def status(identifier: str, user=Depends(actor_dependency)):
        return service.privacy.status(user, identifier)

    @app.post("/api/v1/privacy/deletion-proposals/{identifier}/confirm")
    async def confirm(identifier: str, body: PrivacyConfirmInput, user=Depends(actor_dependency)):
        return await service.privacy.confirm(user, identifier, **body.model_dump())

    @app.post("/api/v1/privacy/retention-holds", status_code=201)
    def hold(body: RetentionHoldInput, user=Depends(actor_dependency)):
        return service.privacy.create_hold(user, body)

    @app.post("/api/v1/privacy/retention-holds/{identifier}/release")
    def release(identifier: str, user=Depends(actor_dependency)):
        return service.privacy.release_hold(user, identifier)

    @app.post("/api/v1/privacy/retention/purge-expired-audits")
    def purge(user=Depends(actor_dependency)):
        return service.privacy.purge_expired_audits(user)
