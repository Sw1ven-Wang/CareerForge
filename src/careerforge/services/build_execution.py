"""Frozen, bounded build planning. Time assumptions are estimates, never guarantees."""

from __future__ import annotations

from math import ceil
from types import SimpleNamespace
from typing import Any

from ..errors import DomainError

BUILD_EXECUTION_CONTRACT = "bounded-build-execution-1.6.4"
UNLIMITED_BUILD_WALL_TIME = "unlimited-build-time-v1"
UNLIMITED_BUILD_EXECUTION = "bounded-calls-unlimited-build-time-v1"
QUOTE_CORRECTION_CONTRACT = {"version": "bounded-quote-correction-1.6.4", "max_attempts": 1}
COMMIT_RESERVE_SECONDS = 60
EVIDENCE_RETRY_CONTRACT = "same-snapshot-evidence-retry-v1"
MECHANISM_DISCOVERY_CONTRACT = "snapshot-mechanism-discovery-v1"
STAGED_MECHANISM_DISCOVERY_CONTRACT = "snapshot-mechanism-discovery-v2"
CONNECTED_MECHANISM_DISCOVERY_CONTRACT = "snapshot-mechanism-discovery-v3"
STAGED_MECHANISM_DISCOVERY_CONTRACTS = {
    STAGED_MECHANISM_DISCOVERY_CONTRACT,
    CONNECTED_MECHANISM_DISCOVERY_CONTRACT,
}
MECHANISM_DISCOVERY_CONTRACTS = {
    MECHANISM_DISCOVERY_CONTRACT,
    *STAGED_MECHANISM_DISCOVERY_CONTRACTS,
}


