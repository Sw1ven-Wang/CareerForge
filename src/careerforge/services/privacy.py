"""Personal-data governance and generation fencing, not a legal compliance claim."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from ..db import aware
from ..errors import DomainError
from ..models_privacy_ext import PrivacyState


def assert_personal_access(
    db: Session, actor: str, *, expected_generation: int | None = None
) -> int:
    """Hold a shared PG row lock through the caller's read/write transaction.

    Deletion's exclusive fence transaction touches ONLY this row and commits before
    taking run/funding locks. Two cross-owner readers therefore cannot deadlock each
    other, and deletion cannot commit until an already-authorized transaction ends.
    Missing migration/bootstrap state is deliberately not silently accepted.
    """
    state = db.scalar(
        select(PrivacyState)
        .where(PrivacyState.principal_id == actor)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if state is None:
        raise DomainError(
            "PRIVACY_STATE_MISSING", "个人数据控制状态缺失，请先完成数据库迁移。", 503
        )
    if state.blocked:
        raise DomainError(
            "PRIVACY_ERASURE_PENDING", "个人资料正在清理，暂停读取、写入及外发。", 409
        )
    if expected_generation is not None and state.generation != expected_generation:
        raise DomainError("PRIVACY_GENERATION_CHANGED", "个人数据版本已变更，拒绝恢复旧资料。", 409)
    return state.generation


# Fixed ownership links, not a recursive "everything referencing the principal" crawl.
# The latter would erase another person's work or shared system publications.
_CHILD_FIELDS = {
    "run_id",
    "conversation_id",
    "turn_id",
    "workspace_id",
    "execution_snapshot_id",
    "execution_id",
    "workflow_id",
    "action_receipt_id",
    "goal_id",
    "plan_id",
    "unit_id",
    "unit_version_id",
    "source_id",
    "node_id",
    "candidate_id",
    "dataset_id",
    "feedback_id",
    "rollout_id",
    "dispute_id",
    "batch_id",
    "attempt_id",
    "activity_id",
    "schedule_id",
    "plan_version_id",
    "publication_id",
}
_CONTROL_TABLES = {"privacy_states", "privacy_deletions", "privacy_retention_holds"}
_PRINCIPAL_TABLES = {
    "source_reads",
    "idempotency_records",
    "feedback_events",
    "disputes",
    "command_authorizations",
    "teaching_observations",
    "submissions",
    "plan_proposal_manifests",
    "learning_activities",
    "learning_attempts",
    "learning_observation_records",
    "learning_adjustments",
    "evaluation_dataset_attestations",
}
# Structural/audit values do not contain arbitrary source or model text. All JSON/body
# columns are scrubbed separately, including fields not yet present in older schemas.
_SAFE_TEXT = {
    "kind",
    "state",
    "status",
    "workflow",
    "workflow_engine",
    "learning_graph_version",
    "orchestration_engine",
    "workflow_type",
    "graph_version",
    "milestone",
    "contract_version",
    "role",
    "target",
    "origin_kind",
    "origin_contract",
    "session_graph_version",
    "unit_type",
    "relation",
    "enforcement",
    "policy_version",
    "decision",
    "signal",
    "origin",
    "assistance_mode",
    "help_mode",
    "evaluator",
    "evaluator_kind",
    "hypothesis_status",
    "assessment",
    "strategy",
    "split",
    "learning_intent",
    "proposed_route",
    "outcome",
    "event_type",
    "arm",
    "environment_version",
    "image_digest",
    "license_id",
    "model_version",
    "profile_version",
    "credential_profile",
    "authorization_ref",
    "scope",
}
_SECRET_COLUMNS = {"token_hash", "nonce_hash", "authorization_digest"}


def retention_policy() -> dict:
    return {
        "version": "personal-data-1.6",
        "scope": "all_personal_content_at_confirmation",
        "erased": [
            "private_sources",
            "conversations_and_memory",
            "learning_and_feedback",
            "coding_files_and_receipts",
            "model_response_bodies",
            "derived_indexes_and_caches",
            "private_legacy_imports",
            "provably_derived_cross_owner_renderings",
        ],
        "retained": [
            "active_account_authentication",
            "pseudonymous_accounting_counters",
            "request_and_authorization_hashes_and_scopes",
            "revoked_dependency_tombstones",
        ],
        "retention_basis": "operator_policy_not_a_statement_of_legal_obligation",
        "minimal_accounting_retention": "while_account_exists; separate_account_closure_required",
        "deletion_audit_days": 365,
        "hold_behavior": "active_explicit_hold_prevents_erasure_and_is_disclosed",
        "excluded": [
            "other_users_independent_records",
            "shared_public_knowledge",
            "external_provider_copies",
            "already_downloaded_exports",
            "operator_backups_pending_retention_rotation",
        ],
        "limitations": "Not a legal-compliance certification. External copies and backups are not recalled by this API.",
        "after_erasure": "new_private_namespace_and_new_budget_authorization_required",
    }


class PrivacyService:
    def __init__(self, app):
        self.app, self.db = app, app.db

    @staticmethod
    def _scope(db: Session, actor: str) -> dict:
        """Select owned rows; children follow only reviewed ownership FK directions."""
        from sqlalchemy import or_

        from ..models import AgentRun, Base, FundingAgreement, Namespace

        tables = Base.metadata.tables
        ns_ids = set(
            db.scalars(
                select(Namespace.id).where(
                    Namespace.owner_id == actor, Namespace.kind != "shared_public"
                )
            )
        )
        selected: dict[str, list[dict]] = {}
        for name, table in tables.items():
            if name in _CONTROL_TABLES or name in {"principals", "artifact_dependencies"}:
                continue
            predicate: ColumnElement[bool] | None = None
            if name == "knowledge_namespaces":
                predicate = table.c.id.in_(ns_ids)
            elif name == "agent_runs":
                # A system-funded public build belongs to the public service, not its admin.
                public_system = (
                    select(AgentRun.id)
                    .join(Namespace, AgentRun.namespace_id == Namespace.id)
                    .join(FundingAgreement, AgentRun.funding_id == FundingAgreement.id)
                    .where(Namespace.kind == "shared_public", FundingAgreement.kind == "system")
                )
                predicate = or_(
                    table.c.principal_id == actor, table.c.namespace_id.in_(ns_ids)
                ) & table.c.id.not_in(public_system)
            elif "namespace_id" in table.c:
                predicate = table.c.namespace_id.in_(ns_ids)
            elif name in _PRINCIPAL_TABLES and "principal_id" in table.c:
                predicate = table.c.principal_id == actor
            elif name == "task_subscriptions":
                predicate = table.c.subscriber_id == actor
            if predicate is not None:
                selected[name] = [
                    dict(r) for r in db.execute(select(table).where(predicate)).mappings()
                ]
        # Projection ids inherit the namespace of their ObjectRow. Non-principal
        # child tables inherit the authoritative ownership of their parent record.
        changed = True
        while changed:
            changed = False
            for name, table in tables.items():
                if (
                    name in selected
                    or name in _CONTROL_TABLES
                    or name
                    in {
                        "principals",
                        "artifact_dependencies",
                        "outbox_messages",
                        "access_grants",
                        "technology_topics",
                        "github_rate_buckets",
                        "shared_builds",
                        "shared_build_slots",
                        "dependency_invalidation_jobs",
                        "dependency_invalidation_frontier",
                    }
                ):
                    continue
                predicates = []
                for col in table.columns:
                    if col.name not in _CHILD_FIELDS and col.name != "id":
                        continue
                    for fk in col.foreign_keys:
                        parent = fk.column.table.name
                        if parent in selected and (
                            col.name != "id" or parent == "dependency_objects"
                        ):
                            values = [r[fk.column.name] for r in selected[parent]]
                            predicates.append(col.in_(values))
                if predicates:
                    selected[name] = [
                        dict(r)
                        for r in db.execute(select(table).where(or_(*predicates))).mappings()
                    ]
                    changed = True
        # A governed private topic owns its global projection; built-in/public topics
        # have no private governance owner and must remain untouched.
        topic_ids = [r["topic_id"] for r in selected.get("topic_governance", [])]
        if topic_ids:
            topics = tables["technology_topics"]
            selected["technology_topics"] = [
                dict(r)
                for r in db.execute(select(topics).where(topics.c.id.in_(topic_ids))).mappings()
            ]
        run_ids = {r["id"] for r in selected.get("agent_runs", [])}
        execution_ids = {r["id"] for r in selected.get("workflow_executions", [])}
        outbox = tables["outbox_messages"]
        selected["outbox_messages"] = [
            dict(r)
            for r in db.execute(
                select(outbox).where(
                    outbox.c.aggregate_id.in_(run_ids)
                    | (
                        (outbox.c.kind == "workflow.execute")
                        & outbox.c.aggregate_id.in_(execution_ids)
                    )
                )
            ).mappings()
        ]
        return selected

    @staticmethod
    def _public_proposal(row) -> dict:
        return {
            "proposal_id": row.id,
            "state": row.state,
            "scope": row.scope,
            "plan_hash": row.plan_hash,
            "plan": row.plan,
            "result": {k: v for k, v in row.result.items() if k != "cache_reference_ids"},
            "expires_at": aware(row.expires_at).isoformat(),
            "completed_at": aware(row.completed_at).isoformat() if row.completed_at else None,
        }

    @staticmethod
    def _owned(db, actor, identifier):
        from ..models_privacy_ext import PrivacyDeletion
        from .security import principal

        principal(db, actor)  # Control plane remains available during blocked erasure.
        row = db.get(PrivacyDeletion, identifier)
        if row is None or row.principal_id != actor:
            raise DomainError("NOT_FOUND", "个人数据操作不存在。", 404)
        return row

    @staticmethod
    def _holds(db, actor):
        from ..db import clock
        from ..models_privacy_ext import PrivacyRetentionHold

        return db.scalars(
            select(PrivacyRetentionHold).where(
                PrivacyRetentionHold.principal_id == actor,
                PrivacyRetentionHold.active.is_(True),
                PrivacyRetentionHold.expires_at > clock(db),
            )
        ).all()

    def status(self, actor: str, identifier: str) -> dict:
        with self.db.read() as db:
            return self._public_proposal(self._owned(db, actor, identifier))

    def export(self, actor: str) -> dict:
        from datetime import datetime

        from ..models import Namespace, ObjectRow
        from ..util import fingerprint
        from .security import principal

        with self.db.read() as db:
            user = principal(db, actor)
            generation = assert_personal_access(db, actor)
            selected = self._scope(db, actor)
            foreign_private = set(
                db.scalars(
                    select(ObjectRow.id)
                    .join(Namespace, ObjectRow.namespace_id == Namespace.id)
                    .where(Namespace.kind != "shared_public", Namespace.owner_id != actor)
                )
            )

            def contains_foreign(value):
                if isinstance(value, str):
                    return value in foreign_private
                if isinstance(value, dict):
                    return any(contains_foreign(v) for v in value.values())
                if isinstance(value, list):
                    return any(contains_foreign(v) for v in value)
                return False

            def encode(value):
                if isinstance(value, datetime):
                    return aware(value).isoformat()
                if isinstance(value, bytes):
                    # HTTP response caches are derivations, not a new export entitlement.
                    return {"omitted_binary_cache": True, "byte_count": len(value)}
                if isinstance(value, dict):
                    return {k: encode(v) for k, v in value.items() if k not in _SECRET_COLUMNS}
                if isinstance(value, list):
                    return [encode(v) for v in value]
                return value

            def export_row(name, row):
                if contains_foreign(row):
                    return {"id": row.get("id"), "redacted": "references_other_private_material"}
                clean = encode(row)
                if name == "model_call_receipts":
                    clean.pop(
                        "response", None
                    )  # Unchecked provider tokens are not an answer/export surface.
                return clean

            records = {
                name: [export_row(name, row) for row in rows]
                for name, rows in selected.items()
                if rows and name not in {"outbox_messages", "task_subscriptions"}
            }
            data = {
                "format": "careerforge-personal-export-1.6",
                "principal_id": actor,
                "profile": {"name": user.name},
                "generation": generation,
                "records": records,
                "retention_policy": retention_policy(),
            }
            # Export is a response, never another retained body or server-side downloadable file.
            return {**data, "content_hash": fingerprint(data)}

    def propose(self, actor: str, scope: str = "all_personal_content") -> dict:
        import secrets
        from datetime import timedelta

        from ..db import clock
        from ..models_privacy_ext import PrivacyDeletion
        from ..util import digest, fingerprint
        from .security import principal

        if scope != "all_personal_content":
            raise DomainError("PRIVACY_SCOPE", "仅支持明确的全部个人正文范围。", 422)
        with self.db.transaction() as db:
            principal(db, actor)
            generation = assert_personal_access(db, actor)
            selected = self._scope(db, actor)
            plan = {
                "scope": scope,
                "policy": retention_policy(),
                "counts_at_proposal": {k: len(v) for k, v in selected.items() if v},
                "holds": [
                    {
                        "id": h.id,
                        "category": h.category,
                        "reason_code": h.reason_code,
                        "expires_at": aware(h.expires_at).isoformat(),
                    }
                    for h in self._holds(db, actor)
                ],
                "confirmation_covers_new_personal_content": True,
                "irreversible": True,
                "generation": generation,
            }
            token = secrets.token_urlsafe(32)
            row = PrivacyDeletion(
                principal_id=actor,
                generation=generation,
                nonce_hash=digest(token),
                plan_hash=fingerprint(plan),
                plan=plan,
                expires_at=clock(db) + timedelta(minutes=30),
                audit_retain_until=clock(db) + timedelta(days=365),
            )
            db.add(row)
            db.flush()
            return {**self._public_proposal(row), "confirmation_nonce": token}

    async def confirm(
        self,
        actor: str,
        identifier: str,
        confirmation_nonce: str,
        plan_hash: str,
        acknowledge_irreversible: bool,
    ) -> dict:
        import asyncio
        import hmac

        from ..db import aware, clock
        from ..models_privacy_ext import PrivacyDeletion
        from ..util import digest

        # Use a worker thread for blocking SQL lock waits. A Redis cache publisher may
        # briefly hold a shared fence lock across async I/O on the main event loop.
        # Blocking that loop while acquiring the erasure lock would deadlock the publisher.
        def begin() -> dict | None:
            with self.db.transaction() as db:
                proposal = self._owned(db, actor, identifier)
                if (
                    not acknowledge_irreversible
                    or not hmac.compare_digest(proposal.nonce_hash, digest(confirmation_nonce))
                    or not hmac.compare_digest(proposal.plan_hash, plan_hash)
                ):
                    raise DomainError("PRIVACY_CONFIRMATION", "删除确认凭据或提案摘要不匹配。", 403)
                state = db.scalar(
                    select(PrivacyState).where(PrivacyState.principal_id == actor).with_for_update()
                )
                if state is None:
                    raise DomainError("PRIVACY_STATE_MISSING", "个人数据控制状态缺失。", 503)
                if proposal.state == "completed":
                    return self._public_proposal(proposal)
                if self._holds(db, actor):
                    raise DomainError(
                        "PRIVACY_RETENTION_HOLD",
                        "存在明确到期的保留例外，请先查看提案和保留记录。",
                        409,
                    )
                if proposal.state == "proposed":
                    if aware(proposal.expires_at) <= clock(db):
                        raise DomainError(
                            "PRIVACY_PROPOSAL_EXPIRED", "删除提案已过期，请重新审阅。"
                        )
                    if state.blocked or state.generation != proposal.generation:
                        raise DomainError("PRIVACY_GENERATION_CHANGED", "其他个人数据操作已开始。")
                    from .public_read_authority import freeze_public_review_sessions

                    # Verify while ordinary access is still available. Retain
                    # only hash-bound public/system review provenance, never
                    # personal session text or a substitute model approval.
                    freeze_public_review_sessions(
                        db,
                        actor,
                        identifier,
                        self._public_authority_reads_in(db, actor),
                        self.app.knowledge,
                    )
                    state.generation += 1
                    state.blocked, state.operation_id, state.updated_at = (
                        True,
                        identifier,
                        clock(db),
                    )
                    proposal.state = "erasing"
                elif state.operation_id != identifier or not state.blocked:
                    raise DomainError("PRIVACY_OPERATION_CONFLICT", "个人数据操作状态不一致。")
            return None

        already = await asyncio.to_thread(begin)
        if already is not None:
            return already

        def erase() -> list[str]:
            with self.db.transaction() as db:
                proposal = db.scalar(
                    select(PrivacyDeletion)
                    .where(PrivacyDeletion.id == identifier)
                    .with_for_update()
                )
                if proposal is None:
                    raise DomainError("NOT_FOUND", "个人数据操作不存在。", 404)
                if proposal.state == "erasing":
                    selected = self._scope(db, actor)
                    cancelled = self.app.runtime.cancel_owned_in(
                        db, actor, reason="PRIVACY_ERASURE"
                    )
                    detached = self.app.shared_builds.detach_owned_in(db, actor)
                    cache_ids = sorted(
                        {
                            str(row[c])
                            for rows in selected.values()
                            for row in rows
                            for c in ("id", "run_id", "namespace_id", "turn_id")
                            if row.get(c)
                        }
                    )
                    result = self._erase_in(db, actor, selected)
                    cache_ids = sorted(set(cache_ids) | set(result.pop("_derived_cache_ids", [])))
                    proposal.result = {
                        **result,
                        "cancelled_runs": cancelled,
                        "detached_subscriptions": detached,
                        "cache_reference_ids": cache_ids,
                    }
                    proposal.state = "purging_cache"
                cache_ids = proposal.result.get("cache_reference_ids", [])
                return cache_ids

        cache_ids = await asyncio.to_thread(erase)
        purged = await self._purge_cache(set(cache_ids) | {actor})

        def complete() -> dict:
            with self.db.transaction() as db:
                state = db.scalar(
                    select(PrivacyState).where(PrivacyState.principal_id == actor).with_for_update()
                )
                proposal = db.get(PrivacyDeletion, identifier)
                if proposal is not None and proposal.state == "completed":
                    return self._public_proposal(proposal)
                if state is None or proposal is None or state.operation_id != identifier:
                    raise DomainError("PRIVACY_OPERATION_CONFLICT", "个人数据操作状态不一致。")
                proposal.result = {
                    k: v for k, v in proposal.result.items() if k != "cache_reference_ids"
                }
                proposal.result = {
                    **proposal.result,
                    "cache_entries_removed": purged,
                    "generation": state.generation,
                    "limitations": retention_policy()["excluded"],
                }
                proposal.state, proposal.completed_at = "completed", clock(db)
                state.blocked, state.operation_id, state.updated_at = False, None, clock(db)
                return self._public_proposal(proposal)

        return await asyncio.to_thread(complete)

    @staticmethod
    def _public_authority_reads_in(db, actor: str) -> set[str]:
        """Retain only source spans already serving as valid public evidence.

        The personal privacy fence is closed during erasure, so this internal
        check does not impersonate a reader or call a data-plane entry point.
        Public namespace labels alone never protect arbitrary read contents.
        """
        from sqlalchemy.orm import aliased

        from ..models import Evidence, Namespace, ObjectRow, SourceRead, SourceSet
        from ..util import digest, fingerprint, manifest
        from .sources import quote_occurrences

        evidence_object, source_object = aliased(ObjectRow), aliased(ObjectRow)
        public_namespaces = select(Namespace.id).where(Namespace.kind == "shared_public")
        rows = db.execute(
            select(SourceRead, Evidence, evidence_object, SourceSet, source_object)
            .join(Evidence, Evidence.read_id == SourceRead.id)
            .join(evidence_object, evidence_object.id == Evidence.id)
            .join(SourceSet, SourceSet.id == SourceRead.source_id)
            .join(source_object, source_object.id == SourceSet.id)
            .where(
                SourceRead.principal_id == actor,
                Evidence.source_id == SourceRead.source_id,
                evidence_object.kind == "evidence",
                evidence_object.state == "active",
                evidence_object.namespace_id.in_(public_namespaces),
                source_object.kind == "source_set",
                source_object.state == "active",
                source_object.namespace_id.in_(public_namespaces),
            )
            .execution_options(yield_per=200)
        )
        retained = set()
        for read, evidence, evidence_obj, source, source_obj in rows:
            if (
                not isinstance(source.files, dict)
                or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in source.files.items()
                )
                or not isinstance(source.metadata_json, dict)
                or not isinstance(source_obj.payload, dict)
                or source_obj.content_hash != fingerprint(source_obj.payload)
                or evidence_obj.content_hash != fingerprint(evidence_obj.payload)
            ):
                continue
            files_hash = manifest(source.files)
            identity = {
                "origin": source.origin_kind,
                "repository": source.repository_key,
                "commit": source.commit_sha,
                "files": files_hash,
            }
            binding = {
                "source_id": evidence.source_id,
                "read_id": evidence.read_id,
                "path": evidence.path,
                "start_line": evidence.start_line,
                "end_line": evidence.end_line,
                "quote_hash": evidence.quote_hash,
            }
            lines = source.files.get(read.path, "").splitlines(keepends=True)
            if (
                source.metadata_json.get("file_manifest_hash") != files_hash
                or source.scope_hash != fingerprint(identity)
                or any(source_obj.payload.get(k) != v for k, v in identity.items())
                or (
                    "license_id" in source_obj.payload
                    and source_obj.payload["license_id"] != source.license_id
                )
                or (
                    "rights_confirmed" in source_obj.payload
                    and source_obj.payload["rights_confirmed"]
                    != source.metadata_json.get("rights_confirmed")
                )
                or (
                    "synthetic" in source_obj.payload
                    and source_obj.payload["synthetic"]
                    != bool(source.metadata_json.get("synthetic"))
                )
                or binding != evidence_obj.payload
                or evidence.path != read.path
                or read.path not in source.files
                or not 1 <= read.first_line <= read.last_line <= len(lines)
                or read.text != "".join(lines[read.first_line - 1 : read.last_line])
                or read.file_hash != digest(source.files[read.path])
                or read.quote_hash != digest(read.text)
                or evidence.file_hash != read.file_hash
                or evidence.quote_hash != digest(evidence.exact_quote)
            ):
                continue
            offsets = quote_occurrences(read.text, evidence.exact_quote)
            if len(offsets) != 1:
                continue
            first = read.first_line + read.text[: offsets[0]].count("\n")
            last = first + evidence.exact_quote.rstrip("\n").count("\n")
            if (evidence.start_line, evidence.end_line) == (first, last):
                retained.add(read.id)
        return retained

    def _erase_in(self, db, actor, selected):
        from sqlalchemy import JSON, LargeBinary, String, and_, delete, update

        from ..models import AccessGrant, Base, Namespace, ObjectRow, Outbox, Principal
        from ..util import fingerprint, uid
        from .dependencies import _graph_lock

        _graph_lock(db, "topology")
        tables = Base.metadata.tables
        object_ids = {r["id"] for r in selected.get("dependency_objects", [])}
        public_authority_reads = self._public_authority_reads_in(db, actor)
        derived_ids, derived_count = self._redact_derived_in(db, actor, object_ids)
        # Internal erasure authority is proven by _scope. Do not call namespace_access:
        # ordinary data-plane entry points are intentionally blocked at this stage.
        for obj in db.scalars(select(ObjectRow).where(ObjectRow.id.in_(object_ids))):
            obj.state, obj.payload = "revoked", {}
            obj.content_hash, obj.revision = fingerprint({}), obj.revision + 1
            db.add(
                Outbox(
                    kind="invalidation",
                    aggregate_id=obj.id,
                    payload={"generation": obj.revision, "reason": "PRIVACY_ERASURE"},
                )
            )
        db.flush()
        for name, rows in selected.items():
            if not rows or name in {"dependency_objects", "knowledge_namespaces"}:
                continue
            table = tables[name]
            for row in rows:
                predicate = and_(*(c == row[c.name] for c in table.primary_key.columns))
                # Reconstructible search material and cache bytes need no audit tombstones.
                if name in {
                    "knowledge_embeddings",
                    "knowledge_search_documents",
                    "github_http_cache",
                }:
                    db.execute(delete(table).where(predicate))
                    continue
                values = {}
                for col in table.columns:
                    key, value = col.name, row[col.name]
                    if col.primary_key or col.foreign_keys or value is None:
                        continue
                    if (
                        name == "source_reads"
                        and row["id"] in public_authority_reads
                        and key in {"path", "text"}
                    ):
                        # The exact public span is an authority witness, not
                        # the admin's personal session. Session metadata below
                        # is still erased; unreferenced/private reads are not
                        # covered by this narrowly validated public exception.
                        continue
                    if isinstance(col.type, JSON):
                        if name == "funding_agreements" and key in {
                            "allowed_roles",
                            "allowed_source_ids",
                            "allowed_data_classes",
                        }:
                            # These identifier/enum scopes are minimal authorization audit.
                            # [] means unrestricted sources: never widen an old contract,
                            # even while retiring the agreement itself as revoked.
                            continue
                        if name == "model_call_receipts" and key == "usage":
                            values[key] = {
                                k: v for k, v in value.items() if type(v) is int and v >= 0
                            }
                        else:
                            values[key] = [] if isinstance(value, list) else {}
                    elif isinstance(col.type, LargeBinary):
                        values[key] = b""
                    elif (
                        isinstance(col.type, String)
                        and key not in _SAFE_TEXT
                        and not key.endswith(("_hash", "_digest", "_sha"))
                    ):
                        # Unique textual keys cannot all become the same empty value.
                        values[key] = (
                            ("erased:" + fingerprint([name, row.get("id"), key])[:24])
                            if key
                            in {
                                "key",
                                "idempotency_key",
                                "origin_key",
                                "subject_key",
                                "applicability_key",
                                "node_key",
                                "task_key",
                                "repository_key",
                                "stage_key",
                                "name",
                                "facet_key",
                                "selection_context",
                                "scope_key",
                                "legacy_id",
                                "table_name",
                            }
                            else ""
                        )
                if name == "agent_runs":
                    values.update(worker_id=None, lease_until=None)
                if name == "workflow_executions":
                    values.update(
                        status="cancelled",
                        worker_id=None,
                        lease_until=None,
                        epoch=row["epoch"] + 1,
                        revision=row["revision"] + 1,
                        wait_kind=None,
                        last_error_code="PRIVACY_ERASURE",
                    )
                if name == "workflow_evolution_requests":
                    values.update(status="cancelled", failure_code="PRIVACY_ERASURE")
                if name == "workflow_tutor_requests":
                    values.update(status="cancelled", failure_code="PRIVACY_ERASURE")
                if name == "workflow_plan_requests":
                    values.update(status="rejected", failure_code="PRIVACY_ERASURE")
                if name == "workflow_control_commands":
                    values.update(status="rejected", checkpoint_id=None)
                if name == "workflow_sessions":
                    values.update(control_state="cancelled", revision=row["revision"] + 1)
                if name == "funding_agreements":
                    values.update(status="revoked")
                if name == "conversation_turns":
                    values.update(status="interrupted")
                if name == "workspaces":
                    values.update(sealed=True, revision=row["revision"] + 1)
                if name == "command_authorizations":
                    values.update(state="cancelled", receipt=None)
                if name == "model_call_receipts":
                    values.update(response=None, status="redacted")
                if name == "submissions":
                    values.update(revoked=True, accepted=False)
                if name == "teaching_observations":
                    values.update(competence_verified=False)
                if name in {"learning_profiles", "learning_plans", "career_goals"}:
                    for key in ("active_plan_id", "active_module_id", "active_version_id"):
                        if key in table.c:
                            values[key] = None
                if name == "learning_attempts":
                    values.update(state="closed")
                if name == "learning_observation_records":
                    values.update(status="withdrawn", assessment="")
                if name == "legacy_import_batches":
                    values.update(status="erased")
                if name in {"policy_rollouts", "evolution_candidates"}:
                    values.update(status="cancelled")
                if name == "maintenance_schedules":
                    values.update(enabled=False, allow_model_calls=False)
                if name == "maintenance_occurrences" and row["status"] in {"scheduled", "planned"}:
                    values.update(status="cancelled", reason="privacy_erasure")
                if name == "topic_governance":
                    values.update(status="retired")
                if name == "source_update_plans":
                    values.update(state="cancelled")
                if name == "taxonomy_proposals":
                    values.update(status="erased")
                if name == "task_subscriptions":
                    values.update(active=False, revision=row["revision"] + 1)
                if name == "outbox_messages":
                    values.update(delivered=True)
                if values:
                    db.execute(update(table).where(predicate).values(**values))
        namespace_ids = [r["id"] for r in selected.get("knowledge_namespaces", [])]
        db.execute(
            update(AccessGrant)
            .where(AccessGrant.namespace_id.in_(namespace_ids))
            .values(active=False, revision=AccessGrant.revision + 1)
        )
        for ns in db.scalars(select(Namespace).where(Namespace.id.in_(namespace_ids))):
            ns.kind, ns.name, ns.revision = "workspace", "已清理个人资料域", ns.revision + 1
        user = db.get(Principal, actor)
        user.name, user.auth_revision = "erased-" + actor, user.auth_revision + 1
        new_ns = Namespace(id=uid(), owner_id=actor, kind="private", name="新的个人资料域")
        db.add(new_ns)
        return {
            "rows_redacted": {k: len(v) for k, v in selected.items() if v},
            "new_namespace_id": new_ns.id,
            "derived_copies_redacted": derived_count,
            "_derived_cache_ids": sorted(derived_ids),
            "retention_policy": retention_policy(),
        }

    @staticmethod
    def _redact_derived_in(db, actor: str, roots: set[str]) -> tuple[set[str], int]:
        """Remove provably copied private renderings, not another person's own work.

        Reverse traversal never enters a shared-public namespace. Only generated
        rendering objects are redacted; other users' questions, independent blocks,
        hand-written notes, accounting and public knowledge remain intact.
        """
        import re

        from ..contracts_answer_completion_v166 import EVIDENCE_ANSWER_STATE
        from ..models import (
            AnswerBlock,
            ArtifactDependency,
            ModelReceipt,
            Namespace,
            ObjectRow,
            Outbox,
            SourceRead,
            StageArtifact,
            StreamEvent,
            Turn,
        )
        from ..util import fingerprint

        seen, frontier = set(roots), sorted(roots)
        derived = []
        while frontier:
            batch, frontier = frontier[:200], frontier[200:]
            rows = db.scalars(
                select(ObjectRow)
                .join(ArtifactDependency, ArtifactDependency.dependent_id == ObjectRow.id)
                .join(Namespace, Namespace.id == ObjectRow.namespace_id)
                .where(
                    ArtifactDependency.source_id.in_(batch),
                    # A shared selection manifest is provenance, not proof
                    # that an independent block copied the erased material.
                    ArtifactDependency.enforcement != "provenance_only",
                    Namespace.kind != "shared_public",
                )
            ).all()
            for obj in rows:
                if obj.id in seen:
                    continue
                seen.add(obj.id)
                frontier.append(obj.id)
                if obj.kind in {
                    "answer_block",
                    "selection_manifest",
                    "comparison",
                    "learning_session",
                    "module_completion_ack",
                }:
                    derived.append(obj)
        references = set(seen)
        matched_stages: set[tuple[str, str]] = set()
        for obj in derived:
            obj.state, obj.payload, obj.content_hash = "revoked", {}, fingerprint({})
            obj.revision += 1
            references.add(obj.id)
            ns = db.get(Namespace, obj.namespace_id)
            if ns is not None:
                ns.revision += 1
            db.add(
                Outbox(
                    kind="invalidation",
                    aggregate_id=obj.id,
                    payload={"generation": obj.revision, "reason": "PRIVACY_DERIVED_COPY"},
                )
            )
            if obj.kind == "answer_block":
                block = db.get(AnswerBlock, obj.id)
                if block is not None:
                    block.content, block.content_hash = {}, fingerprint({})
                    turn = db.scalar(select(Turn).where(Turn.id == block.turn_id).with_for_update())
                    if turn is not None and turn.run_id:
                        matched_stages.update(
                            (turn.run_id, stage)
                            for stage in (
                                "tutor",
                                f"tutor.block:{block.ordinal}",
                                f"tutor.check:{block.ordinal}",
                                f"answer.check:{block.ordinal}",
                            )
                        )
                for event in db.scalars(
                    select(StreamEvent).where(StreamEvent.payload["block_id"].as_string() == obj.id)
                ):
                    event.event_type = "answer.evidence_invalidated"
                    event.payload = {"block_id": obj.id, "reason": "PRIVACY_ERASURE"}
                if block is not None:
                    from .answers import stream_event

                    stream_event(
                        db,
                        block.turn_id,
                        "answer.evidence_invalidated",
                        {"block_id": obj.id, "reason": "PRIVACY_ERASURE"},
                    )
        # A read log is a precise copy of the erased SourceSet, not a learner's prose.
        source_ids = list(
            db.scalars(
                select(ObjectRow.id).where(ObjectRow.id.in_(roots), ObjectRow.kind == "source_set")
            )
        )
        for read in db.scalars(select(SourceRead).where(SourceRead.source_id.in_(source_ids))):
            read.text, read.path = "", ""
            references.add(read.id)
        uuid_pattern = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

        def contains_reference(value):
            if isinstance(value, str):
                return value in references or any(
                    m.group(0) in references for m in uuid_pattern.finditer(value)
                )
            if isinstance(value, dict):
                return any(contains_reference(v) for v in value.values())
            if isinstance(value, list):
                return any(contains_reference(v) for v in value)
            return False

        for turn in db.scalars(select(Turn).execution_options(yield_per=200)):
            if contains_reference(turn.focus.get(EVIDENCE_ANSWER_STATE)):
                turn = db.scalar(
                    select(Turn)
                    .where(Turn.id == turn.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if turn is not None and contains_reference(turn.focus.get(EVIDENCE_ANSWER_STATE)):
                    # This server-owned composite copies the frozen answer and
                    # fact bindings even before an AnswerBlock exists. It is
                    # not another owner's independent question or focus. Keep
                    # the key as a tombstone so interrupted delivery fails its
                    # plan binding instead of silently retrieving a new answer.
                    turn.focus = {**turn.focus, EVIDENCE_ANSWER_STATE: {}}
        for stage in db.scalars(select(StageArtifact).execution_options(yield_per=200)):
            if contains_reference(stage.payload):
                witness = stage.payload
                receipt_id = witness.get("receipt_id")
                if (
                    witness.get("kind") == "model_access_witness"
                    and isinstance(receipt_id, str)
                    and stage.stage_key == "model-access:" + receipt_id
                ):
                    bound_receipt = db.get(ModelReceipt, receipt_id)
                    if (
                        bound_receipt is not None
                        and bound_receipt.run_id == stage.run_id
                        and bound_receipt.request_hash == witness.get("request_hash")
                    ):
                        bound_receipt.response, bound_receipt.status = None, "redacted"
                stage.payload = {}
        for receipt in db.scalars(
            select(ModelReceipt)
            .where(ModelReceipt.response.is_not(None))
            .execution_options(yield_per=200)
        ):
            if (receipt.run_id, receipt.stage_key) in matched_stages or contains_reference(
                receipt.response
            ):
                receipt.response, receipt.status = None, "redacted"
        return references, len(derived)

    async def _purge_cache(self, identifiers: set[str]) -> int:
        import json

        from ..adapters.cache import MemoryCache, RedisCache

        def contains(value):
            if isinstance(value, str):
                return value in identifiers
            if isinstance(value, dict):
                return any(contains(k) or contains(v) for k, v in value.items())
            if isinstance(value, list):
                return any(contains(v) for v in value)
            return False

        cache, removed = self.app.cache, 0
        if isinstance(cache, MemoryCache):
            for key, (_, value) in list(cache.values.items()):
                if contains(value):
                    cache.values.pop(key, None)
                    removed += 1
            return removed
        if isinstance(cache, RedisCache):
            # Do not FLUSHDB: other owners' cache and broker data are outside this request.
            for layer in ("exact", "base", "derived"):
                async for key in cache.redis.scan_iter(match=f"cf15:{layer}:*", count=100):
                    if key.endswith(":lock"):
                        continue
                    raw = await cache.redis.get(key)
                    if raw is None:
                        continue
                    try:
                        value = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if contains(value):
                        removed += int(await cache.redis.delete(key))
            return removed
        raise DomainError(
            "PRIVACY_CACHE_UNSUPPORTED", "缓存适配器未实现安全个人数据清理，保持读取封锁。", 503
        )

    def create_hold(self, actor: str, value) -> dict:
        from datetime import timedelta

        from ..db import aware, clock
        from ..models_privacy_ext import PrivacyRetentionHold
        from .security import principal

        with self.db.transaction() as db:
            if not principal(db, actor).is_admin:
                raise DomainError("FORBIDDEN", "仅管理员可登记保留例外。", 403)
            principal(db, value.principal_id)
            if not clock(db) < aware(value.expires_at) <= clock(db) + timedelta(days=3650):
                raise DomainError("RETENTION_EXPIRY", "保留例外必须明确到期且不超过十年。", 422)
            state = db.scalar(
                select(PrivacyState)
                .where(PrivacyState.principal_id == value.principal_id)
                .with_for_update()
            )
            if state is None or state.blocked:
                raise DomainError("PRIVACY_ERASURE_PENDING", "清理已开始，不能追溯恢复已删除正文。")
            hold = PrivacyRetentionHold(placed_by=actor, **value.model_dump())
            db.add(hold)
            db.flush()
            return {"hold_id": hold.id, "expires_at": aware(hold.expires_at).isoformat()}

    def release_hold(self, actor: str, identifier: str) -> dict:
        from ..models_privacy_ext import PrivacyRetentionHold
        from .security import principal

        with self.db.transaction() as db:
            if not principal(db, actor).is_admin:
                raise DomainError("FORBIDDEN", "仅管理员可解除保留例外。", 403)
            hold = db.get(PrivacyRetentionHold, identifier)
            if hold is None:
                raise DomainError("NOT_FOUND", "保留例外不存在。", 404)
            hold.active = False
            return {"hold_id": hold.id, "active": False}

    def purge_expired_audits(self, actor: str) -> dict:
        from sqlalchemy import delete

        from ..db import clock
        from ..models_privacy_ext import PrivacyDeletion
        from .security import principal

        with self.db.transaction() as db:
            if not principal(db, actor).is_admin:
                raise DomainError("FORBIDDEN", "仅管理员可执行审计保留清理。", 403)
            expired = list(
                db.scalars(
                    select(PrivacyDeletion.id).where(
                        PrivacyDeletion.state == "completed",
                        PrivacyDeletion.audit_retain_until <= clock(db),
                    )
                )
            )
            db.execute(delete(PrivacyDeletion).where(PrivacyDeletion.id.in_(expired)))
            return {"expired_deletion_audits_removed": len(expired)}
