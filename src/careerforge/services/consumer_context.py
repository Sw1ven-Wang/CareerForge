"""Explicit atomic-consumption scope; neither a semantic judge nor a dependency guess.

A complete publication is allowed to contain contextual facts. Only positive
independent standalone review authorizes removing its other reviewed context.
Unmarked historical packs keep their frozen contract; new admissions opt in.
"""

from __future__ import annotations

from typing import Any

from ..errors import DomainError

CONSUMER_CONTEXT_CONTRACT = "reviewed-standalone-consumer-1.6.6-v1"
CONTEXT_EVIDENCE_PROJECTION_CONTRACT = "complete-standalone-fact-support-1.6.6-v1"


def validate_context_contract(value: str | None) -> None:
    if value not in {None, CONSUMER_CONTEXT_CONTRACT}:
        raise DomainError("CONSUMER_CONTEXT_CONTRACT", "未知的事实独立使用合同。", 422)


def standalone_fact_ids(pack: dict[str, Any]) -> set[str]:
    """Check a server-derived witness; never infer approval from absent metadata.

    The caller must obtain this pack from publication_pack, or compare it with
    that authority before using it. A witness by itself grants no permissions.
    Subprojections may retain the complete witness, but not foreign identities.
    """
    context = pack.get("consumer_context")
    if not isinstance(context, dict) or context.get("contract") != CONSUMER_CONTEXT_CONTRACT:
        raise DomainError("CONSUMER_CONTEXT_CONTRACT", "事实缺少已知的独立使用合同。", 422)
    if any(context.get(k) != pack.get(k) for k in ("publication_id", "unit_version_id")):
        raise DomainError("CONSUMER_CONTEXT_BINDING", "独立使用范围不属于该发布版本。", 422)
    rows = context.get("facts")
    if not isinstance(rows, dict):
        raise DomainError("CONSUMER_CONTEXT_BINDING", "事实独立使用范围缺失。", 422)
    result = set()
    for fact in pack["facts"]:
        row = rows.get(fact["fact_atom_id"])
        if (
            not isinstance(row, dict)
            or row.get("claim_id") != fact["claim_id"]
            or type(row.get("standalone")) is not bool
        ):
            raise DomainError("CONSUMER_CONTEXT_BINDING", "事实与独立审核范围不一致。", 422)
        if row["standalone"]:
            result.add(fact["fact_atom_id"])
    return result


def require_standalone_facts(pack: dict[str, Any], fact_ids: set[str]) -> None:
    if not fact_ids or not fact_ids <= standalone_fact_ids(pack):
        raise DomainError(
            "FACT_CONTEXT_REQUIRED", "所选事实未获独立使用审核；请保留原单元或重新审核。", 422
        )


def pack_context_contract(pack: dict[str, Any]) -> str | None:
    if "consumer_context" not in pack:
        return None
    context = pack["consumer_context"]
    marker = context.get("contract") if isinstance(context, dict) else "invalid"
    validate_context_contract(marker)
    if marker is None:
        raise DomainError("CONSUMER_CONTEXT_CONTRACT", "事实独立使用合同缺失。", 422)
    return marker


def context_options(marker: str | None) -> dict[str, str]:
    validate_context_contract(marker)
    return {"consumer_context_contract": marker} if marker else {}


def require_frozen_standalone(frozen: dict, pack: dict, fact_ids: set[str]) -> None:
    marker = frozen.get("consumer_context_contract")
    validate_context_contract(marker)
    if pack_context_contract(pack) != marker:
        raise DomainError("CONSUMER_CONTEXT_CONTRACT", "冻结请求与知识使用合同不一致。", 422)
    if marker:
        require_standalone_facts(pack, fact_ids)
