from uuid import UUID

from fastapi import APIRouter, Query, Request

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


from careerforge.application import study_practice_service as practice  # noqa: E402
from careerforge.schemas.study_practice import AttemptCloseInput, ModuleAttemptInput  # noqa: E402


@router.get("/tracks/{identifier}/attempts", response_model=Envelope)
def module_attempts(
    identifier: UUID,
    plan_id: UUID,
    module_key: str,
    request: Request,
    db: Database,
    user: Authenticated,
) -> Json:
    return wrap(request, practice.attempts(db, user.id, identifier, plan_id, module_key))


@router.post("/tracks/{identifier}/attempts", response_model=Envelope, status_code=201)
def start_attempt(
    identifier: UUID,
    body: ModuleAttemptInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> Json:
    return wrap(request, practice.create_attempt(db, user.id, identifier, body, key))


@router.post("/tracks/{identifier}/attempts/{attempt_id}/close", response_model=Envelope)
def close_attempt(
    identifier: UUID,
    attempt_id: UUID,
    body: AttemptCloseInput,
    request: Request,
    db: Database,
    user: Authenticated,
) -> Json:
    return wrap(request, practice.close_attempt(db, user.id, identifier, attempt_id, body))


@router.get("/tracks/{identifier}/runs/{run_id}/context", response_model=Envelope)
def context_trace(
    identifier: UUID, run_id: UUID, request: Request, db: Database, user: Authenticated
) -> Json:
    from careerforge.application.context_service import detail as context_detail

    return wrap(request, context_detail(db, user.id, identifier, run_id))


@router.get("/tracks/{identifier}/runs/{run_id}/context/evidence", response_model=Envelope)
def context_evidence(
    identifier: UUID,
    run_id: UUID,
    request: Request,
    db: Database,
    user: Authenticated,
    evidence_id: str = Query(min_length=1, max_length=100),
    offset: int = Query(default=0, ge=0, le=2000000),
    limit: int = Query(default=2500, ge=100, le=3500),
) -> Json:
    from careerforge.application.context_service import context_run, recall
    from careerforge.schemas.context import ContextReadArgs

    run = context_run(db, user.id, identifier, run_id)
    return wrap(
        request,
        recall(
            db,
            user.id,
            run.input_manifest,
            ContextReadArgs(evidence_id=evidence_id, offset=offset, limit=limit),
        ),
    )


from careerforge.schemas.subtasks import ReviewBatchInput  # noqa: E402


@router.post("/tracks/{identifier}/review-batches", response_model=Envelope, status_code=202)
def review_batch(
    identifier: UUID,
    body: ReviewBatchInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> Json:
    from careerforge.application.review_batch_service import start as start_batch

    return wrap(request, start_batch(db, user.id, identifier, body, key))


from careerforge.schemas.delegation import DelegationInput  # noqa: E402


@router.post("/tracks/{identifier}/delegations", response_model=Envelope, status_code=202)
def delegate(
    identifier: UUID,
    body: DelegationInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> Json:
    from careerforge.application.autonomous_service import start as start_delegation

    return wrap(request, start_delegation(db, user.id, identifier, body, key))


@router.get("/tracks/{identifier}/delegations", response_model=Envelope)
def delegation_reports(
    identifier: UUID, request: Request, db: Database, user: Authenticated
) -> Json:
    from careerforge.application.autonomous_service import reports

    return wrap(request, reports(db, service.track(db, user.id, identifier)))
