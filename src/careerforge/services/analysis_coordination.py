"""Coordinator -> Analysis goal-coverage contract, without another agent.

A *goal* states what should be investigated.  It never asserts that a source
implements a technology.  Only independently reviewed, published claims may
satisfy final coverage.  The coordinator may schedule at most the one retry
already funded by ``same-snapshot-evidence-retry-v1``.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Final, Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from ..contracts import Contract, MechanismReadTarget
from ..contracts_research_v163 import ResearchTaskContract
from ..errors import DomainError
from ..models import AgentRun, StageArtifact
from ..util import fingerprint
from .mechanism_discovery import STAGED_MECHANISM_PHASES, candidate_catalog
from .retrieval_relevance import _terms

COORDINATOR_ANALYSIS_CONTRACT: Final = "coordinator-analysis-coverage-v1"
BRIEF_STAGE = "coordinator.analysis_brief."
NAVIGATION_CONTRACT: Final = "coordinator-evidence-navigation-v2"
NAVIGATION_FIELD: Final = "coordinator_navigation_contract"
PHASE_FACETS = dict(
    zip(
        STAGED_MECHANISM_PHASES,
        ("registration", "argument_validation", "dispatch", "result_feedback", "failure_boundary"),
        strict=True,
    )
)


def requests_cross_file_flow(question: str) -> bool:
    """Recognize explicit scope; an ordinary request about a function stays ordinary."""
    return bool(
        (
            re.search(r"跨(?:文件|模块)|cross[- ](?:file|module)", question, re.I)
            and re.search(r"调用|链路|机制|派发|流程|call|flow|chain|dispatch", question, re.I)
        )
        or re.search(
            r"(?:完整|端到端).{0,12}(?:工具调用|调用链)|(?:complete|end.to.end).{0,18}tool.call",
            question,
            re.I,
        )
    )


class CoverageObjective(Contract):
    key: str = Field(min_length=1, max_length=160)
    kind: Literal["question", "dimension", "requirement"]
    priority: Literal["required", "alternative", "optional"] = "required"
    dimension: str | None = None
    requirement_id: str | None = None
    # Preserve JD modality and logical groups; an OR branch is not an AND obligation.
    modality: str | None = None
    group_operator: str | None = None
    group_id: str | None = None
    group_path: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    quote: str | None = None
    target_depth: str | None = None
    depth_basis: str | None = None
    required_dimensions: list[str] = Field(default_factory=list, max_length=7)


class CoordinatorAnalysisBrief(Contract):
    version: Literal["coordinator-analysis-coverage-v1"] = COORDINATOR_ANALYSIS_CONTRACT
    source_id: str = Field(min_length=1)
    source_scope_hash: str = Field(min_length=1)
    source_commit: str | None = None
    task_contract_hash: str = Field(min_length=1)
    question: str = Field(min_length=1)
    topic_hints: list[str] = Field(default_factory=list, max_length=75)
    objectives: list[CoverageObjective] = Field(min_length=1, max_length=40)
    open_objective_keys: list[str] = Field(min_length=1, max_length=40)
    round_index: Literal[0, 1] = 0
    prior_review_ids: list[str] = Field(default_factory=list, max_length=5)
    read_targets: list[MechanismReadTarget] = Field(default_factory=list, max_length=4)
    max_rounds: Literal[2] = 2
    completion_rule: Literal["independently_reviewed_published_evidence_only"] = (
        "independently_reviewed_published_evidence_only"
    )

    @model_validator(mode="after")
    def validate_scope(self):
        keys = [row.key for row in self.objectives]
        if len(keys) != len(set(keys)) or len(self.open_objective_keys) != len(
            set(self.open_objective_keys)
        ):
            raise ValueError("Duplicate or ambiguous coverage objectives")
        if not set(self.open_objective_keys) <= set(keys):
            raise ValueError("Open objectives must belong to the frozen goal")
        if self.round_index == 0 and (self.prior_review_ids or self.read_targets):
            raise ValueError("Initial task cannot borrow reviewer or follow-up data")
        if self.round_index == 1 and (not self.prior_review_ids or not self.read_targets):
            raise ValueError("Follow-up requires an independent review lineage")
        return self


def goal_objectives(task: ResearchTaskContract) -> list[CoverageObjective]:
    values = [CoverageObjective(key="question", kind="question")]
    values.extend(
        CoverageObjective(key="dimension:" + dimension, kind="dimension", dimension=dimension)
        for dimension in task.required_dimensions
    )
    for req in task.requirements:
        # Negative JD requirements are constraints, never learning coverage targets.
        if getattr(req, "modality", None) == "not_required":
            continue
        group_path = [group.model_dump(mode="json") for group in getattr(req, "group_path", [])]
        is_alternative = getattr(req, "group_operator", None) == "OR" or any(
            group.get("operator") == "OR" for group in group_path
        )
        values.append(
            CoverageObjective(
                key="requirement:" + req.requirement_id,
                kind="requirement",
                priority=(
                    "optional"
                    if getattr(req, "modality", None) in {"preferred", "bonus"}
                    else "alternative"
                    if is_alternative
                    else "required"
                ),
                requirement_id=req.requirement_id,
                modality=getattr(req, "modality", None),
                group_operator=getattr(req, "group_operator", None),
                group_id=getattr(req, "group_id", None),
                group_path=group_path,
                quote=req.quote,
                target_depth=req.depth,
                depth_basis=req.depth_basis,
                required_dimensions=list(req.required_dimensions),
            )
        )
    return values


def freeze_brief(
    task_data: dict[str, Any],
    *,
    source: Any,
    question: str,
    topic_hints: list[str],
    round_index: Literal[0, 1] = 0,
    open_keys: list[str] | None = None,
    prior_review_ids: list[str] | None = None,
    read_targets: list[dict[str, Any]] | None = None,
    navigation_contract: str | None = None,
    navigation_needs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Only the root coordinator constructs this; never use model-proposed scope."""
    task = ResearchTaskContract.model_validate(task_data)
    if task.original_question != question:
        raise DomainError("ANALYSIS_BRIEF_SCOPE", "分析目标与冻结问题不一致。", 409)
    objectives = goal_objectives(task)
    keys = [row.key for row in objectives]
    if navigation_contract is not None and navigation_contract != NAVIGATION_CONTRACT:
        raise DomainError("ANALYSIS_BRIEF_SCOPE", "未知证据导航合同。", 409)
    groups = objective_groups(objectives) if navigation_contract else None
    if groups is not None:
        keys = groups["active_required"]
        if open_keys is not None and not set(open_keys) <= set(keys):
            raise DomainError("ANALYSIS_BRIEF_SCOPE", "补查不能扩大到可选目标或未指定深度。", 409)
    brief = CoordinatorAnalysisBrief(
        source_id=source.id,
        source_scope_hash=source.scope_hash,
        source_commit=source.commit_sha,
        task_contract_hash=fingerprint(task.model_dump(mode="json")),
        question=question,
        topic_hints=list(dict.fromkeys(topic_hints)),
        objectives=objectives,
        open_objective_keys=keys if open_keys is None else open_keys,
        round_index=round_index,
        prior_review_ids=prior_review_ids or [],
        read_targets=[MechanismReadTarget.model_validate(row) for row in read_targets or []],
    )
    data = brief.model_dump(mode="json")
    if groups is not None:
        data.update(
            navigation_contract=navigation_contract,
            goal_requirements=[req.model_dump(mode="json") for req in task.requirements],
            **groups,
            navigation_needs=navigation_needs or [],
        )
    return data


