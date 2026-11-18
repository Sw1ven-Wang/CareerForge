"""Explicit, versioned Topic/Facet governance with immutable historical references."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..contracts_governance import TaxonomyDecisionInput, TaxonomyProposalInput
from ..db import locked
from ..errors import DomainError
from ..models import Namespace, ObjectRow, Outbox, Topic
from ..models_governance import (
    TaxonomyHead,
    TaxonomyProposal,
    TaxonomyRelease,
    TopicGovernance,
    TopicRelation,
)
from ..util import fingerprint, uid
from .dependencies import check_closure, depend, register, require_object
from .security import namespace_access, namespace_admin
from .taxonomy import facet_object_id, require_topic, schema_view, topic_object_id


class TaxonomyGovernanceService:
    def __init__(self, application):
        self.app, self.db = application, application.db

    def _evidence(
        self, s: Session, actor: str, namespace_id: str, ids: list[str]
    ) -> dict[str, Any]:
        target = namespace_access(s, actor, namespace_id, True)
        check_closure(s, actor, ids)
        refs: dict[str, Any] = {}
        for evidence_id in dict.fromkeys(ids):
            obj = require_object(s, actor, evidence_id, "evidence")
            self.app.knowledge.evidence_dict(s, actor, evidence_id)
            source_ns = namespace_access(s, actor, obj.namespace_id)
            if target.kind == "shared_public" and source_ns.kind != "shared_public":
                raise DomainError("PRIVATE_TO_PUBLIC", "私人出处不得进入公共分类。", 403)
            refs[evidence_id] = {
                "hash": obj.content_hash,
                "revision": obj.revision,
                "namespace_id": obj.namespace_id,
            }
        return refs

    @staticmethod
    def _key(namespace: Namespace, key: str) -> str:
        if namespace.kind == "shared_public":
            return key
        prefix = "ns." + namespace.id.replace("-", "") + "."
        result = key if key.startswith(prefix) else prefix + key
        if len(result) > 120:
            raise DomainError("TOPIC_KEY", "私有主题短键过长。", 422)
        return result

    def propose(self, actor: str, body: TaxonomyProposalInput) -> dict[str, Any]:
        request_hash = fingerprint(body.model_dump(mode="json"))
        with self.db.transaction() as s:
            ns = namespace_access(s, actor, body.namespace_id, True)
            locked(s, Namespace, ns.id)
            old = s.scalar(
                select(TaxonomyProposal).where(
                    TaxonomyProposal.namespace_id == ns.id,
                    TaxonomyProposal.proposer_id == actor,
                    TaxonomyProposal.idempotency_key == body.idempotency_key,
                )
            )
            if old:
                if old.payload.get("request_hash") != request_hash:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "同一工作单键不能替换内容。")
                return self._proposal_dict(old)
            evidence = self._evidence(s, actor, ns.id, body.evidence_ids)
            payload = body.model_dump(mode="json")
            definitions = payload["definitions"]
            key_map = {d["key"]: self._key(ns, d["key"]) for d in definitions}
            for definition in definitions:
                definition["key"] = key_map[definition["key"]]
                for relation in definition["relations"]:
                    relation["target_key"] = key_map.get(
                        relation["target_key"], relation["target_key"]
                    )
            if len({d["key"] for d in definitions}) != len(definitions):
                raise DomainError("TOPIC_KEY", "主题键重复。", 422)
            for key, number in body.source_topic_versions.items():
                topic = require_topic(s, actor, key)
                obj = require_object(s, actor, topic_object_id(key, topic.version))
                if obj.namespace_id != ns.id or topic.version != number:
                    raise DomainError("TAXONOMY_CONFLICT", "主题范围或基线版本已改变。")
            keys = [d["key"] for d in definitions]
            if body.operation in {"update", "schema"}:
                if keys != list(body.source_topic_versions):
                    raise DomainError("TOPIC_IDENTITY", "修订不能改写稳定主题键。", 422)
            elif any(s.scalar(select(Topic.id).where(Topic.key == key)) for key in keys):
                raise DomainError("TOPIC_EXISTS", "新身份键已经存在；合并拆分不能覆盖旧主题。")
            payload.update(evidence_bindings=evidence, request_hash=request_hash)
            row = TaxonomyProposal(
                namespace_id=ns.id,
                proposer_id=actor,
                idempotency_key=body.idempotency_key,
                payload=payload,
                content_hash=fingerprint(payload),
            )
            s.add(row)
            s.flush()
            return self._proposal_dict(row)

    @staticmethod
    def _proposal_dict(row: TaxonomyProposal) -> dict[str, Any]:
        return {
            "proposal_id": row.id,
            "namespace_id": row.namespace_id,
            "status": row.status,
            "provisional": row.status == "proposed",
            "revision": row.revision,
            "content_hash": row.content_hash,
            "payload": row.payload,
            "reviewer_id": row.reviewer_id,
            "reason": row.decision_reason,
            "release_id": row.release_id,
        }

    def detail(self, actor: str, proposal_id: str) -> dict[str, Any]:
        with self.db.read() as s:
            row = s.get(TaxonomyProposal, proposal_id)
            if not row:
                raise DomainError("NOT_FOUND", "分类工作单不存在。", 404)
            namespace_access(s, actor, row.namespace_id)
            if row.content_hash != fingerprint(row.payload):
                raise DomainError("TAXONOMY_HASH", "工作单内容已改变。")
            return self._proposal_dict(row)

    def status(self, actor: str, namespace_id: str) -> dict[str, Any]:
        with self.db.read() as s:
            namespace_access(s, actor, namespace_id)
            head = s.get(TaxonomyHead, namespace_id)
            return {
                "namespace_id": namespace_id,
                "revision": head.revision if head else 0,
                "release_id": head.release_id if head else None,
            }

    def _relations(
        self,
        s: Session,
        actor: str,
        ns: Namespace,
        definitions: list[dict[str, Any]],
        identities: dict[str, Topic],
    ) -> dict[str, list[tuple[str, str]]]:
        # Namespace locks alone cannot serialize a cycle crossing two public
        # namespaces. Serialize graph checks with all graph writers on PostgreSQL;
        # SQLite already serializes these transactions with BEGIN IMMEDIATE.
        if s.get_bind().dialect.name == "postgresql":
            lock_id = int(fingerprint({"taxonomy_graph": "1.6.0"})[:16], 16) - 2**63
            s.execute(select(func.pg_advisory_xact_lock(lock_id)))
        planned: dict[str, list[tuple[str, str]]] = {}
        for definition in definitions:
            topic = identities[definition["key"]]
            refs: list[tuple[str, str]] = []
            for relation in definition["relations"]:
                key = relation["target_key"]
                target = identities.get(key) or require_topic(s, actor, key)
                if target.id == topic.id:
                    raise DomainError("TOPIC_CYCLE", "主题关系不得指向自身。", 422)
                if target.id not in {t.id for t in identities.values()}:
                    target_obj = require_object(
                        s, actor, topic_object_id(target.key, target.version)
                    )
                    target_ns = namespace_access(s, actor, target_obj.namespace_id)
                    if ns.kind == "shared_public" and target_ns.kind != "shared_public":
                        raise DomainError(
                            "PRIVATE_TO_PUBLIC", "公共分类关系不得泄露私有主题。", 403
                        )
                    if target_ns.kind != "shared_public" and target_ns.id != ns.id:
                        raise DomainError(
                            "TOPIC_SCOPE", "跨域关系只能引用公共主题，不能转授其他私有分类。", 403
                        )
                refs.append((target.id, relation["relation"]))
            planned[topic.id] = list(dict.fromkeys(refs))
        for kind in ("prerequisite", "broader"):
            graph: dict[str, set[str]] = {}
            for edge in s.scalars(
                select(TopicRelation)
                .join(Topic, Topic.id == TopicRelation.topic_id)
                .where(TopicRelation.version == Topic.version, TopicRelation.relation == kind)
            ):
                graph.setdefault(edge.topic_id, set()).add(edge.target_topic_id)
            for identity, refs in planned.items():
                graph[identity] = {target for target, relation in refs if relation == kind}
            colors: dict[str, int] = {}
            for identity in planned:
                stack = [(identity, False)]
                while stack:
                    node, exiting = stack.pop()
                    if exiting:
                        colors[node] = 2
                        continue
                    if colors.get(node) == 1:
                        raise DomainError("TOPIC_CYCLE", "先修或上下位关系不能形成环。", 422)
                    if colors.get(node) == 2:
                        continue
                    colors[node] = 1
                    if len(colors) > 10000:
                        raise DomainError("TAXONOMY_LIMIT", "关系闭包超过本次治理上限。")
                    stack.append((node, True))
                    stack.extend((target, False) for target in graph.get(node, set()))
        return planned

    def decide(self, actor: str, proposal_id: str, body: TaxonomyDecisionInput) -> dict[str, Any]:
        with self.db.transaction() as s:
            proposal = s.get(TaxonomyProposal, proposal_id)
            if proposal is None:
                raise DomainError("NOT_FOUND", "分类工作单不存在。", 404)
            ns = namespace_admin(s, actor, proposal.namespace_id)
            locked(s, Namespace, ns.id)
            proposal = locked(s, TaxonomyProposal, proposal_id)
            if proposal.status != "proposed" or proposal.revision != body.expected_revision:
                raise DomainError("TAXONOMY_CONFLICT", "工作单已经裁决或版本变化。")
            if proposal.content_hash != fingerprint(proposal.payload):
                raise DomainError("TAXONOMY_HASH", "工作单内容与提议时不同。")
            head = s.get(TaxonomyHead, ns.id)
            if (head.revision if head else 0) != body.expected_release_revision:
                raise DomainError("TAXONOMY_CONFLICT", "分类发布集合已经变化。")
            payload = proposal.payload
            proposal.reviewer_id, proposal.decision_reason = actor, body.reason
            if not body.approve:
                proposal.status = "rejected"
                proposal.revision += 1
                return self._proposal_dict(proposal)
            if payload["operation"] == "merge" and not body.equivalence_confirmed:
                raise DomainError(
                    "TOPIC_EQUIVALENCE", "合并需要明确审核语义等价，相关不等于同义。", 422
                )
            evidence = self._evidence(s, actor, ns.id, payload["evidence_ids"])
            if evidence != payload["evidence_bindings"]:
                raise DomainError("TAXONOMY_EVIDENCE_CHANGED", "出处版本已变化，须重新提议。")
            for key, number in payload["source_topic_versions"].items():
                topic = require_topic(s, actor, key)
                if topic.version != number:
                    raise DomainError("TAXONOMY_CONFLICT", "主题版本已改变。")
            identities: dict[str, Topic] = {}
            for definition in payload["definitions"]:
                key = definition["key"]
                topic = s.scalar(select(Topic).where(Topic.key == key))
                if payload["operation"] in {"update", "schema"}:
                    if topic is None or key not in payload["source_topic_versions"]:
                        raise DomainError("TOPIC_IDENTITY", "修订目标身份不一致。")
                elif topic is not None:
                    raise DomainError("TOPIC_EXISTS", "新身份键已被其他工作单使用。")
                if topic is None:
                    topic = Topic(
                        id=uid(),
                        key=key,
                        title=definition["title"],
                        aliases=[],
                        facet_schema={},
                        version=1,
                    )
                    s.add(topic)
                    s.flush()
                identities[key] = topic
            relations = self._relations(s, actor, ns, payload["definitions"], identities)
            release_id = uid()
            # Insert release before scoped topic rows; complete the immutable manifest below.
            release = TaxonomyRelease(
                id=release_id,
                namespace_id=ns.id,
                revision=body.expected_release_revision + 1,
                proposal_id=proposal.id,
                reviewer_id=actor,
                manifest={},
                content_hash="",
            )
            s.add(release)
            s.flush()
            changed = []
            for definition in payload["definitions"]:
                topic = identities[definition["key"]]
                if payload["operation"] in {"update", "schema"}:
                    topic.version += 1
                topic.title = definition["title"]
                topic.aliases = list(dict.fromkeys(a["term"] for a in definition["aliases"]))
                topic.facet_schema = definition["facets"]
                frozen = {
                    **definition,
                    "version": topic.version,
                    "release_id": release.id,
                    "origin_evidence_ids": list(evidence),
                    "proposal_id": proposal.id,
                }
                tv = register(
                    s,
                    actor,
                    ns.id,
                    "topic_version",
                    frozen,
                    topic_object_id(topic.key, topic.version),
                )
                fs = register(
                    s,
                    actor,
                    ns.id,
                    "facet_schema",
                    definition["facets"],
                    facet_object_id(topic.key, topic.version),
                )
                for evidence_id in evidence:
                    depend(s, actor, tv.id, evidence_id, "grounded_in")
                    depend(s, actor, fs.id, evidence_id, "grounded_in")
                scope = s.get(TopicGovernance, topic.id)
                if scope:
                    scope.release_id = release.id
                else:
                    s.add(
                        TopicGovernance(
                            topic_id=topic.id, namespace_id=ns.id, release_id=release.id
                        )
                    )
                for target, kind in relations[topic.id]:
                    s.add(
                        TopicRelation(
                            namespace_id=ns.id,
                            topic_id=topic.id,
                            version=topic.version,
                            target_topic_id=target,
                            relation=kind,
                        )
                    )
                changed.append(
                    {
                        "topic_id": topic.id,
                        "key": topic.key,
                        "version": topic.version,
                        "topic_version_id": tv.id,
                        "facet_schema_id": fs.id,
                    }
                )
            replacements = [d["key"] for d in payload["definitions"]]
            if payload["operation"] in {"merge", "split"}:
                for key in payload["source_topic_versions"]:
                    topic = require_topic(s, actor, key)
                    scope = s.get(TopicGovernance, topic.id)
                    if scope:
                        scope.status, scope.replacements, scope.release_id = (
                            "retired",
                            replacements,
                            release.id,
                        )
                    else:
                        s.add(
                            TopicGovernance(
                                topic_id=topic.id,
                                namespace_id=ns.id,
                                release_id=release.id,
                                status="retired",
                                replacements=replacements,
                            )
                        )
            s.flush()
            snapshot = []
            for topic in s.scalars(select(Topic).order_by(Topic.key)):
                obj = s.get(ObjectRow, topic_object_id(topic.key, topic.version))
                if obj is None or obj.namespace_id != ns.id:
                    continue
                scope = s.get(TopicGovernance, topic.id)
                snapshot.append(
                    {
                        "topic_id": topic.id,
                        "key": topic.key,
                        "version": topic.version,
                        "status": scope.status if scope else "active",
                        "replacements": scope.replacements if scope else [],
                    }
                )
            release.manifest = {
                "topics": snapshot,
                "changed": changed,
                "operation": payload["operation"],
                "predecessors": payload["source_topic_versions"],
                "historical_links_rewritten": False,
                "reason": body.reason,
                "equivalence_confirmed": body.equivalence_confirmed,
                "evidence_bindings": evidence,
            }
            release.content_hash = fingerprint(release.manifest)
            if head is None:
                head = TaxonomyHead(
                    namespace_id=ns.id, revision=release.revision, release_id=release.id
                )
                s.add(head)
            else:
                head.revision, head.release_id = release.revision, release.id
            proposal.status, proposal.release_id = "approved", release.id
            proposal.revision += 1
            ns.revision += 1
            s.add(
                Outbox(
                    kind="taxonomy.changed",
                    aggregate_id=release.id,
                    payload={"namespace_id": ns.id},
                )
            )
            return self._proposal_dict(proposal)

    def release(self, actor: str, release_id: str) -> dict[str, Any]:
        with self.db.read() as s:
            row = s.get(TaxonomyRelease, release_id)
            if row is None:
                raise DomainError("NOT_FOUND", "分类发布不存在。", 404)
            namespace_access(s, actor, row.namespace_id)
            if row.content_hash != fingerprint(row.manifest):
                raise DomainError("TAXONOMY_HASH", "历史分类发布完整性错误。")
            return {
                "release_id": row.id,
                "namespace_id": row.namespace_id,
                "revision": row.revision,
                "manifest": row.manifest,
                "content_hash": row.content_hash,
            }

    def map_facets(
        self, actor: str, publication_id: str, topic_key: str, schema_version: int | None = None
    ) -> dict[str, Any]:
        with self.db.read() as s:
            pack = self.app.knowledge.publication_pack(s, actor, publication_id)
            target = schema_view(s, actor, topic_key, schema_version)
            values = [f for f in pack["facets"] if f["topic_key"] == topic_key]
            cells = []
            for key, definition in target["facets"].items():
                candidates = [f for f in values if f["facet_key"] == key]
                compatible = [
                    f
                    for f in candidates
                    if schema_view(s, actor, topic_key, f["schema_version"])["facets"].get(key)
                    == definition
                ]
                if len(compatible) == 1:
                    f = compatible[0]
                    cells.append(
                        {
                            **f,
                            "target_schema_version": target["version"],
                            "mapping_status": "exact"
                            if f["schema_version"] == target["version"]
                            else "compatible_definition",
                            "original_schema_preserved": True,
                        }
                    )
                else:
                    cells.append(
                        {
                            "topic_key": topic_key,
                            "facet_key": key,
                            "target_schema_version": target["version"],
                            "value_status": "unmapped_legacy" if values else "unknown",
                            "value": None,
                            "claim_ids": [],
                            "mapping_status": "fresh_analysis_review_required",
                        }
                    )
            return {
                "publication_id": publication_id,
                "schema": target,
                "cells": cells,
                "knowledge_rewritten": False,
                "model_calls": 0,
            }
