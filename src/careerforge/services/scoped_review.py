"""Pure, deterministic partition of unchanged facts, not an evidence authority.

Callers must validate source/read/object/permission/receipt identities first. The
partition grants no authority by itself and never rewrites a claim or an old review.
Every partial artifact is recomputed from its immutable candidate and actual new
role outputs at publication and consumption. Legacy global prose is not parsed.
"""

from __future__ import annotations

from typing import Any

from ..contracts_scoped_review_v166 import (
    PARTIAL_TITLE,
    REVIEW_READ_COMPLETION,
    REVIEW_READ_COMPLETION_V2,
    SCOPED_PUBLICATION_VERSION,
    SCOPED_REVIEW_VERSION,
    RelationScopedCoverageOutput,
    ScopedCoverageOutput,
    ScopedReviewOutput,
)
from ..errors import DomainError
from ..util import fingerprint
from .coverage_issues import validate_local_issue_indices


def scoped_review_enabled(manifest: dict[str, Any]) -> bool:
    marker = manifest.get("knowledge_review_contract_version")
    completion = manifest.get("review_read_completion")
    if marker is None and completion is None:
        return False
    if marker != SCOPED_REVIEW_VERSION or completion not in (
        REVIEW_READ_COMPLETION,
        REVIEW_READ_COMPLETION_V2,
    ):
        raise DomainError("SCOPED_REVIEW_CONTRACT", "未知或不完整的逐项审核合同。", 409)
    return True


def _unique(values: list[str], allowed: set[str], code: str) -> set[str]:
    if len(values) != len(set(values)) or not set(values) <= allowed:
        raise DomainError("SCOPED_REVIEW_BINDING", code, 422)
    return set(values)