def objective_groups(objectives):
    groups: dict[str, list[str]] = {
        "active_required": [],
        "optional_visible": [],
        "unresolved_scope": [],
    }
    for row in objectives:
        if row.priority == "optional":
            key = "optional_visible"
        elif row.kind == "requirement" and row.target_depth is None:
            key = "unresolved_scope"
        else:
            key = "active_required"
        groups[key].append(row.key)
    return groups


def open_objectives(task_data, coverage, *, investigable=False):
    """Select blockers from the published evaluation's real Boolean obligations."""
    task = ResearchTaskContract.model_validate(task_data)
    complete_groups = {
        group["group_id"]
        for group in coverage.get("requirement_groups", [])
        if group["operator"] == "OR" and group["complete"]
    }
    requirements = {item["requirement_id"]: item for item in coverage["requirements"]}
    dimensions = {item["dimension"]: item for item in coverage["dimensions"]}
    result = []
    for objective in goal_objectives(task):
        if objective.kind == "requirement":
            item = requirements[objective.requirement_id]
            if (
                objective.priority == "optional"
                or objective.group_id in complete_groups
                or any(group["group_id"] in complete_groups for group in objective.group_path)
                or (investigable and objective.target_depth is None)
            ):
                continue
        elif objective.kind == "dimension":
            item = dimensions[objective.dimension]
        else:
            item = coverage["question"]
        if not item["complete"]:
            result.append(objective.key)
    return result


