"""Typed issue catalog shared by coverage admission and the publication gate."""

from ..errors import DomainError

COVERAGE_ISSUES_CONTRACT = "typed-coverage-issue-catalog-v1"
COVERAGE_ISSUES_INSTRUCTIONS = """
coverage_issue_catalog 是原审核问题的类型化索引表，不是新的审核结论。
confirmed_local_issue_indices 只能选择 eligible_local_issues 中的 original_issue_index，
不能使用列表位置、claim_id、element_id，不能重新从 0 编号。
claim/title/unit 问题永远不能填入此列表；它们仍按原审核与事实覆盖规则阻断。
每个编号最多出现一次。没有可确认的主题/维度局部问题时返回 []。
确认某问题仅影响关联不代表批准该关联，也不撤销原阻断理由；保持独立判断。
"""


def coverage_issue_catalog(review):
    scopes = review.scope_review.issue_scopes if review.scope_review else []
    rows = [{
        "original_issue_index": issue.issue_index,
        "scope": issue.scope,
        "target_ids": list(issue.target_ids),
        "reason": review.blocking_issues[issue.issue_index],
    } for issue in scopes]
    return {
        "contract": COVERAGE_ISSUES_CONTRACT,
        "eligible_local_issues": [r for r in rows if r["scope"] in {"topic_link", "facet"}],
        "non_local_issues": [r for r in rows if r["scope"] not in {"topic_link", "facet"}],
        "empty_selection_allowed": True,
        "selection_does_not_approve_annotation": True,
    }


def validate_local_issue_indices(review, confirmed):
    scopes = review.scope_review.issue_scopes if review.scope_review else []
    permitted = {i.issue_index for i in scopes if i.scope in {"topic_link", "facet"}}
    if any(type(i) is not int for i in confirmed) or len(set(confirmed)) != len(confirmed):
        raise DomainError("SCOPED_REVIEW_BINDING", "局部审核编号必须为不重复的整数。", 422)
    if not set(confirmed) <= permitted:
        raise DomainError(
            "SCOPED_REVIEW_BINDING",
            "局部审核编号包含非主题/维度问题或未知编号；必须使用原审核的主题/维度问题编号。",
            422,
        )


def declared_global_issues(review):
    scope = review.scope_review
    return list(dict.fromkeys([
        *(scope.global_blocking_issues if scope else []),
        *(review.blocking_issues[i.issue_index] for i in (scope.issue_scopes if scope else []) if i.scope == "unit"),
    ]))


def validate_global_scope(review, coverage):
    audit = coverage.scope_audit
    if audit is None:
        return
    new = set(audit.global_blocking_issues) - set(declared_global_issues(review))
    if new and any(e.fact_bound for e in audit.elements):
        raise DomainError("COVERAGE_SCOPE_ESCALATION", "新增全局反证与独立获证事实矛盾；局部问题必须指向对应元素或关联。", 422)
