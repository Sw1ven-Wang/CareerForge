"""Deterministic location coverage and collection policy; no semantic or mastery inference."""

from typing import Any, Literal

from careerforge.domain.errors import BusinessError

CollectionPolicy = Literal["coverage", "strict_count"]
JsonObject = dict[str, Any]


def evidence_verification() -> JsonObject:
    return {
        "location": {"status": "verified", "method": "unique_literal_quote"},
        "semantic_support": {"status": "unreviewed", "source": "model_explanation"},
        "runtime": {"status": "not_observed"},
    }


def collection_policy(payload: JsonObject) -> CollectionPolicy:
    # Old tracks had a mandatory count; never silently relax a saved user choice.
    return "coverage" if payload.get("collection_policy") == "coverage" else "strict_count"


def coverage(requirements: list[JsonObject], projects: list[JsonObject]) -> JsonObject:
    rows = []
    for requirement in requirements:
        matching = [
            p
            for p in projects
            if p.get("admitted")
            and any(h["requirement_key"] == requirement["key"] for h in p.get("highlights", []))
        ]
        rows.append(
            {
                "requirement_key": requirement["key"],
                "technology": requirement["technology"],
                "depth": requirement["depth"],
                "located_project_ids": list(dict.fromkeys(p["id"] for p in matching)),
                # A-package checks location only. No model-proposed explanation is an approval.
                "semantic_support": "unreviewed" if matching else "no_evidence",
                "runtime": "not_observed",
            }
        )
    missing = [r["requirement_key"] for r in rows if not r["located_project_ids"]]
    return {
        "rows": rows,
        "missing_keys": missing,
        "located": len(rows) - len(missing),
        "total": len(rows),
        "semantic_verified": 0,
        "scope": "代码位置覆盖；解释的语义支持未独立核验，也没有运行观察。",
    }


def require_plan_coverage(
    payload: JsonObject,
    requirements: list[JsonObject],
    projects: list[JsonObject],
    selected_keys: set[str],
    approved_count: int,
    acknowledge_collection_gap: bool,
) -> JsonObject:
    selected = [r for r in requirements if r["key"] in selected_keys]
    report = coverage(selected, projects)
    if report["missing_keys"]:
        raise BusinessError(
            422,
            "REQUIREMENT_COVERAGE_MISSING",
            "所选技术缺少审核项目的代码位置依据：" + "、".join(report["missing_keys"]),
        )
    shortfall = max(0, payload["target_count"] - approved_count)
    policy = collection_policy(payload)
    if shortfall and policy == "strict_count":
        raise BusinessError(
            409,
            "PROJECT_TARGET_NOT_MET",
            f"严格数量要求：已入库 {approved_count} 个，目标 {payload['target_count']} 个。",
        )
    if shortfall and not acknowledge_collection_gap:
        raise BusinessError(
            409,
            "COLLECTION_GAP_CONFIRMATION_REQUIRED",
            f"所选目标已有代码位置依据；收集偏好仍差 {shortfall} 个，请明确确认开始当前范围。",
        )
    return {
        **report,
        "collection_policy": policy,
        "target_count": payload["target_count"],
        "approved_count": approved_count,
        "shortfall": shortfall,
        "collection_gap_acknowledged": bool(shortfall and acknowledge_collection_gap),
    }