def fence_call_flow(coverage, targets, published_edges, *, autonomous, path_evidence=None):
    """A scoped claim cannot substitute for a requested, independently reviewed call path."""
    if not autonomous and not targets:
        return coverage
    by_key = {edge["edge_key"]: edge for edge in published_edges}
    selected = [
        by_key[row["key"]]
        for row in targets
        if row["key"] in by_key and by_key[row["key"]]["target"] == row
    ]
    pairs = [
        tuple(
            (edge["target"][side]["path"], edge["target"][side]["symbol"])
            for side in ("caller", "callee")
        )
        for edge in selected
    ]

    def connects(last, remaining):
        return not remaining or any(
            first == last and connects(second, remaining[:i] + remaining[i + 1 :])
            for i, (first, second) in enumerate(remaining)
        )

    established = bool(pairs) and len(pairs) == len(targets)
    if autonomous:
        established = (
            established
            and len(pairs) >= 2
            and any(connects(last, pairs[:i] + pairs[i + 1 :]) for i, (_, last) in enumerate(pairs))
        )
    if autonomous and path_evidence is not None:
        established = path_evidence["complete"]
        coverage = {**coverage, "mechanism_path": path_evidence}
    if established:
        return coverage
    dimensions = []
    for row in coverage["dimensions"]:
        if row["dimension"] == "call_flow":
            row = {
                **row,
                "complete": False,
                "status": "partial" if row["evidence"] else "unknown",
                "reason_codes": [*row["reason_codes"], "missing_reviewed_mechanism_connection"],
            }
        dimensions.append(row)
    if dimensions == coverage["dimensions"] and path_evidence is None:
        return coverage
    return {
        **coverage,
        "dimensions": dimensions,
        "complete": False,
        "status": "partial" if coverage["publication_ids"] else "unknown",
    }


def published_phase_witnesses(pack, symbols, claim_support):
    """Bind an approved facet to a concrete symbol through its exact claim citations."""
    identities = Counter((symbol.path, symbol.name) for symbol in symbols)
    output = []
    for facet in pack["facets"]:
        if facet["topic_key"] != "agent.tool_calling" or facet["value_status"] != "known":
            continue
        phase = next((p for p, key in PHASE_FACETS.items() if key == facet["facet_key"]), None)
        if phase is None:
            continue
        for claim_id in facet["claim_ids"]:
            for cite in claim_support.get(claim_id, []):
                candidates = [
                    symbol
                    for symbol in symbols
                    if symbol.path == cite["path"]
                    and symbol.start_line
                    <= cite["start_line"]
                    <= cite["end_line"]
                    <= symbol.end_line
                    and identities[(symbol.path, symbol.name)] == 1
                ]
                if not candidates:
                    continue
                # A nested function belongs to its own body, not every enclosing symbol.
                width = min(row.end_line - row.start_line for row in candidates)
                narrowest = [row for row in candidates if row.end_line - row.start_line == width]
                if len(narrowest) != 1:
                    continue
                symbol = narrowest[0]
                output.append(
                    {
                        "phase": phase,
                        "path": symbol.path,
                        "symbol": symbol.name,
                        "source_id": pack["source_id"],
                        "commit": pack["commit"],
                        "publication_id": pack["publication_id"],
                        "facet_id": facet["id"],
                        "claim_id": claim_id,
                        "citation_id": cite["citation_id"],
                    }
                )
    return output