def derive_scope(
    subject: dict[str, Any], review: ScopedReviewOutput, coverage: ScopedCoverageOutput
) -> dict[str, Any]:
    """Select only positively audited, standalone, byte-identical original facts.

    All original annotations and titles are quarantined on a partial result. A
    unit-level accepted annotation does not prove it survives removal of claims.
    """
    identity = {key: subject[key] for key in ("unit_version_id", "content_hash", "render_hash")}
    claims = {item["claim_id"]: item for item in subject["claims"]}
    elements = {item["element_id"]: item for item in subject["elements"]}
    decisions = {item.claim_id: item for item in review.claim_decisions}
    if len(claims) != len(subject["claims"]) or len(elements) != len(claims):
        raise DomainError("SCOPED_REVIEW_BINDING", "候选事实身份不唯一。", 422)
    if len(decisions) != len(review.claim_decisions) or set(decisions) != set(claims):
        raise DomainError("SCOPED_REVIEW_BINDING", "逐项审核缺少或重复断言。", 422)
    if {item["claim_id"] for item in elements.values()} != set(claims):
        raise DomainError("SCOPED_REVIEW_BINDING", "正文元素与断言不一一对应。", 422)
    _unique(coverage.uncovered_elements, set(elements), "覆盖审核引用未知正文元素。")
    _unique(review.accepted_topic_link_ids, set(subject["topic_link_ids"]), "未知主题关联。")
    _unique(review.accepted_facet_ids, set(subject["facet_ids"]), "未知维度。")
    sr, audit = review.scope_review, coverage.scope_audit
    global_reasons = []
    excluded_claims: set[str] = set()
    positive: set[str] = set()
    checks = {}
    if sr is None or audit is None:
        global_reasons.append("new_scoped_independent_review_required")
    if sr is not None:
        if any(getattr(sr, key) != value for key, value in identity.items()):
            raise DomainError("SCOPED_REVIEW_BINDING", "独立审核不属于固定候选版本。", 422)
        positive = _unique(sr.standalone_claim_ids, set(claims), "独立断言集合不属于候选。")
        if sorted(item.issue_index for item in sr.issue_scopes) != list(
            range(len(review.blocking_issues))
        ):
            raise DomainError(
                "SCOPED_REVIEW_BINDING", "每个阻断理由必须有唯一、明确的适用范围。", 422
            )
        allowed = {
            "claim": set(claims),
            "topic_link": set(subject["topic_link_ids"]),
            "facet": set(subject["facet_ids"]),
            "title": set(),
            "unit": set(),
        }
        for issue in sr.issue_scopes:
            targets = _unique(issue.target_ids, allowed[issue.scope], "阻断理由指向错误版本。")
            if issue.scope in {"claim", "topic_link", "facet"} and not targets:
                raise DomainError("SCOPED_REVIEW_BINDING", "局部问题必须指明对象。", 422)
            if issue.scope == "unit":
                global_reasons.append("unit_blocking_issue")
            if issue.scope == "claim":
                excluded_claims.update(targets)
        if sr.global_blocking_issues:
            global_reasons.append("review_global_blocker")
    if audit is not None:
        if any(
            getattr(audit, key) != value for key, value in identity.items()
        ) or audit.reviewer_output_hash != fingerprint(review.model_dump(mode="json")):
            raise DomainError(
                "SCOPED_REVIEW_BINDING", "覆盖审核与原审核输出或固定版本不一致。", 422
            )
        checks = {item.element_id: item for item in audit.elements}
        if len(checks) != len(audit.elements) or set(checks) != set(elements):
            raise DomainError("SCOPED_REVIEW_BINDING", "覆盖审核必须明确覆盖每个原正文元素。", 422)
        for identifier, item in checks.items():
            expected = elements[identifier]
            if (
                item.claim_id != expected["claim_id"]
                or item.fact_atom_id != expected["fact_atom_id"]
            ):
                raise DomainError("SCOPED_REVIEW_BINDING", "覆盖审核的断言或事实映射不一致。", 422)
        bad = {
            key
            for key, item in checks.items()
            if not item.fact_bound or not item.conditions_preserved
        }
        if (
            coverage.all_facts_bound != all(item.fact_bound for item in checks.values())
            or coverage.conditions_preserved
            != all(item.conditions_preserved for item in checks.values())
            or set(coverage.uncovered_elements) != bad
        ):
            raise DomainError("SCOPED_REVIEW_BINDING", "总覆盖结论与逐项审核矛盾。", 422)
        if audit.global_blocking_issues:
            global_reasons.append("coverage_global_blocker")
    if isinstance(coverage, RelationScopedCoverageOutput):
        validate_local_issue_indices(review, coverage.relation_audit.confirmed_local_issue_indices)
    if review.decision == "reject":
        global_reasons.append("review_rejected_unit")
    retained, quarantine = [], []
    for identifier, element in elements.items():
        cid = element["claim_id"]
        decision = decisions[cid]
        reasons = list(global_reasons)
        if cid not in positive:
            reasons.append("not_independently_approved_as_standalone")
        if cid in excluded_claims:
            reasons.append("scoped_review_blocker")
        if decision.outcome != "supported_within_scope":
            reasons.append("review_" + decision.outcome)
        if not decision.independent_reads:
            reasons.append("independent_read_missing")
        if decision.counterevidence or claims[cid]["counterevidence_ids"]:
            reasons.append("unresolved_counterevidence")
        check = checks.get(identifier)
        if check is None or not (
            check.fact_bound and check.conditions_preserved and check.standalone
        ):
            reasons.append("element_not_independently_approved_as_standalone")
        if claims[cid]["kind"] == "runtime_observation":
            reasons.append("runtime_observation_not_authorized")
        if reasons:
            quarantine.append(
                {"claim_id": cid, "element_id": identifier, "reason_codes": sorted(set(reasons))}
            )
        else:
            retained.append(element)
    payload = {
        "contract_version": SCOPED_PUBLICATION_VERSION,
        **identity,
        "source_id": subject["source_id"],
        "source_commit": subject["source_commit"],
        "source_scope_hash": subject["source_scope_hash"],
        "subject_hash": fingerprint(subject),
        "reviewer_output_hash": fingerprint(review.model_dump(mode="json")),
        "coverage_output_hash": fingerprint(coverage.model_dump(mode="json")),
        "status": "partial" if retained else "blocked",
        "retained_claim_ids": [item["claim_id"] for item in retained],
        "retained_fact_ids": [item["fact_atom_id"] for item in retained],
        "retained_element_ids": [item["element_id"] for item in retained],
        "quarantined_claims": quarantine,
        "quarantined_topic_link_ids": sorted(subject["topic_link_ids"]),
        "quarantined_facet_ids": sorted(subject["facet_ids"]),
        "annotation_reason": "unit_level_annotations_not_reapproved_for_subset",
        "title": PARTIAL_TITLE,
        "original_title_suppressed": True,
        "original_limitations_suppressed": True,
        "whole_unit_complete": False,
        "task_satisfied": False,
        "runtime_observed": False,
    }
    return {**payload, "derivation_hash": fingerprint(payload)}


