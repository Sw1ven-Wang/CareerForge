import base64
import json
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from careerforge.api.dependencies import (
    Authenticated,
    Database,
    IdempotencyKey,
    require_origin,
)
from careerforge.application.attachment_service import available as available_attachment
from careerforge.application.common import digest
from careerforge.application.goal_service import get_goal, goal_result, save_goal
from careerforge.application.source_service import (
    create_citation,
    delete_source,
    get_snapshot,
    get_source,
    import_source,
    latest,
    new_snapshot,
)
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError, not_found
from careerforge.infrastructure.db.models import (
    CareerGoal,
    Citation,
    Deletion,
    LoginAttempt,
    Source,
    SourceSnapshot,
    User,
    UserSession,
)
from careerforge.infrastructure.db.session import get_engine
from careerforge.schemas.resources import (
    CitationInput,
    Credentials,
    Envelope,
    GoalInput,
    GoalRevision,
    SnapshotInput,
    SourceImport,
    VersionInput,
)

app = FastAPI(title="CareerForge", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Idempotency-Key", "X-CSRF-Token"],
)


def source_read_status(db: Session, source: Source) -> str:
    snapshot = latest(db, source)
    if snapshot is None:
        return "unread"
    if snapshot.attachment_id:
        try:
            available_attachment(db, source.owner_id, snapshot.attachment_id)
        except BusinessError as error:
            if error.status == 404:
                return "revoked"
            raise
    return "available"


hasher = PasswordHasher()
dummy_hash = hasher.hash("dummy-auth-timing-password")


@app.middleware("http")
async def request_identity(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request.state.request_id = str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def envelope(request: Request, data: Any, next_cursor: str | None = None) -> dict[str, Any]:
    result = {"data": data, "request_id": request.state.request_id}
    if isinstance(data, list):
        result["next_cursor"] = next_cursor
    return result


@app.exception_handler(BusinessError)
async def business_error(request: Request, error: BusinessError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": False,
                "details": error.details,
            },
            "request_id": request.state.request_id,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    # Omit input/ctx: they may contain passwords, private documents or model output.
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "INVALID_REQUEST",
                "message": "请求字段不合法。",
                "retryable": False,
                "details": {
                    "fields": [
                        {"location": list(item["loc"]), "type": item["type"]}
                        for item in error.errors()
                    ]
                },
            },
            "request_id": request.state.request_id,
        },
    )


@app.exception_handler(SQLAlchemyError)
async def database_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "DEPENDENCY_UNAVAILABLE",
                "message": "数据服务暂不可用。",
                "retryable": True,
                "details": {},
            },
            "request_id": request.state.request_id,
        },
    )


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
def ready(db: Database) -> dict[str, str]:
    db.execute(select(1))
    # A connection alone is insufficient before migrations have created identity tables.
    db.execute(select(User.id).limit(1))
    return {"status": "ready"}


@app.post(
    "/api/v1/auth/register",
    status_code=201,
    response_model=Envelope,
    response_model_exclude_none=True,
)
def register(request: Request, value: Credentials, db: Database) -> dict[str, Any]:
    require_origin(request)
    user = User(email=value.email, password_hash=hasher.hash(value.password))
    try:
        with db.begin_nested():
            db.add(user)
            db.flush()
    except IntegrityError:
        raise BusinessError(409, "REGISTRATION_REJECTED", "无法创建该账号。") from None
    return envelope(request, {"user_id": str(user.id), "email": user.email})


