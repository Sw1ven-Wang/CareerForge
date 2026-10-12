import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from careerforge.application.common import digest
from careerforge.config.settings import get_settings
from careerforge.domain.errors import BusinessError
from careerforge.infrastructure.db.models import UserSession
from careerforge.infrastructure.db.session import get_db

Database = Annotated[Session, Depends(get_db, scope="function")]


@dataclass(frozen=True)
class CurrentUser:
    id: UUID
    session: UserSession


def require_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin not in get_settings().allowed_origins:
        raise BusinessError(403, "ORIGIN_REJECTED", "请求来源不受信任。")


def get_current_user(request: Request, db: Database) -> CurrentUser:
    token = request.cookies.get("careerforge_session")
    if not token:
        raise BusinessError(401, "UNAUTHENTICATED", "请先登录。")
    session = db.scalars(
        select(UserSession).where(
            UserSession.token_hash == digest(token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > func.now(),
        )
    ).first()
    if session is None:
        raise BusinessError(401, "UNAUTHENTICATED", "会话已失效，请重新登录。")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        require_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(csrf, session.csrf_token):
            raise BusinessError(403, "CSRF_REJECTED", "请求校验失败，请刷新登录状态。")
    return CurrentUser(session.owner_id, session)


Authenticated = Annotated[CurrentUser, Depends(get_current_user)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
