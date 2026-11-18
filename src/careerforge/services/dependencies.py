"""Typed cross-namespace dependency graph. Edges belong to the consumer's domain."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import CTE

from ..db import clock
from ..errors import DomainError
from ..models import AccessGrant, ArtifactDependency, Impact, Namespace, ObjectRow, Outbox
from ..models_runtime_ext import DependencyFrontier, DependencyInvalidationJob
from ..util import canonical, fingerprint, uid
from .security import namespace_access, namespace_admin, principal

# All legal triples are explicit. Unknown pairs cannot be written by model output.
EDGES: dict[tuple[str, str, str], set[str]] = {}


def _edges(consumer: str, sources: list[str], kind: str, *enforcements: str) -> None:
    for source in sources:
        EDGES[consumer, source, kind] = set(enforcements)


_edges("evidence", ["source_set"], "grounded_in", "blocking")
_edges("claim", ["evidence", "source_set"], "requires_evidence", "blocking")
_edges("fact_atom", ["claim"], "requires_claim", "blocking")
_edges("render_element", ["fact_atom"], "renders_fact", "blocking")
_edges("knowledge_unit_version", ["render_element"], "contains_fact", "blocking")
_edges("knowledge_unit_version", ["source_set"], "grounded_in", "blocking")
_edges("review_decision", ["claim"], "reviewed_claim", "provenance_only")
_edges("review_decision", ["knowledge_unit_version"], "reviewed_version", "provenance_only")
_edges("review_decision", ["evidence"], "requires_evidence", "blocking")
_edges("topic_link", ["topic_version"], "classified_by", "blocking")
_edges("facet_value", ["claim"], "requires_claim", "blocking")
_edges("facet_value", ["facet_schema"], "conforms_to", "blocking")
_edges("topic_version", ["evidence"], "grounded_in", "blocking")
_edges("facet_schema", ["evidence"], "grounded_in", "blocking")
_edges("publication", ["knowledge_unit_version"], "publishes_version", "blocking")
_edges("publication", ["review_decision"], "requires_review", "blocking")
_edges("publication", ["fact_atom"], "requires_fact", "blocking")
_edges("publication", ["topic_link"], "classified_by", "impact_only")
_edges("publication", ["facet_value"], "uses_facet", "blocking", "impact_only")
_edges("comparison", ["publication"], "compares_with", "blocking")
_edges("comparison", ["facet_value"], "uses_facet", "blocking")
_edges("comparison", ["facet_schema"], "conforms_to", "blocking")
_edges("comparison", ["topic_link"], "requires_topic_link", "blocking")
_edges("answer_block", ["publication"], "teaches_from", "blocking")
_edges("answer_block", ["fact_atom"], "states_fact", "blocking")
_edges("answer_block", ["source_set"], "observes_source", "blocking")
_edges("answer_block", ["comparison"], "compares_with", "blocking")
_edges("answer_block", ["topic_link"], "requires_topic_link", "blocking")
_edges("plan_module", ["publication"], "teaches_from", "blocking")
_edges("plan_module", ["topic_link"], "requires_topic_link", "blocking")
_edges("exercise", ["plan_module"], "derives_from", "impact_only")
_edges("exercise", ["publication"], "teaches_from", "impact_only")
_edges("selection_head", ["publication"], "selects", "blocking")
_edges("selection_manifest", ["publication"], "selects_source", "blocking")
_edges("selection_manifest", ["goal_version"], "binds_goal_version", "blocking")
_edges("selection_manifest", ["source_set"], "constrains_source_path", "blocking")
for _consumer in ["answer_block", "comparison", "plan_module"]:
    _edges(_consumer, ["selection_manifest"], "generated_under", "provenance_only")
_edges("selection_manifest", ["plan_module"], "binds_session_module", "blocking")
_edges("answer_block", ["selection_manifest"], "bound_to_session", "blocking")
_edges("learning_session", ["selection_manifest"], "uses_session_selection", "blocking")
_edges("module_completion_ack", ["learning_session"], "acknowledges_session", "blocking")
_edges("module_completion_ack", ["answer_block"], "acknowledges_delivery", "blocking")
_edges("ordinary_checker_turn", ["ordinary_checker_conversation"], "admitted_under", "blocking")
_edges("answer_block", ["ordinary_checker_turn"], "bound_to_ordinary_checker", "blocking")
_edges(
    "answer_feedback_target",
    ["selection_manifest", "ordinary_checker_turn", "source_set"],
    "targets_answer",
    "blocking",
)
_edges(
    "answer_feedback_turn",
    ["answer_feedback_target", "ordinary_checker_turn"],
    "freezes_feedback",
    "blocking",
)
OBJECT_TYPES = {x for key in EDGES for x in key[:2]}
# Personal goal snapshots are standalone authority roots. They have no public
# consumers and are not source content; model egress declares private_jd.
OBJECT_TYPES.update(
    {"goal_version", "jd_revision", "learning_preference_event", "source_navigation_ack"}
)
OBJECT_TYPES.add("goal_addendum")
OBJECT_TYPES.add("project_selection_policy")
_edges("project_selection_policy", ["goal_version"], "binds_goal_version", "blocking")
OBJECT_TYPES.add("project_portfolio")
OBJECT_TYPES.add("draft_implementation_link")
OBJECT_TYPES.add("linked_analysis_preview")
OBJECT_TYPES.add("jd_knowledge_mapping_preview")
OBJECT_TYPES.add("knowledge_curation_preview")
for _kind in ["curation_semantic_review", "curation_tutor_trial", "curation_trial_check"]:
    OBJECT_TYPES.add(_kind)
    _edges(_kind, ["knowledge_curation_preview"], "validates_curation", "blocking")
    _edges(_kind, ["knowledge_unit_version"], "validates_candidate_unit", "blocking")
    _edges(_kind, ["evidence"], "requires_independent_evidence", "blocking")
    if _kind != "curation_semantic_review":
        _edges(_kind, ["curation_semantic_review"], "requires_semantic_review", "blocking")
_edges("curation_trial_check", ["curation_tutor_trial"], "checks_tutor_trial", "blocking")
OBJECT_TYPES.add("curation_validation_summary")
OBJECT_TYPES.add("curation_publication_batch")
_edges(
    "curation_publication_batch",
    ["curation_validation_summary"],
    "publishes_validated_candidates",
    "blocking",
)
_edges("curation_publication_batch", ["publication"], "contains_publication", "blocking")
_edges(
    "curation_publication_batch",
    ["knowledge_implementation_link"],
    "contains_implementation_link",
    "blocking",
)
_edges(
    "curation_validation_summary", ["knowledge_curation_preview"], "summarizes_curation", "blocking"
)
_edges(
    "curation_validation_summary",
    ["curation_semantic_review"],
    "includes_semantic_review",
    "blocking",
)
_edges("curation_validation_summary", ["curation_tutor_trial"], "includes_tutor_trial", "blocking")
_edges("curation_validation_summary", ["curation_trial_check"], "includes_trial_check", "blocking")
_edges("knowledge_curation_preview", ["goal_version"], "binds_goal_version", "blocking")
_edges("knowledge_curation_preview", ["knowledge_curation_preview"], "revises_curation", "blocking")
_edges(
    "knowledge_curation_preview", ["curation_semantic_review"], "corrected_from_review", "blocking"
)
_edges(
    "knowledge_curation_preview",
    ["linked_analysis_preview"],
    "curates_analysis_preview",
    "blocking",
)
_edges(
    "knowledge_curation_preview", ["jd_knowledge_mapping_preview"], "uses_jd_mapping", "blocking"
)
_edges("knowledge_curation_preview", ["knowledge_unit_version"], "curates_draft_unit", "blocking")
_edges("jd_knowledge_mapping_preview", ["goal_version"], "binds_goal_version", "blocking")
_edges(
    "jd_knowledge_mapping_preview", ["linked_analysis_preview"], "maps_analysis_preview", "blocking"
)
_edges("jd_knowledge_mapping_preview", ["project_portfolio"], "maps_selected_portfolio", "blocking")
_edges("jd_knowledge_mapping_preview", ["draft_implementation_link"], "maps_draft_pair", "blocking")
_edges("linked_analysis_preview", ["goal_version"], "binds_goal_version", "blocking")
_edges("linked_analysis_preview", ["project_portfolio"], "analyzes_portfolio", "blocking")
_edges("linked_analysis_preview", ["knowledge_unit_version"], "contains_analysis_draft", "blocking")
_edges("linked_analysis_preview", ["draft_implementation_link"], "contains_draft_pair", "blocking")
_edges(
    "draft_implementation_link", ["knowledge_unit_version"], "links_architecture_draft", "blocking"
)
_edges(
    "draft_implementation_link",
    ["knowledge_unit_version"],
    "links_implementation_draft",
    "blocking",
)
_edges("draft_implementation_link", ["goal_version"], "binds_goal_version", "blocking")
_edges("project_portfolio", ["goal_version"], "binds_goal_version", "blocking")
_edges("project_portfolio", ["project_pool_entry"], "selects_project", "blocking")
OBJECT_TYPES.update({"project_pool_entry", "project_quality_report"})
_edges("project_quality_report", ["source_set"], "assesses_project_source", "blocking")
_edges("project_pool_entry", ["source_set"], "stores_project_source", "blocking")
_edges("project_pool_entry", ["project_quality_report"], "uses_project_quality", "blocking")
_edges("project_pool_entry", ["goal_version"], "binds_goal_version", "blocking")
_edges(
    "project_quality_report",
    ["project_quality_report"],
    "reassesses_quality_report",
    "provenance_only",
)
_edges("project_pool_entry", ["project_pool_entry"], "reassesses_project_entry", "provenance_only")
OBJECT_TYPES.update(
    {"learning_language_profile", "learning_knowledge_outline", "knowledge_implementation_link"}
)
_edges("learning_language_profile", ["goal_version"], "binds_goal_version", "blocking")
_edges(
    "learning_language_profile",
    ["learning_language_profile"],
    "inherits_language_choice",
    "blocking",
)
_edges("learning_knowledge_outline", ["goal_version"], "binds_goal_version", "blocking")
_edges(
    "learning_knowledge_outline", ["learning_language_profile"], "uses_language_profile", "blocking"
)
_edges("knowledge_implementation_link", ["publication"], "links_knowledge", "blocking")
_edges("goal_version", ["goal_version"], "amends_goal", "blocking")
_edges("goal_version", ["goal_addendum"], "applies_addendum", "blocking")
_edges("goal_addendum", ["goal_version"], "derived_from_goal", "blocking")
_edges("goal_addendum", ["goal_addendum"], "inherits_addendum", "blocking")
_edges("goal_addendum", ["topic_version"], "maps_user_objective_to_topic", "blocking")
_edges("goal_addendum", ["facet_schema"], "maps_user_objective_to_facet", "blocking")
_edges("goal_version", ["jd_revision"], "derived_from_jd", "blocking")
_edges("goal_version", ["topic_version"], "maps_requirement_to_topic", "blocking")
_edges("goal_version", ["facet_schema"], "maps_requirement_to_facet", "blocking")
# These registry nodes contain source/knowledge/taxonomy text, not just personal
# learning state or a selection pointer. Their private namespace requires the
# private_source egress category even when reached from an otherwise public pack.
SOURCE_CONTENT_TYPES = {
    "source_set",
    "evidence",
    "claim",
    "fact_atom",
    "render_element",
    "knowledge_unit_version",
    "review_decision",
    "topic_link",
    "topic_version",
    "facet_value",
    "facet_schema",
    "publication",
}


def register(
    db: Session,
    actor: str,
    ns_id: str,
    kind: str,
    payload: dict[str, Any],
    identifier: str | None = None,
) -> ObjectRow:
    namespace_access(db, actor, ns_id, True)
    if kind not in OBJECT_TYPES:
        raise DomainError("OBJECT_TYPE", "未注册依赖对象类型。", 422)
    row = ObjectRow(
        id=identifier or uid(),
        namespace_id=ns_id,
        kind=kind,
        payload=payload,
        content_hash=fingerprint(payload),
    )
    db.add(row)
    db.flush()
    return row


def require_object(
    db: Session, actor: str, identifier: str, kind: str | None = None, active: bool = True
) -> ObjectRow:
    row = db.get(ObjectRow, identifier)
    if row is None or (kind and row.kind != kind):
        raise DomainError("NOT_FOUND", "对象不可访问。", 404)
    namespace_access(db, actor, row.namespace_id)
    if active and row.state != "active":
        raise DomainError("EVIDENCE_UNAVAILABLE", "证据已暂停或撤销。", 409)
    if row.content_hash != fingerprint(row.payload):
        raise DomainError("ARTIFACT_HASH", "资料完整性校验失败。", 409)
    return row


def _graph_lock(db: Session, key: str) -> None:
    if db.get_bind().dialect.name == "postgresql":
        value = int(fingerprint({"dependency_lock": key})[:16], 16) - 2**63
        db.execute(select(func.pg_advisory_xact_lock(value)))


def _reachable(roots: list[str], blocking_only: bool) -> CTE:
    # UNION (not UNION ALL) deduplicates in the database, including malformed
    # historical cycles. Database work memory can spill; Python never stores V.
    closure = (
        select(ObjectRow.id.label("node_id"))
        .where(ObjectRow.id.in_(roots))
        .cte("dependency_closure", recursive=True)
    )
    condition = (
        ArtifactDependency.enforcement == "blocking"
        if blocking_only
        else ArtifactDependency.enforcement != "provenance_only"
    )
    return closure.union(
        select(ArtifactDependency.source_id)
        .join(closure, ArtifactDependency.dependent_id == closure.c.node_id)
        .where(condition)
    )


def _reaches(db: Session, start: str, target: str) -> bool:
    closure = _reachable([start], blocking_only=False)
    result = db.scalar(select(exists(select(closure.c.node_id).where(closure.c.node_id == target))))
    if result is None:
        raise DomainError("GRAPH_QUERY", "依赖图检查没有返回权威结果。", 503)
    return bool(result)


def depend(
    db: Session,
    actor: str,
    dependent_id: str,
    source_id: str,
    kind: str,
    enforcement: str = "blocking",
) -> ArtifactDependency:
    # Serializing topology writers makes the cycle check and edge insert atomic.
    # SQLite already serializes writers via BEGIN IMMEDIATE. No network I/O here.
    _graph_lock(db, "topology")
    consumer = require_object(db, actor, dependent_id)
    source = require_object(db, actor, source_id)
    consumer_ns = namespace_access(db, actor, consumer.namespace_id, True)
    source_ns = namespace_access(db, actor, source.namespace_id)
    if consumer_ns.kind == "shared_public" and source_ns.kind != "shared_public":
        raise DomainError("PRIVATE_TO_PUBLIC", "私人知识不能进入公共依赖。", 403)
    if enforcement not in EDGES.get((consumer.kind, source.kind, kind), set()):
        raise DomainError("DEPENDENCY_TYPE", "不支持的依赖类型组合。", 422)
    old = db.scalar(
        select(ArtifactDependency).where(
            ArtifactDependency.dependent_id == dependent_id,
            ArtifactDependency.source_id == source_id,
            ArtifactDependency.kind == kind,
            ArtifactDependency.enforcement == enforcement,
        )
    )
    if old:
        return old
    if enforcement != "provenance_only":
        # Prevent a new edge appearing behind an invalidation cursor through an
        # active-looking object whose blocking ancestor has already been revoked.
        check_closure(db, actor, [source_id])
    if enforcement != "provenance_only" and _reaches(db, source_id, dependent_id):
        raise DomainError("DEPENDENCY_CYCLE", "依赖图不得形成失效传播环。", 422)
    user = principal(db, actor)
    row = ArtifactDependency(
        edge_namespace_id=consumer.namespace_id,
        dependent_id=consumer.id,
        dependent_type=consumer.kind,
        dependent_namespace_id=consumer.namespace_id,
        source_id=source.id,
        source_type=source.kind,
        source_namespace_id=source.namespace_id,
        kind=kind,
        enforcement=enforcement,
        authorization_ref=f"actor:{actor}",
        authorization_revision=user.auth_revision,
    )
    db.add(row)
    db.flush()
    return row


@dataclass(frozen=True)
class ClosureSummary:
    checked_objects: int
    closure_hash: str
    root_revisions: dict[str, int]
    public_only: bool = True
    private_source_content: bool = False
    public_source_content: bool = False

    @property
    def source_data_classes(self) -> list[str]:
        """Observed source categories, never an authorization grant."""
        return [
            category
            for category, present in (
                ("private_source", self.private_source_content),
                ("public_source", self.public_source_content),
            )
            if present
        ]


def check_closure(db: Session, actor: str, roots: list[str]) -> ClosureSummary:
    """Streaming authority barrier independent of asynchronous invalidation progress.

    Memory is bounded by 128 database rows plus at most 1024 requested roots, not
    by transitive graph size. The return value is a bounded audit summary; callers
    must never use it instead of a fresh authority check on their next operation.
    """
    if len(roots) > 1024:
        raise DomainError(
            "GRAPH_ROOT_LIMIT", "单次最多校验1024个显式根；闭包节点数不受此限制。", 422
        )
    user = principal(db, actor)
    root_revisions = {
        identifier: require_object(db, actor, identifier).revision for identifier in set(roots)
    }
    if not roots:
        return ClosureSummary(0, sha256(b"").hexdigest(), root_revisions)
    closure = _reachable(list(root_revisions), blocking_only=True)
    grant = exists(
        select(AccessGrant.id).where(
            AccessGrant.namespace_id == Namespace.id,
            AccessGrant.principal_id == user.id,
            AccessGrant.active.is_(True),
        )
    )
    authorized = or_(Namespace.owner_id == user.id, Namespace.kind == "shared_public", grant)
    query = (
        select(ObjectRow, authorized.label("authorized"), Namespace.owner_id, Namespace.kind)
        .join(closure, ObjectRow.id == closure.c.node_id)
        .outerjoin(Namespace, Namespace.id == ObjectRow.namespace_id)
        .order_by(ObjectRow.id)
    )
    from .privacy import assert_personal_access

    digest, count, public_only = sha256(), 0, True
    private_source_content = public_source_content = False
    # One cached owner, not an unbounded set of all reachable namespaces.
    previous_owner: str | None = None
    for row, permitted, owner_id, namespace_kind in db.execute(
        query.execution_options(yield_per=128, populate_existing=True)
    ):
        if not permitted:
            raise DomainError("NOT_FOUND", "依赖对象不可访问。", 404)
        if namespace_kind == "shared_public":
            public_source_content = public_source_content or row.kind in SOURCE_CONTENT_TYPES
        else:
            public_only = False
            private_source_content = private_source_content or row.kind in SOURCE_CONTENT_TYPES
            if previous_owner != owner_id:
                assert_personal_access(db, owner_id)
                previous_owner = owner_id
        if row.state != "active":
            raise DomainError("EVIDENCE_UNAVAILABLE", "证据已暂停或撤销。", 409)
        if row.content_hash != fingerprint(row.payload):
            raise DomainError("ARTIFACT_HASH", "资料完整性校验失败。", 409)
        digest.update(canonical([row.id, row.revision, row.content_hash]).encode())
        count += 1
    return ClosureSummary(
        count,
        digest.hexdigest(),
        root_revisions,
        public_only,
        private_source_content,
        public_source_content,
    )


def assert_source_scope(db: Session, roots: list[str], allowed_source_ids: list[str]) -> None:
    """Check funded source identities in SQL without materializing the closure.

    An empty stored list is the existing unrestricted-source contract, unlike an
    empty role/data-class list (which is deny-all). No old funding is expanded.
    This scope check complements, never replaces, check_closure's live ACL/state
    and hash barrier. The calling transaction owns the funding/run lock.
    """
    if len(roots) > 1024 or len(allowed_source_ids) > 1024:
        raise DomainError("FUNDING_SOURCE_SCOPE", "单次来源范围最多1024个显式身份。", 422)
    if not allowed_source_ids or not roots:
        return
    closure = _reachable(roots, blocking_only=True)
    outside = db.scalar(
        select(ObjectRow.id)
        .join(closure, ObjectRow.id == closure.c.node_id)
        .where(ObjectRow.kind == "source_set", ObjectRow.id.not_in(allowed_source_ids))
        .limit(1)
    )
    if outside is not None:
        raise DomainError("FUNDING_SOURCE_SCOPE", "来源不在本次费用授权的固定范围内。", 403)


def invalidate(db: Session, actor: str, identifier: str, state: str, reason: str) -> ObjectRow:
    # The topology fence is shared with depend(): no edge may commit behind an
    # invalidation cursor after checking an ancestor's pre-revocation snapshot.
    _graph_lock(db, "topology")
    obj = require_object(db, actor, identifier, active=False)
    ns = namespace_admin(db, actor, obj.namespace_id)
    db.refresh(obj, with_for_update=True)
    db.refresh(ns)
    if state not in {"suspended", "revoked"}:
        raise DomainError("INVALID_STATE", "只允许暂停或撤销。", 422)
    if obj.state == "revoked" and state != "revoked":
        raise DomainError("REVOKED_IMMUTABLE", "已撤销发布不能恢复原记录。")
    if obj.state == state:
        return obj
    obj.state = state
    obj.revision += 1
    ns.revision += 1
    db.add(
        Outbox(
            kind="invalidation",
            aggregate_id=obj.id,
            payload={"generation": obj.revision, "reason": reason[:200]},
        )
    )
    return obj


@dataclass(frozen=True)
class PropagationProgress:
    job_id: str
    complete: bool
    edges_scanned: int
    new_impacts: int


def propagate_batch(
    db: Session, root: str, generation: int, batch_size: int = 256
) -> PropagationProgress:
    """One durable bounded batch. Internal result: never expose cross-domain counts.

    The caller commits this transaction, then leaves the outbox undelivered until
    complete=True. A crash rolls back just this batch; job/frontier cursors survive
    earlier commits. The job row lock serializes concurrent/repeated deliveries.
    """
    if not 1 <= batch_size <= 1000 or generation < 1:
        raise DomainError("GRAPH_BATCH", "传播批次或代际无效。", 422)
    _graph_lock(db, "invalidation:" + root + ":" + str(generation))
    state = db.execute(
        select(ObjectRow.state, ObjectRow.revision).where(ObjectRow.id == root)
    ).first()
    # An upheld dispute can restore a suspended source before this historical
    # outbox is delivered. The durable originating invalidation, not today's
    # state, proves the generation; current reads still use the live closure.
    origin = db.scalar(
        select(Outbox.id)
        .where(
            Outbox.kind == "invalidation",
            Outbox.aggregate_id == root,
            Outbox.payload["generation"].as_integer() == generation,
        )
        .limit(1)
    )
    if state is None or generation > state.revision or origin is None:
        raise DomainError("INVALIDATION_STATE", "失效来源或代际不符合权威记录。", 409)
    job = db.scalar(
        select(DependencyInvalidationJob)
        .where(
            DependencyInvalidationJob.root_id == root,
            DependencyInvalidationJob.generation == generation,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None:
        job = DependencyInvalidationJob(root_id=root, generation=generation)
        db.add(job)
        db.flush()
        db.add(DependencyFrontier(job_id=job.id, node_id=root))
        db.flush()
    if job.status == "completed":
        return PropagationProgress(job.id, True, 0, 0)
    job.status = "running"
    remaining, scanned, created = batch_size, 0, 0
    while remaining:
        frontier = db.scalar(
            select(DependencyFrontier)
            .where(DependencyFrontier.job_id == job.id, DependencyFrontier.done.is_(False))
            .order_by(DependencyFrontier.id)
            .limit(1)
        )
        if frontier is None:
            break
        query = select(
            ArtifactDependency.id,
            ArtifactDependency.dependent_id,
            ArtifactDependency.dependent_namespace_id,
            ArtifactDependency.enforcement,
        ).where(
            ArtifactDependency.source_id == frontier.node_id,
            ArtifactDependency.enforcement != "provenance_only",
        )
        if frontier.edge_cursor is not None:
            query = query.where(ArtifactDependency.id > frontier.edge_cursor)
        edges = db.execute(query.order_by(ArtifactDependency.id).limit(min(remaining, 128))).all()
        if not edges:
            frontier.done = True
            remaining -= 1
            db.flush()
            continue
        for edge in edges:
            impact = db.scalar(
                select(Impact).where(
                    Impact.source_id == root,
                    Impact.consumer_id == edge.dependent_id,
                    Impact.generation == generation,
                )
            )
            if impact is None:
                db.add(
                    Impact(
                        source_id=root,
                        consumer_id=edge.dependent_id,
                        namespace_id=edge.dependent_namespace_id,
                        generation=generation,
                        enforcement=edge.enforcement,
                    )
                )
                created += 1
            elif edge.enforcement == "blocking" and impact.enforcement != "blocking":
                impact.enforcement = "blocking"
            if edge.enforcement == "blocking" and not db.scalar(
                select(DependencyFrontier.id).where(
                    DependencyFrontier.job_id == job.id,
                    DependencyFrontier.node_id == edge.dependent_id,
                )
            ):
                db.add(DependencyFrontier(job_id=job.id, node_id=edge.dependent_id))
            frontier.edge_cursor = edge.id
            scanned += 1
            remaining -= 1
            db.flush()
        more = db.scalar(
            select(ArtifactDependency.id)
            .where(
                ArtifactDependency.source_id == frontier.node_id,
                ArtifactDependency.enforcement != "provenance_only",
                ArtifactDependency.id > edges[-1].id,
            )
            .limit(1)
        )
        if more is None:
            frontier.done = True
            db.flush()
    pending = db.scalar(
        select(DependencyFrontier.id)
        .where(DependencyFrontier.job_id == job.id, DependencyFrontier.done.is_(False))
        .limit(1)
    )
    job.edges_scanned += scanned
    job.impacts_created += created
    if pending is None:
        job.status, job.completed_at = "completed", clock(db)
    db.flush()
    return PropagationProgress(job.id, pending is None, scanned, created)


def propagate(db: Session, root: str, generation: int) -> int:
    """Compatibility helper for one batch; dispatchers must use propagate_batch."""
    return propagate_batch(db, root, generation).new_impacts