@app.post("/api/v1/auth/login", response_model=Envelope, response_model_exclude_none=True)
def login(request: Request, response: Response, value: Credentials, db: Database) -> dict[str, Any]:
    require_origin(request)
    identity = digest(value.email)
    # Rate-limit receipts survive rejected login transactions; passwords are never persisted.
    with Session(get_engine()) as rate_db, rate_db.begin():
        rate_db.execute(select(func.pg_advisory_xact_lock(int(identity[:15], 16))))
        count = (
            rate_db.scalar(
                select(func.count())
                .select_from(LoginAttempt)
                .where(
                    LoginAttempt.identity_hash == identity,
                    LoginAttempt.created_at > func.now() - timedelta(minutes=15),
                )
            )
            or 0
        )
        if count >= 10:
            raise BusinessError(429, "LOGIN_RATE_LIMITED", "登录过于频繁，请稍后重试。")
        rate_db.add(LoginAttempt(identity_hash=identity))
    user = db.scalars(select(User).where(User.email == value.email)).first()
    valid: bool
    try:
        valid = hasher.verify(user.password_hash if user else dummy_hash, value.password)
    except (VerificationError, InvalidHashError):
        valid = False
    if not user or not valid:
        raise BusinessError(401, "LOGIN_REJECTED", "邮箱或密码不正确。")
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = db.scalar(select(func.now()))
    assert now is not None
    session = UserSession(
        token_hash=digest(token),
        owner_id=user.id,
        csrf_token=csrf,
        expires_at=now + timedelta(hours=get_settings().session_hours),
    )
    db.add(session)
    response.set_cookie(
        "careerforge_session",
        token,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="strict",
        max_age=get_settings().session_hours * 3600,
        path="/",
    )
    return envelope(request, {"user_id": str(user.id), "email": user.email, "auth_mode": "session"})


@app.get("/api/v1/auth/me", response_model=Envelope, response_model_exclude_none=True)
def me(request: Request, user: Authenticated, db: Database) -> dict[str, Any]:
    value = db.get(User, user.id)
    assert value is not None
    return envelope(
        request, {"user_id": str(user.id), "email": value.email, "auth_mode": "session"}
    )


@app.get("/api/v1/auth/csrf", response_model=Envelope, response_model_exclude_none=True)
def csrf(request: Request, user: Authenticated) -> dict[str, Any]:
    return envelope(request, {"csrf_token": user.session.csrf_token})


@app.post("/api/v1/auth/logout", response_model=Envelope, response_model_exclude_none=True)
def logout(
    request: Request, response: Response, user: Authenticated, db: Database
) -> dict[str, Any]:
    user.session.revoked_at = db.scalar(select(func.now()))
    response.delete_cookie("careerforge_session", path="/")
    return envelope(request, {"logged_out": True})


def paginate(
    query: Any, model: Any, db: Session, cursor: str | None, limit: int
) -> tuple[list[Any], str | None]:
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            timestamp, identifier = datetime.fromisoformat(value[0]), UUID(value[1])
            if timestamp.tzinfo is None:
                raise ValueError
        except (ValueError, TypeError, IndexError, KeyError):
            raise BusinessError(400, "INVALID_CURSOR", "列表游标无效。") from None
        query = query.where(tuple_(model.created_at, model.id) > (timestamp, identifier))
    rows = list(db.scalars(query.order_by(model.created_at, model.id).limit(limit + 1)))
    following = None
    if len(rows) > limit:
        last = rows[limit - 1]
        following = base64.urlsafe_b64encode(
            json.dumps([last.created_at.isoformat(), str(last.id)]).encode()
        ).decode()
    return rows[:limit], following