def reviewed_mechanism_path(edges, witnesses):
    """Walk only published calls within one source and match independently reviewed phases."""
    phases = STAGED_MECHANISM_PHASES
    graph = defaultdict(list)
    at = defaultdict(lambda: defaultdict(list))
    endpoints = {}

    def node(row, endpoint):
        return (row["source_id"], row["commit"], endpoint["path"], endpoint["symbol"])

    for edge in edges:
        start, end = (node(edge, edge["target"][side]) for side in ("caller", "callee"))
        graph[start].append((end, edge["edge_id"]))
        endpoints[edge["edge_id"]] = (start, end)
    for proof in witnesses:
        at[node(proof, proof)][proof["phase"]].append(proof)

    established_transitions = set()
    best_nodes, best_edges, best_witnesses = (), (), ()

    def visit(current, route, edge_ids, proofs):
        nonlocal best_nodes, best_edges, best_witnesses
        while len(proofs) < len(phases) and at[current].get(phases[len(proofs)]):
            proofs = (*proofs, at[current][phases[len(proofs)]][0])
        for first, second in zip(proofs, proofs[1:], strict=False):
            established_transitions.add((first["phase"], second["phase"]))
        if (len(proofs), len(edge_ids)) > (len(best_witnesses), len(best_edges)):
            best_nodes, best_edges, best_witnesses = route, edge_ids, proofs
        for end, identifier in graph[current]:
            if end not in route:
                visit(end, (*route, end), (*edge_ids, identifier), proofs)

    # A source-cited entry must precede the other phases. Without it a local
    # call fragment cannot be presented as the complete cross-file route.
    for start in sorted(at):
        if at[start].get("entry"):
            visit(start, (start,), (), ())
    seen_phases = {row["phase"] for row in witnesses}
    missing_phases = [phase for phase in phases if phase not in seen_phases]
    missing_transitions = [
        [before, after]
        for before, after in zip(phases, phases[1:], strict=False)
        if (before, after) not in established_transitions
    ]
    complete = len(best_witnesses) == len(phases) and len(best_edges) >= 2
    return {
        "complete": complete,
        "scope": "published_static_calls",
        "published_edge_ids": sorted(endpoints),
        "path_edge_ids": list(best_edges),
        "path_symbols": [{"path": n[2], "symbol": n[3]} for n in best_nodes],
        "phase_witnesses": witnesses,
        "path_witnesses": list(best_witnesses),
        "missing_phases": missing_phases,
        "missing_transitions": missing_transitions,
        "reason_codes": [
            *(["missing_published_phase"] if missing_phases else []),
            *(["missing_published_transition"] if missing_transitions else []),
            *(["no_single_published_cross_file_path"] if not complete else []),
        ],
    }


# This is a source-navigation hint only, never evidence that a code path exists.
_PHASE_PRIORITY = {
    "dimension:purpose": ("registration", "dispatch"),
    "dimension:project_structure": ("registration", "dispatch"),
    "dimension:module_responsibilities": ("registration", "dispatch"),
    "dimension:call_flow": ("dispatch", "result"),
    "dimension:implementation": ("dispatch", "registration", "result"),
    "dimension:boundaries": ("boundary", "registration"),
    "dimension:tradeoffs": ("boundary", "dispatch"),
    "question": ("result", "dispatch", "boundary"),
}


