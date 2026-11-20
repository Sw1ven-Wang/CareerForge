"""Admission-pinned reviewer binding checks before Coverage, never normalization.

The final derive_scope/knowledge gates remain authoritative and unchanged.
Unmarked historical runs keep their original schemas, prompts and receipt hashes.
"""

from copy import deepcopy
from typing import Any

from ..contracts_scope_binding_v166 import (
    REVIEW_SCOPE_BINDING_CONTRACT,
    REVIEW_SCOPE_BINDING_FIELD,
    BoundScopedResearchReviewOutput,
    BoundScopedReviewOutput,
)
from ..contracts_scoped_review_v166 import ScopedReviewOutput
from ..errors import DomainError
from .scoped_review import scoped_review_enabled

SCOPE_BINDING_INSTRUCTIONS = """
逐项范围对象绑定合同：unit/title 的 target_ids 必须是空数组 []，不得填写候选 unit_version_id；
候选身份已由 scope_review.unit_version_id/content_hash/render_hash 唯一绑定。
claim/topic_link/facet 的 target_ids 必须非空且无重复，分别只能取本候选 subject.claims[].claim_id、
subject.topic_links[].id、subject.facets[].id；不能混用其他种类或其他版本的 ID。
issue_scopes 必须恰好覆盖 blocking_issues 的每个索引一次。不得为通过绑定检查而删除或改写阻断理由，
不得把全局/unit 问题改成局部或接受；unit 仍阻断全部部分事实，空 target_ids 不表示不存在问题。
本合同不修补原断言，不增加证据，不改变独立性、反证、权限或发布门禁。
"""


def reviewer_identity_schema(output_type, subject, independent_reads=None):
    """Constrain identity copying, never the reviewer's substantive verdict."""
    schema = output_type.model_json_schema()
    if independent_reads is not None:
        reference = schema.get("$defs", {}).get("ReadReference", {}).get("properties", {})
        if "read_id" in reference:
            reference["read_id"]["enum"] = [row["read_id"] for row in independent_reads]
    # Object IDs are selections from this candidate, never generated text.
    allowed = {
        "claim": [row["claim_id"] for row in subject.get("claims", [])],
        "topic_link": [row["id"] for row in subject.get("topic_links", [])],
        "facet": [row["id"] for row in subject.get("facets", [])],
    }

    def constrain_array(field, values):
        if values:
            field["items"]["enum"] = values
        else:
            field["maxItems"] = 0

    for field, kind in (("accepted_topic_link_ids", "topic_link"), ("accepted_facet_ids", "facet")):
        constrain_array(schema["properties"][field], allowed[kind])
    for name, definition in list(schema.get("$defs", {}).items()):
        props = definition.get("properties", {})
        if "claim_id" in props and "outcome" in props:
            props["claim_id"]["enum"] = allowed["claim"]
        if "standalone_claim_ids" in props:
            constrain_array(props["standalone_claim_ids"], allowed["claim"])
        if {"scope", "target_ids", "issue_index"} <= props.keys():
            variants = []
            for kind in ("claim", "topic_link", "facet", "title", "unit"):
                if kind not in props["scope"].get("enum", [props["scope"].get("const")]):
                    continue
                if kind in allowed and not allowed[kind]:
                    continue
                variant = deepcopy(definition)
                variant["properties"]["scope"]["enum"] = [kind]
                constrain_array(variant["properties"]["target_ids"], allowed.get(kind, []))
                variants.append(variant)
            schema["$defs"][name] = {"anyOf": variants}
    identity = subject.get("scope_identity", {})
    for definition in schema.get("$defs", {}).values():
        properties = definition.get("properties", {})
        if {
            "unit_version_id",
            "content_hash",
            "render_hash",
            "standalone_claim_ids",
        } <= properties.keys():
            for key in ("unit_version_id", "content_hash", "render_hash"):
                if key in identity:
                    properties[key]["enum"] = [identity[key]]
    return schema