SCOPED_REVIEW_INSTRUCTIONS = """
新增逐项范围合同：不降低真伪标准，不追求整单元批准率。scope_review 必须绑定 subject.scope_identity。
仅把原文、全部必要条件、证据类型在独立原文中获证且无需其他被拒命题也成立的原断言列入
standalone_claim_ids。不能在 boundary 里补一个条件就当作原断言已有该条件；需改文的断言仍不通过。
必须逐项查看 subject.standalone_publication_views 的实际单事实视图；“因此”“该行为”等措辞若依赖
被移除的前提、指代、标题或其他断言，则该原断言不独立，不能保留，不能悄悄补写前提。
为每条 blocking_issues 按原数组位置填写唯一 issue_scopes；全局或无法归属的问题使用 unit，
不能为保留部分事实而把全局反证改称局部。global_blocking_issues 如实保留整体可信性问题。
原断言有独立证据不等于相关 Topic/Facet 正确。系统部分发布会删除原标题、整体描述和全部 Topic/Facet，
仅使用中性行政标题；不要因此放行依赖这些标签、整体适用描述或其他被拒命题才成立的事实。必要条件必须已在该原断言正文或 conditions 中保留。不得改写原候选。
缺少独立原文如实 insufficient/partially_supported；不把分析读取冒充独立读取，不声称运行验证。
null scope_review 只保留原整单元严格路径，绝不授权部分发布。原任务充分性仍独立审核。
"""
SCOPED_COVERAGE_INSTRUCTIONS = """
使用新增 scope_audit 对每个原 render_ast 元素逐项检查，不改变原文。精确绑定 scope_binding，
elements 必须一一覆盖 subject.scope_elements 的 element_id/claim_id/fact_atom_id。
fact_bound 检查实际支持且证据类型没有升级；conditions_preserved 检查原命题是否包含必要限定，
standalone 检查去掉其他命题、原标题、整体描述、Topic/Facet 后该原事实是否仍可独立成立。
单事实视图里的 text 与同一 facts 项内的 conditions 是一体的原始事实；其中已存条件并未被移除，
应合并理解，不要求在 text 中逐字重复 conditions。Reviewer 后加的 boundary 不属于这些原始条件，不能补证。
额外边界说明不能代替 subject.standalone_publication_views 中原正文缺少的必要前提；含无独立指代或依赖被删除命题的
结论须 standalone=false。不得把其他单事实视图的文字自动合并为该事实的已审条件。边界说明补充了
必要限定不等于原正文已经保留该限定。全局或无法归属的语义问题写 global_blocking_issues。
总 all_facts_bound/conditions_preserved 必须与逐项 AND 一致；uncovered_elements 必须精确包含
任一上述两个检查不通过的元素。不能借局部审核消除独立 Reviewer 的全局阻断或反证。
null scope_audit 只保留原整单元严格路径，不授权部分发布。不得生成改写事实或假读回执。
"""


def mark_partial_publication_coverage(
    coverage: dict[str, Any], units: list[dict[str, Any]]
) -> dict[str, Any]:
    """Keep requested whole-scope status partial without rewriting semantic verdicts.

    Exact-question sufficiency is independently computed from retained anchors.
    It may later close that exact question through the existing answer/completion
    gates. Partial publication alone never supplies or upgrades that assessment.
    """
    if not any(unit.get("publication_scope", {}).get("status") == "partial" for unit in units):
        return coverage
    return {**coverage, "status": "partial", "complete": False, "partial_publication": True}