def plan_execution(body: Any, embeddings: bool) -> dict[str, Any]:
    """Choose one fixed shape from explicit call and configurable wall-clock assumptions."""
    units = body.max_units_per_project
    search_needed = not body.source_id and not body.repository_url
    selection_calls = int(
        bool(search_needed and getattr(body, "candidate_selection_version", None))
    )
    discovery_calls = int(bool(getattr(body, "discover_mechanisms", False)))
    fixed = (
        1
        + int(bool(body.jd_text or getattr(body, "jd_revision_id", None)))
        + int(search_needed)
        + selection_calls
        + discovery_calls
    )
    wall_calls = max(
        0, (body.duration_minutes * 60 - COMMIT_RESERVE_SECONDS) // body.planning_call_seconds
    )
    unlimited = getattr(body, "build_wall_time_contract", None) == UNLIMITED_BUILD_WALL_TIME
    ceiling = body.call_cap if unlimited else min(body.call_cap, wall_calls)
    auto_retry = getattr(body, "auto_evidence_retry", False)
    attempts = 2 if auto_retry else 1
    index_calls = attempts * units if embeddings else 0
    # Reserve one quote correction per analysis final/source and review final/unit.
    review_limit = (
        6 if getattr(body, "reviewer_round_policy", None) == "budgeted-six-rounds-v1" else 3
    )
    options = [
        (a, r)
        for a in range(1, 5)
        for r in range(1, review_limit + 1)
        if attempts * (a + 2 + (r + 3) * units) + index_calls + fixed <= ceiling
    ]
    minimum_calls = attempts * (3 + 4 * units) + index_calls + fixed
    minimum_minutes = ceil(
        (minimum_calls * body.planning_call_seconds + COMMIT_RESERVE_SECONDS) / 60
    )
    if not options:
        code = "BUILD_BUDGET" if body.call_cap < minimum_calls else "BUILD_TIME_BUDGET"
        raise DomainError(
            code,
            f"当前参数至少需 {minimum_calls} 次调用；按 planning_call_seconds="
            f"{body.planning_call_seconds} 的规划假设，duration_minutes 至少需 {minimum_minutes}。"
            "可调整 call_cap、duration_minutes、planning_call_seconds 或 max_units_per_project；"
            "规划时间不是完成保证，不会跳过独立审核、事实覆盖或任务充分性审核。",
            422,
        )
    a, r = max(options, key=lambda value: (min(value), sum(value), value[0]))
    per_source = attempts * (a + 2 + (r + 3) * units) + index_calls
    requested_sources = 1 if body.source_id or body.repository_url else body.max_projects
    sources = min(requested_sources, (ceiling - fixed) // per_source)
    search_calls = (
        min(
            4,
            ceiling
            - sources * per_source
            - 1
            - int(bool(body.jd_text or getattr(body, "jd_revision_id", None)))
            - selection_calls,
        )
        if search_needed
        else 0
    )
    fixed = (
        1
        + int(bool(body.jd_text or getattr(body, "jd_revision_id", None)))
        + search_calls
        + selection_calls
        + discovery_calls
    )
    planned_calls = sources * per_source + fixed
    return {
        "version": UNLIMITED_BUILD_EXECUTION if unlimited else BUILD_EXECUTION_CONTRACT,
        **(
            {
                "build_wall_time_contract": UNLIMITED_BUILD_WALL_TIME,
                "build_duration_limit_seconds": None,
                "deadline_basis": "funding_expires_at",
            }
            if unlimited
            else {}
        ),
        "duration_minutes": body.duration_minutes,
        "planning_call_seconds": body.planning_call_seconds,
        "time_assumption": "configurable_per_call_estimate_not_a_guarantee",
        "commit_reserve_seconds": COMMIT_RESERVE_SECONDS,
        "call_capacity": ceiling,
        "authorized_call_cap": body.call_cap,
        "planned_call_ceiling": planned_calls,
        "estimated_seconds": planned_calls * body.planning_call_seconds + COMMIT_RESERVE_SECONDS,
        "minimum_call_count": minimum_calls,
        "minimum_estimated_seconds": minimum_calls * body.planning_call_seconds
        + COMMIT_RESERVE_SECONDS,
        "source_limit": sources,
        "unit_limit_per_source": units,
        "analysis_rounds": a,
        "reviewer_rounds": r,
        "analysis_calls_per_source": a + 2,
        "review_calls_per_unit": r + 3,
        "embedding_calls_per_source": index_calls,
        "fixed_call_reserve": fixed,
        "extraction_call_reserve": int(bool(body.jd_text or getattr(body, "jd_revision_id", None))),
        "search_call_reserve": search_calls,
        "sufficiency_call_reserve": 1,
        "quote_correction_calls_per_source": 1 + units,
        **({"evidence_retry_call_reserve": a + 2 + (r + 3) * units} if auto_retry else {}),
        **({"candidate_selection_call_reserve": selection_calls} if selection_calls else {}),
        **({"mechanism_discovery_call_reserve": discovery_calls} if discovery_calls else {}),
    }


def validate_execution_manifest(manifest: dict[str, Any], call_cap: int) -> dict[str, Any] | None:
    """Reject forged/inconsistent new plans; unmarked persisted runs keep their old path."""
    marker = manifest.get("execution_contract_version")
    if marker is None:
        if manifest.get("build_wall_time_contract") is not None:
            raise DomainError("BUILD_EXECUTION_CONTRACT", "构建时限选择缺少配对执行合同。", 409)
        return None
    unlimited = marker == UNLIMITED_BUILD_EXECUTION
    wall_time = manifest.get("build_wall_time_contract")
    if (
        marker not in {BUILD_EXECUTION_CONTRACT, UNLIMITED_BUILD_EXECUTION}
        or (wall_time == UNLIMITED_BUILD_WALL_TIME) != unlimited
        or wall_time not in {None, UNLIMITED_BUILD_WALL_TIME}
    ):
        raise DomainError("BUILD_EXECUTION_CONTRACT", "未知构建执行合同，不自动升级或重放。", 409)
    plan = manifest.get("execution_plan")
    correction = manifest.get("quote_correction_contract")
    if (
        not isinstance(plan, dict)
        or not isinstance(correction, dict)
        or correction != QUOTE_CORRECTION_CONTRACT
        or type(correction.get("max_attempts")) is not int
    ):
        raise DomainError("BUILD_EXECUTION_CONTRACT", "构建执行计划或引用纠正预算不完整。", 409)
    try:
        required = (
            "duration_minutes",
            "planning_call_seconds",
            "commit_reserve_seconds",
            "call_capacity",
            "authorized_call_cap",
            "planned_call_ceiling",
            "estimated_seconds",
            "source_limit",
            "unit_limit_per_source",
            "analysis_rounds",
            "reviewer_rounds",
            "analysis_calls_per_source",
            "review_calls_per_unit",
            "embedding_calls_per_source",
            "fixed_call_reserve",
            "sufficiency_call_reserve",
            "quote_correction_calls_per_source",
            "extraction_call_reserve",
            "search_call_reserve",
            "minimum_call_count",
            "minimum_estimated_seconds",
        )
        if any(type(plan[key]) is not int for key in required):
            raise ValueError
        selection_calls = plan.get("candidate_selection_call_reserve", 0)
        if type(selection_calls) is not int or selection_calls not in {0, 1}:
            raise ValueError
        discovery = manifest.get("mechanism_discovery_contract")
        discovery_calls = plan.get("mechanism_discovery_call_reserve", 0)
        if (
            discovery not in {None, *MECHANISM_DISCOVERY_CONTRACTS}
            or type(discovery_calls) is not int
            or discovery_calls != int(discovery in MECHANISM_DISCOVERY_CONTRACTS)
        ):
            raise ValueError
        a, r, units = (
            plan["analysis_rounds"],
            plan["reviewer_rounds"],
            plan["unit_limit_per_source"],
        )
        auto_retry = manifest.get("evidence_retry_contract") == EVIDENCE_RETRY_CONTRACT
        attempts = 2 if auto_retry else 1
        minimum = (
            attempts * (3 + 4 * units)
            + plan["embedding_calls_per_source"]
            + 1
            + plan["extraction_call_reserve"]
            + int(plan["search_call_reserve"] > 0)
            + selection_calls
            + discovery_calls
        )
        total = (
            plan["source_limit"]
            * (attempts * (a + 2 + (r + 3) * units) + plan["embedding_calls_per_source"])
            + plan["fixed_call_reserve"]
        )
        if not (
            plan["version"] == marker
            and plan.get("build_wall_time_contract") == wall_time
            and (
                not unlimited
                or (
                    "build_duration_limit_seconds" in plan
                    and plan["build_duration_limit_seconds"] is None
                    and plan.get("deadline_basis") == "funding_expires_at"
                )
            )
            and plan["time_assumption"] == "configurable_per_call_estimate_not_a_guarantee"
            and 1 <= plan["duration_minutes"] <= 10080
            and 1 <= plan["planning_call_seconds"] <= 3600
            and plan["commit_reserve_seconds"] == COMMIT_RESERVE_SECONDS
            and (manifest.get("evidence_retry_contract") is None or auto_retry)
            and ("evidence_retry_call_reserve" in plan) == auto_retry
            and (
                not auto_retry
                or (
                    units == plan["source_limit"] == 1
                    and plan["evidence_retry_call_reserve"] == a + 2 + (r + 3) * units
                )
            )
            and 1 <= a <= 4
            and 1
            <= r
            <= (6 if manifest.get("reviewer_round_policy") == "budgeted-six-rounds-v1" else 3)
            and 1 <= units <= 5
            and 1 <= plan["source_limit"] <= 5
            and plan["embedding_calls_per_source"] in {0, attempts * units}
            and plan["extraction_call_reserve"] in {0, 1}
            and 0 <= plan["search_call_reserve"] <= 4
            and plan["fixed_call_reserve"]
            == 1
            + plan["extraction_call_reserve"]
            + plan["search_call_reserve"]
            + selection_calls
            + discovery_calls
            and plan["sufficiency_call_reserve"] == 1
            and plan["analysis_calls_per_source"] == a + 2
            and plan["review_calls_per_unit"] == r + 3
            and plan["quote_correction_calls_per_source"] == 1 + units
            and plan["minimum_call_count"] == minimum
            and plan["minimum_estimated_seconds"]
            == minimum * plan["planning_call_seconds"] + COMMIT_RESERVE_SECONDS
            and plan["planned_call_ceiling"] == total
            and plan["estimated_seconds"]
            == total * plan["planning_call_seconds"] + COMMIT_RESERVE_SECONDS
            and (unlimited or plan["estimated_seconds"] <= plan["duration_minutes"] * 60)
            and total <= plan["call_capacity"] <= 100
            and 1 <= plan["authorized_call_cap"] <= 100
            and plan["call_capacity"]
            == (
                plan["authorized_call_cap"]
                if unlimited
                else min(
                    plan["authorized_call_cap"],
                    (plan["duration_minutes"] * 60 - COMMIT_RESERVE_SECONDS)
                    // plan["planning_call_seconds"],
                )
            )
            and manifest["investigation_contracts"]["analysis"]["max_rounds"] == a
            and manifest["investigation_contracts"]["reviewer"]["max_rounds"] == r
        ):
            raise ValueError
        # Child caps are role allocations, not the root's complete capacity.
        if "duration_minutes" in manifest and not (
            manifest["duration_minutes"] == plan["duration_minutes"]
            and manifest["planning_call_seconds"] == plan["planning_call_seconds"]
            and plan["call_capacity"] <= call_cap
            and plan["authorized_call_cap"] == call_cap
            and manifest.get("auto_evidence_retry", False) is auto_retry
            and manifest.get("discover_mechanisms", False) is bool(discovery_calls)
            and (
                not auto_retry
                or bool(manifest.get("source_id"))
                or (
                    manifest.get("repository_retry_contract")
                    == "single-repository-fixed-snapshot-retry-v1"
                    and bool(manifest.get("repository_url"))
                    and manifest.get("max_projects") == 1
                    and plan["source_limit"] == 1
                )
            )
            and manifest["max_units_per_project"] == units
            and plan["source_limit"] <= manifest["max_projects"]
        ):
            raise ValueError
        if (
            "duration_minutes" in manifest
            and plan_execution(
                SimpleNamespace(**manifest), bool(plan["embedding_calls_per_source"])
            )
            != plan
        ):
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError):
        raise DomainError(
            "BUILD_EXECUTION_CONTRACT", "冻结执行计划与调查或调用预算不一致。", 409
        ) from None
    return plan
