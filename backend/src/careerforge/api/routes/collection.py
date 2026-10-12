from uuid import UUID

from fastapi import APIRouter, Request

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.application import collection_service as service
from careerforge.schemas.collection import CollectionInput
from careerforge.schemas.resources import Envelope
from careerforge.workflows.job_collection import tool_definitions

router = APIRouter(prefix="/api/v1/collection", tags=["job-collection"])


def envelope(request, data):
    return {"data": data, "request_id": request.state.request_id}


@router.get("/config", response_model=Envelope)
def config(request: Request, user: Authenticated):
    return envelope(request, service.configuration())


@router.get("/skill", response_model=Envelope)
def skill(request: Request, user: Authenticated):
    return envelope(
        request,
        {
            "name": "official-recruitment",
            "content": service.SKILL_PATH.read_text(encoding="utf-8"),
            "tools": tool_definitions([row["id"] for row in service.configuration()["channels"]]),
        },
    )


@router.post("/runs", response_model=Envelope, status_code=202)
def start(
    request: Request,
    value: CollectionInput,
    user: Authenticated,
    db: Database,
    idempotency_key: IdempotencyKey = None,
):
    return envelope(request, service.start(db, user.id, value, idempotency_key))


@router.get("/runs", response_model=Envelope)
def history(request: Request, user: Authenticated, db: Database):
    return envelope(request, service.history(db, user.id))


@router.get("/runs/{run_id}", response_model=Envelope)
def result(run_id: UUID, request: Request, user: Authenticated, db: Database):
    return envelope(request, service.result(db, user.id, run_id))
