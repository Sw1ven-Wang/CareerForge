from uuid import UUID

from fastapi import APIRouter, Request

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.application import source_library_service as service
from careerforge.schemas.resources import Envelope
from careerforge.schemas.source_library import (
    CategoryInput,
    CategoryRevision,
    LibrarySourceRevision,
)

router = APIRouter(prefix="/api/v1/source-library", tags=["source-library"])


def envelope(request, data):
    return {"data": data, "request_id": request.state.request_id}


@router.post("/initialize", response_model=Envelope)
def initialize(request: Request, user: Authenticated, db: Database):
    return envelope(request, service.initialize(db, user.id))


@router.post("/categories", response_model=Envelope, status_code=201)
def create_category(
    request: Request,
    value: CategoryInput,
    user: Authenticated,
    db: Database,
    idempotency_key: IdempotencyKey = None,
):
    return envelope(request, service.save_category(db, user.id, value, idempotency_key))


@router.post("/categories/{category_id}", response_model=Envelope)
def revise_category(
    category_id: UUID,
    request: Request,
    value: CategoryRevision,
    user: Authenticated,
    db: Database,
    idempotency_key: IdempotencyKey = None,
):
    return envelope(
        request, service.save_category(db, user.id, value, idempotency_key, category_id)
    )


@router.post("/sources/{source_id}", response_model=Envelope)
def revise_source(
    source_id: UUID,
    request: Request,
    value: LibrarySourceRevision,
    user: Authenticated,
    db: Database,
    idempotency_key: IdempotencyKey = None,
):
    return envelope(request, service.edit_source(db, user.id, source_id, value, idempotency_key))
