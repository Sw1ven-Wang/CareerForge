from uuid import UUID

from fastapi import APIRouter, Request

from careerforge.api.dependencies import Authenticated, Database, IdempotencyKey
from careerforge.application import job_contract_service as service
from careerforge.application.source_service import get_snapshot
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError, not_found
from careerforge.schemas.job_agent import ExtractionRunInput
from careerforge.schemas.job_contract import CaptureImport, ContextInput, FactRevision
from careerforge.schemas.resources import Envelope

router = APIRouter(prefix="/api/v1/job-data", tags=["job-data-contract"])


def enabled():
    if not get_settings().job_contract_write:
        raise BusinessError(409, "CONTRACT_WRITES_DISABLED", "标准化写入已关闭。")


def response(request, data):
    return {"data": data, "request_id": request.state.request_id}


@router.post("/import", status_code=201, response_model=Envelope)
def ingest(
    body: CaptureImport, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
):
    enabled()
    from pydantic import ValidationError

    try:
        return response(request, service.import_capture(db, user.id, body, key))
    except (ValidationError, AttributeError) as error:
        raise BusinessError(
            422, "INVALID_PROVIDER_PAYLOAD", "来源字段类型不合法，请检查单个岗位对象。"
        ) from error


@router.get("/source-snapshots/{identifier}", response_model=Envelope)
def source_snapshot(identifier: UUID, request: Request, db: Database, user: Authenticated):
    row = service.job_for_source(db, user.id, identifier)
    if not row:
        raise not_found()
    return response(
        request,
        {
            **service.result(row),
            "facts": [service.result(f) for f in service.facts_for(db, user.id, row.id)],
        },
    )


@router.post("/source-snapshots/{identifier}/normalize", response_model=Envelope)
def normalize(
    identifier: UUID, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
):
    enabled()
    from careerforge.infrastructure.db.models import SourceSnapshot

    source = db.get(SourceSnapshot, identifier)
    if source is None or source.owner_id != user.id:
        raise not_found()
    snapshot = get_snapshot(db, user.id, source.source_id, identifier)
    from careerforge.application.source_service import get_source

    row = service.from_legacy(db, user.id, get_source(db, user.id, source.source_id), snapshot)
    return response(request, service.result(row))


@router.get("/artifacts/{identifier}", response_model=Envelope)
def detail(identifier: UUID, request: Request, db: Database, user: Authenticated):
    row = service.read(db, user.id, identifier)
    data = service.result(row)
    if row.kind == "capture":
        data["raw_payload"] = row.raw_bytes.decode("utf-8") if row.raw_bytes is not None else None
    return response(request, data)


@router.post("/facts/{identifier}/versions", status_code=201, response_model=Envelope)
def revise(
    identifier: UUID,
    body: FactRevision,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
):
    enabled()
    return response(request, service.revise_fact(db, user.id, identifier, body, key))


@router.post("/planning-contexts", status_code=201, response_model=Envelope)
def context(
    body: ContextInput, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
):
    enabled()
    return response(request, service.create_context(db, user.id, body, key))


@router.post(
    "/planning-contexts/{identifier}/alignment-runs", status_code=202, response_model=Envelope
)
def alignment(
    identifier: UUID, request: Request, db: Database, user: Authenticated, key: IdempotencyKey
):
    enabled()
    from sqlalchemy import select

    from careerforge.application.common import idempotency
    from careerforge.application.planning_service import VERSION, validate_alignment
    from careerforge.application.run_service import create_run
    from careerforge.infrastructure.db.models import MarketReport

    record = idempotency(
        db, user.id, "job-contract.alignment", key, {"context_id": str(identifier)}
    )
    row = service.read(db, user.id, identifier, "context")
    context = row.payload["context"]
    report = service.read(db, user.id, UUID(context["demand_report_id"]), "report")
    market = db.scalar(
        select(MarketReport).where(
            MarketReport.run_id == report.run_id, MarketReport.owner_id == user.id
        )
    )
    if market is None:
        raise not_found()
    data = {
        "planning_context_id": str(identifier),
        "goal_version_id": context["goal_version_id"],
        "market_report_id": str(market.id),
        "personal_evidence_version_ids": context["personal_evidence_version_ids"],
        "user_constraints": {
            "available_hours": None,
            "notes": "每周工时与语言偏好见冻结规划上下文",
        },
    }
    validate_alignment(db, user.id, data)
    if record.response_summary:
        return response(request, record.response_summary)
    run = create_run(
        db,
        user.id,
        "alignment",
        VERSION,
        data,
        call_budget=0,
        goal_version_id=UUID(context["goal_version_id"]),
    )
    record.response_summary = {"run_id": str(run.id), "status": run.status, "version": run.version}
    return response(request, record.response_summary)


@router.get("/agent-config", response_model=Envelope)
def agent_config(request: Request, user: Authenticated):
    from careerforge.application.job_agent_service import configuration

    return response(request, configuration())


@router.post("/extraction-runs", status_code=202, response_model=Envelope)
def extraction(
    body: ExtractionRunInput,
    request: Request,
    db: Database,
    user: Authenticated,
    key: IdempotencyKey,
):
    enabled()
    from careerforge.application.job_agent_service import start

    return response(request, start(db, user.id, body, key))
