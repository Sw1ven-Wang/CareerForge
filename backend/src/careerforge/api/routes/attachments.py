from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.application import attachment_service as service
from careerforge.schemas.resources import Envelope, VersionInput

router = APIRouter(prefix="/api/v1/attachments", tags=["attachments"])


@router.post("", status_code=201, response_model=Envelope)
async def upload(
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
    file: Annotated[UploadFile, File()],
    purpose: Annotated[Literal["source", "personal_evidence", "submission"], Form()],
) -> dict[str, Any]:
    body = await file.read(service.MAX_BYTES + 1)
    return {
        "data": service.upload(db, user.id, file.filename or "", body, purpose, key),
        "request_id": request.state.request_id,
    }


@router.get("/{identifier}", response_model=Envelope)
def metadata(
    identifier: UUID, request: Request, db: Database, user: Authenticated
) -> dict[str, Any]:
    return {
        "data": service.metadata(service.available(db, user.id, identifier)),
        "request_id": request.state.request_id,
    }


@router.get("/{identifier}/content")
def content(
    identifier: UUID, db: Database, user: Authenticated, snapshot_id: UUID | None = None
) -> Response:
    row = service.content(db, user.id, identifier, snapshot_id)
    return Response(
        content=row.body,
        media_type=row.mime,
        headers={"Content-Disposition": "attachment", "Cache-Control": "no-store"},
    )


@router.delete("/{identifier}", status_code=202, response_model=Envelope)
def delete(
    identifier: UUID,
    body: VersionInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
) -> dict[str, Any]:
    return {
        "data": service.delete(db, user.id, identifier, body.expected_version, key),
        "request_id": request.state.request_id,
    }