def coverage_identity_schema(
    output_type,
    subject,
    reviewer_hash,
    review=None,
    independent_reads=None,
    *,
    bounded_global_scope=False,
):
    """Bind copied identities to the candidate without prescribing audit verdicts."""
    schema = output_type.model_json_schema()
    identity = {**subject["scope_identity"], "reviewer_output_hash": reviewer_hash}
    audit = schema["$defs"]["StandaloneCoverageAudit"]["properties"]
    for key, value in identity.items():
        audit[key]["enum"] = [value]
    base = schema["$defs"]["ScopedElementCheck"]
    variants = []
    for element in subject["scope_elements"]:
        variant = deepcopy(base)
        for key in ("element_id", "claim_id", "fact_atom_id"):
            variant["properties"][key]["enum"] = [element[key]]
        variants.append(variant)
    audit["elements"]["items"] = {"anyOf": variants}
    audit["elements"]["minItems"] = len(variants)
    audit["elements"]["maxItems"] = len(variants)
    if review is not None and "RelationAudit" in schema["$defs"]:
        blocked = {
            target
            for issue in (review.scope_review.issue_scopes if review.scope_review else [])
            if issue.scope in {"topic_link", "facet"}
            for target in issue.target_ids
        }
        fields = schema["$defs"]["RelationAudit"]["properties"]
        indices = [
            issue.issue_index
            for issue in (review.scope_review.issue_scopes if review.scope_review else [])
            if issue.scope in {"topic_link", "facet"}
        ]
        if indices:
            fields["confirmed_local_issue_indices"]["items"]["enum"] = indices
        else:
            fields["confirmed_local_issue_indices"]["maxItems"] = 0
        for field, accepted in (
            ("topic_links", review.accepted_topic_link_ids),
            ("facets", review.accepted_facet_ids),
        ):
            eligible = sorted(set(accepted) - blocked)
            if not eligible:
                fields[field]["maxItems"] = 0
            else:
                item = deepcopy(schema["$defs"]["AnnotationApproval"])
                item["properties"]["annotation_id"]["enum"] = eligible
                item["properties"]["claim_ids"]["items"]["enum"] = [
                    c.claim_id for c in review.claim_decisions if c.independent_reads
                ]
                item["properties"]["independent_read_ids"]["items"]["enum"] = [
                    r["read_id"] for r in (independent_reads or [])
                ]
                fields[field]["items"] = item
    if bounded_global_scope and review is not None:
        from .coverage_issues import declared_global_issues

        base_audit = schema["$defs"]["StandaloneCoverageAudit"]
        local = deepcopy(base_audit)
        known = declared_global_issues(review)
        blockers = local["properties"]["global_blocking_issues"]
        if known:
            blockers["items"]["enum"] = known
        else:
            blockers["maxItems"] = 0
        global_audit = deepcopy(base_audit)
        for element in global_audit["properties"]["elements"]["items"]["anyOf"]:
            element["properties"]["fact_bound"]["enum"] = [False]
        schema["$defs"]["StandaloneCoverageAudit"] = {"anyOf": [local, global_audit]}
    return schema


def scope_binding_enabled(manifest: dict[str, Any]) -> bool:
    if REVIEW_SCOPE_BINDING_FIELD not in manifest:
        return False
    if manifest[REVIEW_SCOPE_BINDING_FIELD] != REVIEW_SCOPE_BINDING_CONTRACT:
        raise DomainError(
            "REVIEW_SCOPE_BINDING_CONTRACT", "未知逐项对象绑定合同，禁止静默升级或重放。", 409
        )
    if not scoped_review_enabled(manifest):
        raise DomainError(
            "REVIEW_SCOPE_BINDING_CONTRACT", "逐项对象绑定必须附属于已冻结的逐项审核合同。", 409
        )
    return True


def validate_reviewer_bindings(
    subject: dict[str, Any],
    review: ScopedReviewOutput | BoundScopedReviewOutput | BoundScopedResearchReviewOutput,
) -> None:
    """Mirror existing ID/shape checks early using the actual expanded candidate.

    This is called only for the new admission marker. It does not accept facts,
    infer issue locality, edit outputs, or replace the final deterministic gate.
    """

    def unique(values: list[str], allowed: set[str], message: str) -> set[str]:
        if len(values) != len(set(values)) or not set(values) <= allowed:
            raise DomainError("SCOPED_REVIEW_BINDING", message, 422)
        return set(values)

    claims = {item["claim_id"] for item in subject["claims"]}
    topics = {item["id"] for item in subject["topic_links"]}
    facets = {item["id"] for item in subject["facets"]}
    decisions = unique(
        [item.claim_id for item in review.claim_decisions], claims, "逐项审核重复或引用未知断言。"
    )
    if decisions != claims:
        raise DomainError("SCOPED_REVIEW_BINDING", "逐项审核缺少候选断言。", 422)
    unique(review.accepted_topic_link_ids, topics, "审核引用未知或重复主题关联。")
    unique(review.accepted_facet_ids, facets, "审核引用未知或重复维度。")
    scope = review.scope_review
    if scope is None:
        return  # Existing strict whole-unit path; never authority for partial admission.
    if any(
        getattr(scope, key) != subject["scope_identity"][key]
        for key in ("unit_version_id", "content_hash", "render_hash")
    ):
        raise DomainError("SCOPED_REVIEW_BINDING", "审核不属于固定候选版本。", 422)
    unique(scope.standalone_claim_ids, claims, "独立断言集合引用未知或重复候选断言。")
    if sorted(item.issue_index for item in scope.issue_scopes) != list(
        range(len(review.blocking_issues))
    ):
        raise DomainError("SCOPED_REVIEW_BINDING", "每个阻断理由必须恰有一个范围。", 422)
    allowed = {
        "claim": claims,
        "topic_link": topics,
        "facet": facets,
        "title": set(),
        "unit": set(),
    }
    for issue in scope.issue_scopes:
        unique(issue.target_ids, allowed[issue.scope], "阻断目标不属于本候选或该范围类型。")
        if issue.scope in {"claim", "topic_link", "facet"} and not issue.target_ids:
            raise DomainError("SCOPED_REVIEW_BINDING", "局部问题必须指定本候选对象。", 422)
