from uuid import UUID

from fastapi import APIRouter, Request

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.api.routes.planning import wrap
from careerforge.application import study_service as service
from careerforge.domain.json_types import JsonObject as Json
from careerforge.schemas.resources import Envelope
from careerforge.schemas.study import (
    CandidateInput,
    ExtractionRecovery,
    ModuleProgress,
    RoleInput,
    TrackInput,
    TrackSettings,
)

router = APIRouter(prefix="/api/v1/study", tags=["agent-study"])


@router.get("/tracks", response_model=Envelope)
def listing(request: Request, db: Database, user: Authenticated) -> Json:
    return wrap(request, service.listing(db, user.id))


@router.post("/tracks", response_model=Envelope, status_code=201)
def create(
    body: TrackInput, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
) -> Json:
    return wrap(request, service.create(db, user.id, body, key))


@router.get("/tracks/{identifier}", response_model=Envelope)
def detail(identifier: UUID, request: Request, db: Database, user: Authenticated) -> Json:
    return wrap(request, service.detail(db, service.track(db, user.id, identifier)))


@router.put("/tracks/{identifier}", response_model=Envelope)
def configure(
    identifier: UUID, body: TrackSettings, request: Request, db: Database, user: Authenticated
) -> Json:
    return wrap(request, service.configure(db, user.id, identifier, body))


@router.post("/tracks/{identifier}/candidates", response_model=Envelope, status_code=201)
def candidate(
    identifier: UUID,
    body: CandidateInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> Json:
    return wrap(request, service.add_candidate(db, user.id, identifier, body, key))


@router.post("/tracks/{identifier}/runs", response_model=Envelope, status_code=202)
def start(
    identifier: UUID,
    body: RoleInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> Json:
    return wrap(request, service.start(db, user.id, identifier, body, key))


@router.put("/tracks/{identifier}/progress", response_model=Envelope)
def progress(
    identifier: UUID, body: ModuleProgress, request: Request, db: Database, user: Authenticated
) -> Json:
    return wrap(request, service.progress(db, user.id, identifier, body))


@router.post("/tracks/{identifier}/recover-extraction", response_model=Envelope)
def recover(
    identifier: UUID,
    body: ExtractionRecovery,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> Json:
    return wrap(request, service.recover_extraction(db, user.id, identifier, body, key))
