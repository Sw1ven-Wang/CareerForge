from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import locked
from ..errors import DomainError
from ..models import AccessGrant, Namespace, Principal
from ..util import digest


def create_principal(
    db: Session, name: str, admin: bool = False
) -> tuple[Principal, Namespace, str]:
    token = secrets.token_urlsafe(48)
    principal = Principal(name=name, token_hash=digest(token), is_admin=admin)
    db.add(principal)
    db.flush()
    from ..models_privacy_ext import PrivacyState

    db.add(PrivacyState(principal_id=principal.id, generation=0, blocked=False))
    db.flush()
    ns = Namespace(kind="private", name=name + " 私有知识", owner_id=principal.id)
    db.add(ns)
    db.flush()
    return principal, ns, token


def authenticate(db: Session, token: str) -> Principal:
    row = db.scalar(
        select(Principal).where(Principal.token_hash == digest(token), Principal.active.is_(True))
    )
    if row is None:
        raise DomainError("UNAUTHENTICATED", "登录令牌无效。", 401)
    return row


def principal(db: Session, identifier: str) -> Principal:
    row = db.get(Principal, identifier)
    if row is None or not row.active:
        raise DomainError("UNAUTHENTICATED", "身份不可用。", 401)
    return row


def namespace_access(db: Session, actor: str, ns_id: str, write: bool = False) -> Namespace:
    user = principal(db, actor)
    from .privacy import assert_personal_access

    ns = db.get(Namespace, ns_id)
    if ns is None:
        raise DomainError("NOT_FOUND", "资料不可访问。", 404)
    if ns.kind != "shared_public":
        assert_personal_access(db, actor)
        if ns.owner_id != actor:
            assert_personal_access(db, ns.owner_id)
    if ns.owner_id == user.id:
        return ns
    grant = db.scalar(
        select(AccessGrant).where(
            AccessGrant.namespace_id == ns.id,
            AccessGrant.principal_id == actor,
            AccessGrant.active.is_(True),
        )
    )
    if grant and (not write or grant.role in {"writer", "admin"}):
        return ns
    if ns.kind == "shared_public" and not write:
        return ns
    raise DomainError("NOT_FOUND", "资料不可访问。", 404)


def namespace_admin(db: Session, actor: str, ns_id: str) -> Namespace:
    ns = namespace_access(db, actor, ns_id, True)
    if ns.owner_id == actor:
        return ns
    grant = db.scalar(
        select(AccessGrant).where(
            AccessGrant.namespace_id == ns_id,
            AccessGrant.principal_id == actor,
            AccessGrant.active.is_(True),
            AccessGrant.role == "admin",
        )
    )
    if grant is None:
        raise DomainError("FORBIDDEN", "需要该知识域管理员权限。", 403)
    return ns


def accessible_namespaces(db: Session, actor: str) -> list[Namespace]:
    principal(db, actor)
    from .privacy import assert_personal_access

    assert_personal_access(db, actor)
    grants = set(
        db.scalars(
            select(AccessGrant.namespace_id).where(
                AccessGrant.principal_id == actor, AccessGrant.active.is_(True)
            )
        )
    )
    result: list[Namespace] = []
    for ns in db.scalars(select(Namespace)):
        if ns.owner_id != actor and ns.kind != "shared_public" and ns.id not in grants:
            continue
        try:
            result.append(namespace_access(db, actor, ns.id))
        except DomainError as exc:
            if exc.code == "PRIVACY_ERASURE_PENDING" and ns.owner_id != actor:
                continue
            raise
    return result


def private_namespace(db: Session, actor: str) -> Namespace:
    principal(db, actor)
    from .privacy import assert_personal_access

    assert_personal_access(db, actor)
    ns = db.scalar(
        select(Namespace).where(Namespace.owner_id == actor, Namespace.kind == "private")
    )
    if ns is None:
        raise DomainError("NOT_FOUND", "缺少个人知识域。", 404)
    return ns


def authorization_revision(db: Session, actor: str) -> dict[str, Any]:
    user = principal(db, actor)
    return {
        "principal": user.id,
        "auth_revision": user.auth_revision,
        "namespaces": {ns.id: ns.revision for ns in accessible_namespaces(db, actor)},
    }


def set_grant(db: Session, actor: str, ns_id: str, recipient: str, role: str, active: bool) -> None:
    namespace_admin(db, actor, ns_id)
    if role not in {"reader", "writer", "admin"}:
        raise DomainError("ROLE", "未知授权角色。", 422)
    principal(db, recipient)
    ns = locked(db, Namespace, ns_id)
    row = db.scalar(
        select(AccessGrant)
        .where(AccessGrant.namespace_id == ns_id, AccessGrant.principal_id == recipient)
        .with_for_update()
    )
    if row:
        row.role, row.active, row.revision = role, active, row.revision + 1
    else:
        db.add(AccessGrant(namespace_id=ns_id, principal_id=recipient, role=role, active=active))
    ns.revision += 1
    locked(db, Principal, recipient).auth_revision += 1
