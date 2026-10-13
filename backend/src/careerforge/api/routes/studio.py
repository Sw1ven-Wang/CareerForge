from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import select

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.api.routes.planning import wrap
from careerforge.application import studio_service as service
from careerforge.infrastructure.db.models import LearningWorkspace
from careerforge.schemas.resources import Envelope
from careerforge.schemas.studio import (
    CommandInput,
    ConfirmInput,
    FileInput,
    StudioAgentInput,
    WorkspaceInput,
)

router = APIRouter(prefix="/api/v1/studio", tags=["learning-studio"])


@router.get("/workspaces", response_model=Envelope)
def listing(request: Request, db: Database, user: Authenticated):
    rows = db.scalars(
        select(LearningWorkspace)
        .where(LearningWorkspace.owner_id == user.id)
        .order_by(LearningWorkspace.created_at.desc())
        .limit(100)
    )
    return wrap(
        request, [{"workspace_id": str(r.id), "name": r.name, "revision": r.revision} for r in rows]
    )


@router.post("/workspaces", response_model=Envelope, status_code=201)
def create(
    body: WorkspaceInput, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
):
    return wrap(request, service.create(db, user.id, body, key))


@router.get("/workspaces/{identifier}", response_model=Envelope)
def detail(identifier: UUID, request: Request, db: Database, user: Authenticated):
    return wrap(request, service.detail(service.workspace(db, user.id, identifier), db))


@router.put("/workspaces/{identifier}/file", response_model=Envelope)
def save(identifier: UUID, body: FileInput, request: Request, db: Database, user: Authenticated):
    return wrap(request, service.save_file(db, user.id, identifier, body))


@router.post("/workspaces/{identifier}/agent-runs", response_model=Envelope, status_code=202)
def agent(
    identifier: UUID,
    body: StudioAgentInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
):
    return wrap(request, service.start_agent(db, user.id, identifier, body, key))


@router.post("/workspaces/{identifier}/patches/{action_id}/apply", response_model=Envelope)
def apply(
    identifier: UUID,
    action_id: UUID,
    body: ConfirmInput,
    request: Request,
    db: Database,
    user: Authenticated,
):
    return wrap(request, service.apply(db, user.id, identifier, action_id, body))


@router.post("/workspaces/{identifier}/commands", response_model=Envelope, status_code=201)
def propose(
    identifier: UUID, body: CommandInput, request: Request, db: Database, user: Authenticated
):
    return wrap(request, service.propose_command(db, user.id, identifier, body))


@router.post("/workspaces/{identifier}/commands/{action_id}/execute", response_model=Envelope)
def execute(
    identifier: UUID,
    action_id: UUID,
    body: ConfirmInput,
    request: Request,
    db: Database,
    user: Authenticated,
):
    return wrap(request, service.execute_command(db, user.id, identifier, action_id, body))


from careerforge.schemas.studio import ReferenceLinkInput  # noqa: E402


@router.post("/workspaces/{identifier}/reference-links", response_model=Envelope, status_code=201)
def bind_reference(
    identifier: UUID,
    body: ReferenceLinkInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
):
    return wrap(request, service.bind_reference(db, user.id, identifier, body, key))
