"""Versioned model-visible query grammar and the existing public-search guard.

Requirement logic is not query-string syntax. New admissions expose this contract
and validate before local-first discovery. Unmarked/previously admitted runs keep
their original wire requests; the GitHub adapter retains its existing rejection.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field

from ..contracts import Contract, QueryPlan, QuerySpec
from ..errors import DomainError

RESEARCH_QUERY_CONTRACT = "public-research-query-1.6.6"
QUERY_MIN_LENGTH = 2
QUERY_MAX_LENGTH = 180
QUERY_CHANNELS = ("repository", "code", "documentation", "issues", "pull_requests", "releases")
# Preserve the existing adapter predicate exactly, including case and boundaries.
PUBLIC_QUERY_DENY_PATTERN = r"(?:^|\s)(?:OR|NOT)(?:\s|$)|(?:is|visibility):(?:private|internal)"
QUERY_CONTRACT_INSTRUCTIONS = (
    "\nquery_contract 是实际检索接口的语法合同，planner 和 reflector 均必须遵守。"
    "JD 的 AND/OR/NOT 与可选、否定关系是目标语义，不是 query 字符串的语法。"
    "query 长度须为 2 至 180 字符，通道只能取 query_contract.allowed_channels。"
    "query 禁止作为独立布尔操作符的 OR/NOT（忽略大小写），"
    "禁止 is:private、is:internal、visibility:private、visibility:internal。"
    "OR 替代关系可在预算内选择一个分支作一条查询；保留其他分支为未检索或未覆盖，"
    "不能把 OR 改成全部必需，也不能把 preferred/bonus 或 not_required 升格为硬目标。"
    "保留完整目标限定于 question/research_task，不把私人岗位原文复制到 query。"
    "外部 code/documentation 搜索仍须鉴权，公开范围由适配器强制；"
    "本地命中不证明外部API可用或源码事实成立。无合法且有依据的查询可返回空 queries；"
    "不得为规避合同编造仓库、改写既有查询或声称验证与覆盖。"
)


def validate_public_search_query(query: str, channel: str) -> None:
    """Same non-I/O predicate used by the historical GitHub search adapter."""
    if not QUERY_MIN_LENGTH <= len(query) <= QUERY_MAX_LENGTH or channel not in QUERY_CHANNELS:
        raise DomainError("QUERY", "GitHub 查询长度或通道无效。", 422)
    if re.search(PUBLIC_QUERY_DENY_PATTERN, query, re.I):
        raise DomainError(
            "QUERY_SCOPE", "研究通道仅允许明确公开的查询，不能用布尔表达式绕过范围。", 422
        )


def query_contract_context(manifest: dict[str, Any]) -> dict[str, Any]:
    """Only an admission-frozen marker permits changing the model wire request."""
    marker = manifest.get("research_query_contract")
    if marker is None:
        return {}
    from .research_planning import RESEARCH_PLANNING_CONTRACT

    if (
        marker != RESEARCH_QUERY_CONTRACT
        or manifest.get("research_planning_contract") != RESEARCH_PLANNING_CONTRACT
    ):
        raise DomainError("RESEARCH_QUERY_CONTRACT", "未知或缺少固定规划范围的查询合同。", 409)
    plan_type = query_plan_type(manifest)
    return {
        "query_contract": {
            "version": RESEARCH_QUERY_CONTRACT,
            "min_length": QUERY_MIN_LENGTH,
            "max_length": QUERY_MAX_LENGTH,
            "allowed_channels": list(
                PUBLIC_CHANNELS if plan_type is PublicQueryPlan else QUERY_CHANNELS
            ),
            **({"code_search_available": False} if plan_type is PublicQueryPlan else {}),
            "disallowed_boolean_operators": ["OR", "NOT"],
            "disallowed_scope_selectors": [
                "is:private",
                "is:internal",
                "visibility:private",
                "visibility:internal",
            ],
            "case_insensitive_rejection_pattern": PUBLIC_QUERY_DENY_PATTERN,
            "scope": "public_only",
            "external_code_documentation_requires_authentication": True,
            "max_queries_per_plan": 3,
            "requirement_logic_is_not_query_syntax": True,
            "invalid_queries_are_not_rewritten_or_retried": True,
        }
    }


PUBLIC_CHANNELS = ("repository", "issues", "pull_requests", "releases")


class PublicQuerySpec(QuerySpec):
    channel: Literal["repository", "issues", "pull_requests", "releases"] = "repository"


class PublicQueryPlan(Contract):
    queries: list[PublicQuerySpec] = Field(default_factory=list, max_length=3)
    stop_reason: str | None = None


def query_plan_type(manifest):
    capabilities = manifest.get("research_query_capabilities")
    if capabilities is None:
        return QueryPlan
    if capabilities != {
        "contract": "github-public-capabilities-v1",
        "channels": list(PUBLIC_CHANNELS),
    }:
        raise DomainError("RESEARCH_QUERY_CAPABILITIES", "未知的冻结检索能力。", 409)
    return PublicQueryPlan
