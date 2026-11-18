"""Shared frontier ordering and counterfactual replay of frozen search observations.

Replay evaluates scheduling only. It cannot establish benefits of new queries or an
LLM query-language change without observations for that different intervention.
"""

from __future__ import annotations

from typing import Any

from ..errors import DomainError
from .policies import validate_policy


def frontier_key(node: dict[str, Any], policy: dict[str, Any], covered_topics: set[str]) -> tuple:
    preferred = set(policy.get("preferred_topic_keys", []))
    topics = set(node.get("topics", []))
    stable = (int(node.get("depth", 0)), str(node.get("created_at", "")), str(node.get("id", "")))
    if policy.get("branch_order") == "breadth_first":
        return stable
    return (not bool(topics & preferred), not bool(topics - covered_topics), *stable)


def replay_frontier(policy: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Spend the same frozen HTTP budget over a complete, observed logical tree."""
    policy = validate_policy("research_heuristic", policy)
    nodes = data.get("nodes", [])
    budget = data.get("request_budget")
    if (
        not isinstance(nodes, list)
        or not 1 <= len(nodes) <= 200
        or type(budget) is not int
        or not 1 <= budget <= 200
    ):
        raise DomainError("RESEARCH_REPLAY_SCHEMA", "研究回放缺少有界节点和请求预算。", 422)
    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        required = {"id", "parent_id", "depth", "topics", "repository_ids", "cost"}
        if (
            not isinstance(node, dict)
            or not required <= node.keys()
            or not isinstance(node["id"], str)
            or node["id"] in by_id
            or type(node["cost"]) is not int
            or not 1 <= node["cost"] <= 30
            or type(node["depth"]) is not int
            or node["depth"] < 1
            or not isinstance(node["topics"], list)
            or not isinstance(node["repository_ids"], list)
        ):
            raise DomainError("RESEARCH_REPLAY_SCHEMA", "冻结研究节点合同无效。", 422)
        by_id[node["id"]] = node
    for node in nodes:
        parent = node["parent_id"]
        if parent is None:
            if node["depth"] != 1:
                raise DomainError("RESEARCH_REPLAY_SCHEMA", "顶层查询深度必须为1。", 422)
        elif parent not in by_id or node["depth"] != by_id[parent]["depth"] + 1:
            raise DomainError("RESEARCH_REPLAY_SCHEMA", "父节点不存在、跨树或深度错误。", 422)
    visited: set[str] = set()
    covered: set[str] = set()
    repos: set[str] = set()
    trace: list[str] = []
    used = 0
    while True:
        available = [
            n
            for n in nodes
            if n["id"] not in visited
            and (n["parent_id"] is None or n["parent_id"] in visited)
            and used + n["cost"] <= budget
        ]
        if not available:
            break
        node = min(available, key=lambda n: frontier_key(n, policy, covered))
        visited.add(node["id"])
        trace.append(node["id"])
        used += node["cost"]
        repos.update(str(repo) for repo in node["repository_ids"])
        if node["repository_ids"]:
            covered.update(node["topics"])
    return {
        "visited_nodes": trace,
        "repository_ids": sorted(repos),
        "requests_used": used,
        "request_budget": budget,
        "scope": "frozen_observations_frontier_only",
    }


async def execute_query_replay(
    gateway: Any,
    run: tuple[str, int, str],
    prefix: str,
    policy: dict[str, Any],
    case: dict[str, Any],
    *,
    model_cap: int,
    request_cap: int,
    max_nodes: int,
    max_depth: int,
) -> dict[str, Any]:
    """Execute real planner/reflector calls against a recorded query response corpus.

    This performs no network search. An unseen query is missing counterfactual data,
    not an empty result and never evidence that the candidate is worse or better.
    Frozen response costs are charged identically to both arms, including cached and
    locally observed searches. The exact same query and channel must have an audited
    observation in the corpus.
    """
    import re

    from ..contracts import QueryPlan
    from ..util import fingerprint

    policy = validate_policy("research_heuristic", policy)
    corpus = {entry["query_key"]: entry for entry in case["corpus"]}
    nodes: list[dict[str, Any]] = []
    seen = set()
    next_id = 0
    model_calls = 0
    request_used = 0
    repositories: set[str] = set()
    covered: set[str] = set()
    unobserved: list[dict[str, str]] = []
    trace = []
    cost_unverified = False
    incomplete = False

    def expand(plan: QueryPlan, parent_id: str | None, depth: int) -> None:
        nonlocal next_id
        if depth > max_depth:
            return
        for query in plan.queries:
            key = fingerprint(
                {
                    "q": query.query.lower().strip(),
                    "topics": sorted(query.topic_keys),
                    "channel": query.channel,
                }
            )
            if key in seen or len(nodes) >= max_nodes - 1:
                continue
            # Public query scope is unchanged even though replay makes no HTTP calls.
            if re.search(
                r"(?:^|\s)(?:OR|NOT)(?:\s|$)|(?:is|visibility):(?:private|internal)",
                query.query,
                re.I,
            ):
                raise DomainError("RESEARCH_QUERY_SCOPE", "候选查询试图扩大到未授权来源。", 403)
            if not set(query.topic_keys) <= set(case["topic_keys"]):
                raise DomainError("RESEARCH_QUERY_SCOPE", "候选查询扩展到本题未授权主题。", 403)
            seen.add(key)
            next_id += 1
            nodes.append(
                {
                    "id": str(next_id),
                    "parent_id": parent_id,
                    "depth": depth,
                    "question": query.question,
                    "query": query.query,
                    "channel": query.channel,
                    "topics": query.topic_keys,
                    "created_at": f"{next_id:05d}",
                    "visited": False,
                }
            )

    reply = await gateway.call(
        *run,
        prefix + "research.initial_plan",
        "planner",
        "将问题分解为至多3个针对GitHub项目的不同检索问题。不要编造仓库。"
        "查询关注实现线索，不把README宣传当作源码事实。channel可为repository/code/documentation/issues/pull_requests/releases；代码类搜索需鉴权。空queries表示没有合理问题。",
        {
            "question": case["question"],
            "topic_keys": case["topic_keys"],
            "remaining_nodes": max_nodes - 1,
            "research_heuristic": policy,
        },
        QueryPlan,
        data_classes=["learning_state"],
    )
    model_calls += 1
    expand(QueryPlan.model_validate(reply.structured), None, 1)
    while pending := [n for n in nodes if not n["visited"]]:
        node = min(pending, key=lambda n: frontier_key(n, policy, covered))
        node["visited"] = True
        key = fingerprint({"query": node["query"], "channel": node["channel"]})
        entry = corpus.get(key)
        if entry is None:
            unobserved.append({"query": node["query"], "channel": node["channel"]})
            trace.append(
                {"node_id": node["id"], "query_key": key, "status": "unobserved_counterfactual"}
            )
            continue
        cost = entry.get("request_cost")
        if type(cost) is not int or cost < 0:
            cost_unverified = True
            trace.append(
                {"node_id": node["id"], "query_key": key, "status": "request_cost_unverified"}
            )
            continue
        if request_used + cost > request_cap:
            trace.append(
                {
                    "node_id": node["id"],
                    "query_key": key,
                    "status": "frozen_request_budget_boundary",
                }
            )
            continue
        request_used += cost
        observation = entry["observation"]
        candidates = observation.get("candidates", [])
        discovered = {str(c.get("repository_id", c.get("name"))) for c in candidates}
        repositories.update(discovered)
        if candidates:
            covered.update(node["topics"])
        incomplete = (
            incomplete
            or bool(observation.get("incomplete_results"))
            or bool(observation.get("error_code"))
        )
        trace.append(
            {
                "node_id": node["id"],
                "query_key": key,
                "status": "observed",
                "observation_id": entry["observation_id"],
                "observation_hash": entry["observation_hash"],
                "request_cost": cost,
                "repository_ids": sorted(discovered),
            }
        )
        if (
            candidates
            and not observation.get("error_code")
            and node["depth"] < max_depth
            and len(nodes) < max_nodes - 1
            and model_calls < model_cap
        ):
            reflection = await gateway.call(
                *run,
                prefix + "reflect:" + node["id"],
                "reflector",
                "基于实际检索观察寻找尚未回答的具体问题。只提出有新线索的检索，最多3个。"
                "不要重复原问题；无需扩展返回空queries及停止理由。不生成项目技术结论。",
                {
                    "root_question": case["question"],
                    "branch_question": node["question"],
                    "observations": observation,
                    "known_repository_count": len(repositories),
                    "topic_keys": case["topic_keys"],
                    "remaining_nodes": max_nodes - 1 - len(nodes),
                    "research_heuristic": policy,
                },
                QueryPlan,
                data_classes=["learning_state"],
            )
            model_calls += 1
            expand(QueryPlan.model_validate(reflection.structured), node["id"], node["depth"] + 1)
    return {
        "repository_ids": sorted(repositories),
        "tree": nodes,
        "trace": trace,
        "unobserved_queries": unobserved,
        "request_cost_unverified": cost_unverified,
        "requests_used": request_used,
        "request_cap": request_cap,
        "model_calls": model_calls,
        "model_cap": model_cap,
        "incomplete": incomplete,
        "scope": "actual_query_generation_on_frozen_observations_not_live_search",
        "verified_implementation": False,
        "new_external_requests": 0,
    }
