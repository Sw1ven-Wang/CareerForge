from __future__ import annotations

import json
import re
from importlib.resources import files
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import Database
from ..errors import DomainError
from ..models import Namespace, ObjectRow, Topic
from ..models_governance import TopicGovernance
from ..util import digest, fingerprint
from .dependencies import check_closure, register, require_object
from .security import accessible_namespaces, authorization_revision, namespace_admin


def topic_object_id(key: str, version: int = 1) -> str:
    return str(uuid5(NAMESPACE_URL, f"careerforge:topic:{key}:{version}"))


def facet_object_id(key: str, version: int = 1) -> str:
    return str(uuid5(NAMESPACE_URL, f"careerforge:facets:{key}:{version}"))


def seed(db: Database, admin: str, public_namespace: str) -> None:
    raw_seed = files("careerforge").joinpath("data/topics.json").read_text(encoding="utf-8")
    data = json.loads(raw_seed)
    seed_provenance = {
        "package_version": "careerforge-taxonomy-seed-1.6.6-jd-atomic-v1",
        "license_id": "MIT",
        "source_path": "careerforge/data/topics.json",
        "source_hash": digest(raw_seed),
        "approved_by": admin,
        "approval_operation": "explicit_namespace_admin_bootstrap",
    }
    with db.transaction() as s:
        namespace_admin(s, admin, public_namespace)
        for item in data:
            row = s.scalar(select(Topic).where(Topic.key == item["key"]))
            if row:
                continue
            row = Topic(
                id=str(uuid5(NAMESPACE_URL, "careerforge:topic:" + item["key"])),
                key=item["key"],
                title=item["title"],
                aliases=item["aliases"],
                facet_schema=item["facets"],
            )
            s.add(row)
            register(
                s,
                admin,
                public_namespace,
                "topic_version",
                {**item, "seed_provenance": seed_provenance},
                topic_object_id(item["key"]),
            )
            register(
                s,
                admin,
                public_namespace,
                "facet_schema",
                item["facets"],
                facet_object_id(item["key"]),
            )


def require_topic(
    s: Session, actor: str, key: str, version: int | None = None, *, allow_retired: bool = False
) -> Topic:
    identity = s.execute(select(Topic.id, Topic.version).where(Topic.key == key)).first()
    if identity is None:
        raise DomainError("TOPIC_UNREGISTERED", "主题尚未在授权分类中发布。", 404)
    obj = require_object(
        s, actor, topic_object_id(key, version or identity.version), "topic_version"
    )
    check_closure(s, actor, [obj.id])
    # Do not load alias/definition projections before checking the frozen object's ACL.
    topic = s.get(Topic, identity.id)
    if topic is None:
        raise DomainError("TOPIC_UNREGISTERED", "主题投影不存在。", 404)
    if version is None or version == topic.version:
        aliases = [
            alias["term"] if isinstance(alias, dict) else alias
            for alias in obj.payload.get("aliases", [])
        ]
        if "title" in obj.payload and (
            topic.title != obj.payload["title"] or topic.aliases != list(dict.fromkeys(aliases))
        ):
            raise DomainError("TAXONOMY_HASH", "分类投影与已批准版本不一致。")
    governance = s.get(TopicGovernance, topic.id)
    if version is None and not allow_retired and governance and governance.status != "active":
        raise DomainError("TOPIC_RETIRED", "主题已合并或拆分；旧关联不自动重映射。", 409)
    return topic


def visible_topics(s: Session, actor: str | None) -> list[Topic]:
    result: list[Topic] = []
    namespaces = (
        [ns.id for ns in accessible_namespaces(s, actor)]
        if actor is not None
        else list(s.scalars(select(Namespace.id).where(Namespace.kind == "shared_public")))
    )
    # Governed private keys are filtered in SQL before loading any alias body.
    # Legacy unscoped rows expose only key/version to the authorization check.
    candidates = s.execute(
        select(Topic.key, Topic.version)
        .outerjoin(TopicGovernance, TopicGovernance.topic_id == Topic.id)
        .where(
            or_(TopicGovernance.namespace_id.in_(namespaces), TopicGovernance.topic_id.is_(None)),
            or_(TopicGovernance.status == "active", TopicGovernance.topic_id.is_(None)),
        )
        .order_by(Topic.key)
    )
    for key, number in candidates:
        try:
            if actor is not None:
                topic = require_topic(s, actor, key)
            else:
                obj = s.get(ObjectRow, topic_object_id(key, number))
                ns = s.get(Namespace, obj.namespace_id) if obj else None
                if (
                    not obj
                    or not ns
                    or ns.kind != "shared_public"
                    or obj.state != "active"
                    or obj.content_hash != fingerprint(obj.payload)
                ):
                    continue
                # Revoked provenance is blocked even before projection propagation.
                topic = require_topic(s, ns.owner_id, key)
        except DomainError:
            continue
        result.append(topic)
    return result


def schema_view(
    s: Session, actor: str, topic_key: str, version: int | None = None
) -> dict[str, Any]:
    topic = require_topic(s, actor, topic_key, version, allow_retired=version is not None)
    number = topic.version if version is None else version
    obj = require_object(s, actor, facet_object_id(topic_key, number), "facet_schema")
    check_closure(s, actor, [obj.id])
    return {
        "topic_key": topic_key,
        "version": number,
        "facets": obj.payload,
        "namespace_id": obj.namespace_id,
        "topic_version_id": topic_object_id(topic_key, number),
        "facet_schema_id": obj.id,
    }


def topic_terms(s: Session, actor: str | None, keys: list[str]) -> list[str]:
    return list(
        dict.fromkeys(
            term
            for topic in visible_topics(s, actor)
            if topic.key in keys
            for term in [topic.title, *topic.aliases]
        )
    )


def taxonomy_revision(s: Session, actor: str) -> str:
    return fingerprint(authorization_revision(s, actor))


def term_matches(text: str, term: str) -> bool:
    """Match a term, not an English identifier fragment.

    CJK aliases can be embedded in ordinary Chinese text; ASCII terms must
    respect identifier boundaries (cache is not TTLCache, queue is not queued).
    This is a lexical hint, never a grant of topic or source scope.
    """
    if not term:
        return False
    left = r"(?<![a-zA-Z0-9_])" if term[0].isascii() and term[0].isalnum() else ""
    right = r"(?![a-zA-Z0-9_])" if term[-1].isascii() and term[-1].isalnum() else ""
    return re.search(left + re.escape(term) + right, text, re.IGNORECASE) is not None


def resolve_topics(db: Database, text: str, actor: str | None = None) -> list[str]:
    matches = []
    with db.read() as s:
        for topic in visible_topics(s, actor):
            # Do not map Go as a substring of arbitrary English prose.
            aliases = [
                a for a in topic.aliases if len(a) >= 3 or any("\u4e00" <= c <= "\u9fff" for c in a)
            ]
            if any(term_matches(text, alias) for alias in aliases) or term_matches(text, topic.key):
                matches.append(topic.key)
    return matches


def expand_query(
    db: Database, text: str, topic_keys: list[str], actor: str | None = None
) -> list[str]:
    values = [text]
    with db.read() as s:
        for topic in visible_topics(s, actor):
            if topic.key not in topic_keys:
                continue
            values += [topic.title, *topic.aliases]
    return list(dict.fromkeys(values))[:30]