def coverage_read_candidates(
    source,
    symbols,
    question: str,
    open_keys: list[str],
    existing_reads: list[dict[str, Any]],
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Select new frozen windows; no speculative edges or unbounded repo scan."""
    if not 0 <= limit <= 4:
        raise ValueError("Follow-up window budget exceeded")
    if limit == 0:
        return []
    edges, windows = candidate_catalog(source, symbols, question, staged=True, connected=True)
    ranked = []
    phases: list[str] = []
    for objective in open_keys:
        phases.extend(_PHASE_PRIORITY.get(objective, ("dispatch", "result", "boundary")))
    phases = list(dict.fromkeys(phases)) or ["dispatch", "result", "boundary"]
    for window in windows.values():
        purpose = window["purpose"]
        phase = window.get("phase_hint", "validation" if purpose == "registration" else purpose)
        priority = (
            phases.index(purpose)
            if purpose in phases
            else (phases.index(phase) if phase in phases else len(phases))
        )
        ranked.append((priority, window))
    # Cross-file call-site candidates are also only navigational hints.
    for row in edges.values():
        for side in ("caller", "callee"):
            window = row[side]
            priority = phases.index("dispatch") if "dispatch" in phases else len(phases)
            ranked.append((priority + (side == "callee"), window))
    paths = source.files
    output, chosen = [], set()
    for _, window in sorted(
        ranked, key=lambda item: (item[0], item[1]["path"], item[1]["start_line"])
    ):
        clean = {
            key: window[key] for key in ("path", "symbol", "start_line", "end_line", "purpose")
        }
        try:
            target = MechanismReadTarget.model_validate(clean)
        except ValueError:
            continue
        if target.path not in paths or target.end_line > len(paths[target.path].splitlines()):
            continue
        key = (target.path, target.start_line, target.end_line)
        if key in chosen or any(
            r.get("path") == target.path
            and r.get("first_line", 10**10) <= target.start_line
            and r.get("last_line", -1) >= target.end_line
            for r in existing_reads
        ):
            continue
        chosen.add(key)
        output.append(target.model_dump(mode="json"))
        if len(output) == limit:
            break
    return output


def navigation_windows(
    source, symbols, question, coverage, open_keys, previous_reads, targets, findings, initial_edges
):
    """Allocate the remaining new reads to reviewer gaps, then reachable missing phases."""
    mechanism = coverage.get("mechanism_path", {})
    missing = mechanism.get("missing_phases", [])
    transitions = mechanism.get("missing_transitions", [])
    phase_order = list(
        dict.fromkeys(
            [phase for transition in transitions for phase in transition]
            + missing
            + [phase for key in open_keys for phase in _PHASE_PRIORITY.get(key, ())]
        )
    )
    if not phase_order:
        phase_order = ["dispatch", "result", "boundary"]
    phase_order = ["entry" if phase == "registration" else phase for phase in phase_order]
    premise = " ".join(row["missing_premise"] for row in findings)
    query_terms = _terms(question + " " + premise)
    established = {(row["path"], row["symbol"]) for row in mechanism.get("path_symbols", [])}
    leads, alternative = [], []
    candidates = list(targets)
    edges, windows = candidate_catalog(
        source, symbols, question + " " + premise[:800], staged=True, connected=True, directed=True
    )

    def covers(row, window):
        return (
            row["path"] == window["path"]
            and row["start_line"] <= window["start_line"]
            and row["end_line"] >= window["end_line"]
        )

    def clean(window):
        return {k: window[k] for k in ("path", "symbol", "start_line", "end_line", "purpose")}

    def already(window):
        return any(
            covers(
                {
                    "path": row["path"],
                    "start_line": row["first_line"],
                    "end_line": row["last_line"],
                },
                window,
            )
            for row in previous_reads
        )

    def add(window, phase, origin, needed):
        nearby = [
            proof
            for proof in mechanism.get("phase_witnesses", [])
            if proof["phase"] == phase
            or (proof["path"], proof["symbol"]) == (window["path"], window["symbol"])
        ]
        leads.append(
            {
                "phase": phase,
                "needed_premise": needed,
                "target_symbols": [{"path": window["path"], "symbol": window["symbol"]}],
                "candidate_sources": [origin],
                "verified_evidence": nearby,
                "window": window,
                "window_budget": 1,
                "status": "candidate",
            }
        )

    def phase_for(window):
        return window.get("phase_hint") or (
            "validation" if window["purpose"] == "registration" else window["purpose"]
        )

    def need(phase):
        link = next((row for row in transitions if row[1] == phase), None)
        return (
            "需要已审核的具体关系连接 " + " -> ".join(link)
            if link
            else "需要当前固定源码证实阶段 " + phase + " 的适用条件与边界"
        )

    for window in targets:
        add(window, phase_for(window), "independent_review_gap", premise or "独立审核要求补充前提")

    # Reserve both endpoint windows together. A proposed replacement edge is a
    # read lead until Analysis and Independent Review each verify it.
    if mechanism and not mechanism["complete"] and len(candidates) <= 2:
        original = {
            tuple((edge[side]["path"], edge[side]["symbol"]) for side in ("caller", "callee"))
            for edge in initial_edges
        }

        def edge_rank(edge):
            ends = {(edge[side]["path"], edge[side]["symbol"]) for side in ("caller", "callee")}
            labels = " ".join(path + " " + symbol for path, symbol in sorted(ends))
            return (
                -len(ends & established),
                -len(query_terms & _terms(labels)),
                edge["candidate_id"],
            )

        for edge in sorted(edges.values(), key=edge_rank):
            ends = tuple(
                (edge[side]["path"], edge[side]["symbol"]) for side in ("caller", "callee")
            )
            if ends in original:
                continue
            new = [
                clean(edge[side])
                for side in ("caller", "callee")
                if not any(covers(row, edge[side]) for row in candidates)
            ]
            if not new or len(candidates) + len(new) > 4 or any(already(row) for row in new):
                continue
            for window in new:
                candidates.append(window)
                add(window, "dispatch", "static_cross_file_callsite", need("dispatch"))
            alternative = [
                {
                    "key": "mechanism." + edge["candidate_id"],
                    "caller": clean(edge["caller"]),
                    "callee": clean(edge["callee"]),
                }
            ]
            break

    for window in sorted(
        windows.values(),
        key=lambda row: (
            phase_order.index(phase_for(row))
            if phase_for(row) in phase_order
            else len(phase_order),
            (row["path"], row["symbol"]) not in established,
            -len(query_terms & _terms(row["path"] + " " + row["symbol"])),
            row["path"],
            row["start_line"],
        ),
    ):
        if len(candidates) == 4:
            break
        if already(window) or any(covers(row, window) for row in candidates):
            continue
        target = clean(window)
        candidates.append(target)
        phase = phase_for(window)
        add(target, phase, "static_phase_window", need(phase))
    return candidates, leads, alternative


def require_bound_brief(db, child: AgentRun, source):
    """Reconstruct authority from the frozen root stages, not from child input."""
    if not child.parent_id and "coordinator_analysis_contract" not in child.input_manifest:
        return None
    parent = db.get(AgentRun, child.parent_id) if child.parent_id else None
    manifest = child.input_manifest
    marker = manifest.get("coordinator_analysis_contract")
    navigation = manifest.get(NAVIGATION_FIELD)
    parent_marker = parent.input_manifest.get("coordinator_analysis_contract") if parent else None
    if marker is None and parent_marker is None:
        return None

    def reject():
        return DomainError("ANALYSIS_BRIEF_SCOPE", "分析目标、授权、快照或审核谱系不一致。", 409)

    if (
        parent is None
        or parent.parent_id
        or parent.workflow != "knowledge_build"
        or marker != COORDINATOR_ANALYSIS_CONTRACT
        or marker != parent_marker
        or navigation != parent.input_manifest.get(NAVIGATION_FIELD)
        or parent.input_manifest.get("source_id") != source.id
        or parent.principal_id != child.principal_id
        or parent.namespace_id != child.namespace_id
        or parent.funding_id != child.funding_id
        or child.workflow != "analysis"
        or fingerprint(parent.input_manifest) != parent.input_hash
        or fingerprint(manifest) != child.input_hash
        or manifest.get("source_ids") != [source.id]
    ):
        raise reject()

    def stage(key):
        row = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == parent.id, StageArtifact.stage_key == key
            )
        )
        if row is None or row.input_hash != parent.input_hash:
            raise reject()
        return row.payload

    round_index: Literal[0, 1] = 1 if "retry_lineage" in manifest else 0
    stage_key = BRIEF_STAGE + str(round_index)
    if manifest.get("analysis_brief_stage") != stage_key:
        raise reject()
    saved = stage(stage_key)
    if manifest.get("analysis_brief_hash") != fingerprint(saved):
        raise reject()
    task = stage("research_context").get("research_task")
    prepared = stage("sources")
    if (
        task != manifest.get("research_task")
        or prepared.get("source_ids") != [source.id]
        or manifest.get("question") != parent.input_manifest.get("question")
        or prepared.get("topic_keys") != manifest.get("topic_keys")
    ):
        raise reject()
    options = {}
    if round_index:
        lineage = stage("evidence_retry_plan")
        first_review = stage("review_child")
        reviewer = db.get(AgentRun, first_review.get("run_id"))
        result = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == first_review.get("run_id"),
                StageArtifact.stage_key == "result",
            )
        )
        if (
            lineage != manifest.get("retry_lineage")
            or lineage.get("prior_review_run_id") != first_review.get("run_id")
            or reviewer is None
            or reviewer.workflow != "review"
            or reviewer.parent_id != parent.id
            or reviewer.status not in {"succeeded", "partially_succeeded"}
            or fingerprint(reviewer.input_manifest) != reviewer.input_hash
            or result is None
            or result.input_hash != reviewer.input_hash
            or result.payload.get("review_ids") != lineage.get("prior_review_ids")
            or lineage.get("source_id") != source.id
            or lineage.get("source_commit") != source.commit_sha
            or lineage.get("source_scope_hash") != source.scope_hash
            or lineage.get("read_targets") != manifest.get("mechanism_read_targets")
        ):
            raise reject()
        options = {
            "open_keys": lineage.get("coverage_open_objectives") or ["question"],
            "prior_review_ids": lineage["prior_review_ids"],
            "read_targets": lineage["read_targets"],
            "navigation_needs": lineage.get("navigation_needs"),
        }
    try:
        expected = freeze_brief(
            task,
            source=source,
            question=parent.input_manifest["question"],
            topic_hints=prepared["topic_keys"],
            round_index=round_index,
            navigation_contract=navigation,
            **options,
        )
        for target in expected["read_targets"]:
            if target["path"] not in source.files or target["end_line"] > len(
                source.files[target["path"]].splitlines()
            ):
                raise ValueError("Target is outside source")
    except (KeyError, TypeError, ValueError) as exc:
        raise reject() from exc
    if saved != expected:
        raise reject()
    return saved