@app.post(
    "/api/v1/career-goals",
    status_code=201,
    response_model=Envelope,
    response_model_exclude_none=True,
)
def create_goal(
    request: Request,
    value: GoalInput,
    user: Authenticated,
    db: Database,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return envelope(request, save_goal(db, user.id, value, idempotency_key))


@app.post(
    "/api/v1/career-goals/{goal_id}/versions",
    status_code=201,
    response_model=Envelope,
    response_model_exclude_none=True,
)
def revise_goal(
    goal_id: UUID,
    request: Request,
    value: GoalRevision,
    user: Authenticated,
    db: Database,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return envelope(request, save_goal(db, user.id, value, idempotency_key, goal_id))


@app.get("/api/v1/career-goals", response_model=Envelope)
def goals(
    request: Request,
    user: Authenticated,
    db: Database,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    rows, following = paginate(
        select(CareerGoal).where(CareerGoal.owner_id == user.id), CareerGoal, db, cursor, limit
    )
    return envelope(request, [goal_result(db, row) for row in rows], following)


@app.get(
    "/api/v1/career-goals/{goal_id}", response_model=Envelope, response_model_exclude_none=True
)
def goal_detail(
    goal_id: UUID, request: Request, user: Authenticated, db: Database
) -> dict[str, Any]:
    return envelope(request, goal_result(db, get_goal(db, user.id, goal_id)))


@app.post(
    "/api/v1/sources/import",
    status_code=201,
    response_model=Envelope,
    response_model_exclude_none=True,
)
def source_import(
    request: Request,
    value: SourceImport,
    user: Authenticated,
    db: Database,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return envelope(request, import_source(db, user.id, value, idempotency_key))


@app.get("/api/v1/sources", response_model=Envelope)
def sources(
    request: Request,
    user: Authenticated,
    db: Database,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    source_type: str | None = None,
    read_status: str | None = None,
) -> dict[str, Any]:
    query = select(Source).where(Source.owner_id == user.id, Source.revoked_at.is_(None))
    if source_type:
        query = query.where(Source.source_type == source_type)
    has_body = (
        select(SourceSnapshot.id)
        .where(SourceSnapshot.source_id == Source.id, SourceSnapshot.owner_id == user.id)
        .exists()
    )
    if read_status == "available":
        query = query.where(has_body)
    elif read_status == "unread":
        query = query.where(~has_body)
    elif read_status is not None:
        raise BusinessError(422, "INVALID_FILTER", "可读状态筛选无效。")
    rows, following = paginate(query, Source, db, cursor, limit)
    metadata = {
        source_id: values
        for source_id, values in db.execute(
            select(SourceSnapshot.source_id, SourceSnapshot.metadata_values)
            .where(
                SourceSnapshot.owner_id == user.id,
                SourceSnapshot.source_id.in_([row.id for row in rows]),
            )
            .distinct(SourceSnapshot.source_id)
            .order_by(SourceSnapshot.source_id, SourceSnapshot.version_no.desc())
        )
    }
    return envelope(
        request,
        [
            {
                "source_id": str(row.id),
                "source_type": row.source_type,
                "source_url": row.source_url,
                "job_category": metadata.get(row.id, {}).get("job_category"),
                "source_category": metadata.get(row.id, {}).get("source_category"),
                "title": row.title,
                "version": row.revision,
                "created_at": row.created_at.isoformat(),
                "read_status": source_read_status(db, row),
            }
            for row in rows
        ],
        following,
    )


@app.get("/api/v1/sources/{source_id}", response_model=Envelope, response_model_exclude_none=True)
def source_detail(
    source_id: UUID, request: Request, user: Authenticated, db: Database
) -> dict[str, Any]:
    source = get_source(db, user.id, source_id)
    snapshots = db.scalars(
        select(SourceSnapshot)
        .where(SourceSnapshot.source_id == source.id, SourceSnapshot.owner_id == user.id)
        .order_by(SourceSnapshot.version_no.desc())
    ).all()
    return envelope(
        request,
        {
            "source_id": str(source.id),
            "source_url": source.source_url,
            "source_type": source.source_type,
            "title": source.title,
            "version": source.revision,
            "read_status": source_read_status(db, source),
            "snapshots": [
                {
                    "source_snapshot_id": str(row.id),
                    "version_no": row.version_no,
                    "content_hash": row.content_hash,
                    "metadata": row.metadata_values,
                }
                for row in snapshots
            ],
        },
    )


@app.get(
    "/api/v1/sources/{source_id}/snapshots/{snapshot_id}",
    response_model=Envelope,
    response_model_exclude_none=True,
)
def snapshot_detail(
    source_id: UUID, snapshot_id: UUID, request: Request, user: Authenticated, db: Database
) -> dict[str, Any]:
    value = get_snapshot(db, user.id, source_id, snapshot_id)
    return envelope(
        request,
        {
            "source_snapshot_id": str(value.id),
            "source_id": str(value.source_id),
            "version_no": value.version_no,
            "content": value.content,
            "content_hash": value.content_hash,
            "metadata": value.metadata_values,
            "blocks": [{"block_id": "body", "char_start": 0, "char_end": len(value.content)}],
            "offset_unit": "Unicode code point; [start,end)",
        },
    )


@app.post(
    "/api/v1/sources/{source_id}/snapshots",
    status_code=201,
    response_model=Envelope,
    response_model_exclude_none=True,
)
def revise_snapshot(
    source_id: UUID,
    request: Request,
    value: SnapshotInput,
    user: Authenticated,
    db: Database,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return envelope(request, new_snapshot(db, user.id, source_id, value, idempotency_key))


@app.delete(
    "/api/v1/sources/{source_id}",
    status_code=202,
    response_model=Envelope,
    response_model_exclude_none=True,
)
def source_delete(
    source_id: UUID,
    request: Request,
    value: VersionInput,
    user: Authenticated,
    db: Database,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return envelope(
        request, delete_source(db, user.id, source_id, value.expected_version, idempotency_key)
    )


@app.get(
    "/api/v1/deletions/{deletion_id}", response_model=Envelope, response_model_exclude_none=True
)
def deletion_detail(
    deletion_id: UUID, request: Request, user: Authenticated, db: Database
) -> dict[str, Any]:
    job: Any = db.scalars(
        select(Deletion).where(Deletion.id == deletion_id, Deletion.owner_id == user.id)
    ).first()
    if job is None:
        from careerforge.infrastructure.db.models import EvidenceDeletion

        job = db.scalar(
            select(EvidenceDeletion).where(
                EvidenceDeletion.id == deletion_id, EvidenceDeletion.owner_id == user.id
            )
        )
    if job is None:
        raise not_found()
    return envelope(request, {"deletion_id": str(job.id), "status": job.status})


@app.post(
    "/api/v1/citations", status_code=201, response_model=Envelope, response_model_exclude_none=True
)
def citation_create(
    request: Request,
    value: CitationInput,
    user: Authenticated,
    db: Database,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    return envelope(request, create_citation(db, user.id, value, idempotency_key))


@app.get(
    "/api/v1/citations/{citation_id}", response_model=Envelope, response_model_exclude_none=True
)
def citation_detail(
    citation_id: UUID, request: Request, user: Authenticated, db: Database
) -> dict[str, Any]:
    value = db.scalars(
        select(Citation).where(Citation.id == citation_id, Citation.owner_id == user.id)
    ).first()
    if value is None:
        from careerforge.application.repository_service import (
            citation_detail as code_citation_detail,
        )

        return envelope(request, code_citation_detail(db, user.id, citation_id))
    snapshot = get_snapshot(db, user.id, value.source_id, value.snapshot_id)
    return envelope(
        request,
        {
            "citation_id": str(value.id),
            "source_snapshot_id": str(snapshot.id),
            "char_start": value.char_start,
            "char_end": value.char_end,
            "text_hash": value.text_hash,
            "quote": snapshot.content[value.char_start : value.char_end],
        },
    )


from careerforge.api.routes.runs import router as runs_router  # noqa: E402

app.include_router(runs_router)

from careerforge.api.routes.attachments import router as attachments_router  # noqa: E402

app.include_router(attachments_router)

from careerforge.api.routes.market_reports import router as market_router  # noqa: E402

app.include_router(market_router)

from careerforge.api.routes.planning import router as planning_router  # noqa: E402

app.include_router(planning_router)

from careerforge.api.routes.projects import router as projects_router  # noqa: E402

app.include_router(projects_router)

from careerforge.api.routes.learning import router as learning_router  # noqa: E402

app.include_router(learning_router)

from careerforge.api.routes.job_data import router as job_data_router  # noqa: E402

app.include_router(job_data_router)
