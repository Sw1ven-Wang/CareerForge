"""Versioned, lossless FactAtom/support selection over fresh reviewed packs.

This module does not approve facts or read raw source as knowledge. Callers must
first obtain each full publication through KnowledgeService.publication_pack.
Facts, conditions, support and counterevidence are indivisible. Publication
metadata is not evidence for an individual fact; facets travel only with all
their referenced claims. Full immutable publication records are never edited.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from ..errors import DomainError
from ..util import canonical, fingerprint
from .consumer_context import (
    CONSUMER_CONTEXT_CONTRACT,
    CONTEXT_EVIDENCE_PROJECTION_CONTRACT,
    pack_context_contract,
    require_standalone_facts,
    standalone_fact_ids,
    validate_context_contract,
)
from .dependencies import check_closure, require_object
from .question_coverage import QUESTION_COVERAGE_CONTRACT, validate_preferred_facts
from .retrieval_relevance import _terms

EVIDENCE_PROJECTION_CONTRACT = "complete-fact-support-1.6.6"
FACT_SELECTION_CONTRACT = "current-question-fact-idf-1.6.6"
METADATA_SCOPE = {
    "title": "publication_label",
    "topic_links": "publication_annotations_not_fact_support",
    "facets": "all_referenced_claims_selected",
    "limitations": "complete_publication_boundaries",
}


def byte_size(value: Any) -> int:
    return len(canonical(value).encode("utf-8"))


def fact_support(knowledge, db, actor: str, pack: dict) -> tuple[dict, dict]:
    """Resolve actual claim edges, never guess support by quote/text overlap."""
    bindings, citations = {}, {}
    for fact in pack["facts"]:
        claim = require_object(db, actor, fact["claim_id"], "claim")
        if (
            claim.payload["statement"] != fact["text"]
            or claim.payload["conditions"] != fact["conditions"]
        ):
            raise DomainError("FACT_HASH", "事实与已审核断言不一致。")
        binding = {
            "support_refs": list(dict.fromkeys(claim.payload["support_refs"])),
            "counterevidence_refs": list(
                dict.fromkeys(claim.payload.get("counterevidence_refs", []))
            ),
        }
        if not binding["support_refs"]:
            raise DomainError("EVIDENCE_PROJECTION", "事实缺少完整支持引用。")
        for identifier in [*binding["support_refs"], *binding["counterevidence_refs"]]:
            if identifier not in citations:
                citation = knowledge.evidence_dict(db, actor, identifier)
                if (
                    citation["source_id"] != pack["source_id"]
                    or citation["commit"] != pack["commit"]
                ):
                    raise DomainError("EVIDENCE_PROJECTION", "事实支持不属于固定来源版本。")
                citations[identifier] = citation
        bindings[fact["fact_atom_id"]] = binding
    return bindings, citations


def project_pack(
    pack: dict,
    selected_fact_ids: set[str],
    bindings: dict,
    citations: dict,
    selection_manifest_id: str,
    *,
    force_projection: bool = False,
    allowed_facet_ids: set[str] | None = None,
) -> dict:
    """Keep exact facts and complete canonical support, with no quote trimming."""
    context_contract = pack_context_contract(pack)
    if context_contract:
        require_standalone_facts(pack, selected_fact_ids)
    facts = [f for f in pack["facts"] if f["fact_atom_id"] in selected_fact_ids]
    if not facts or {f["fact_atom_id"] for f in facts} != selected_fact_ids:
        raise DomainError("EVIDENCE_PROJECTION", "投影事实不属于固定发布。")
    citation_ids = list(
        dict.fromkeys(
            identifier
            for f in facts
            for kind in ("support_refs", "counterevidence_refs")
            for identifier in bindings[f["fact_atom_id"]][kind]
        )
    )
    # An unmodified complete publication has its original wire shape. The
    # selection manifest still records the new contract and exact bindings.
    if (
        not force_projection
        and len(facts) == len(pack["facts"])
        and set(citation_ids) == {c["citation_id"] for c in pack["citations"]}
        and (allowed_facet_ids is None or {f["id"] for f in pack["facets"]} <= allowed_facet_ids)
    ):
        return pack
    claim_ids = {f["claim_id"] for f in facts}
    return {
        **pack,
        "facts": facts,
        "citations": [citations[identifier] for identifier in citation_ids],
        **(
            {
                "mechanism_edges": [
                    edge
                    for edge in pack["mechanism_edges"]
                    if edge["fact_atom_id"] in selected_fact_ids
                ]
            }
            if "mechanism_edges" in pack
            else {}
        ),
        "facets": [
            f
            for f in pack["facets"]
            if set(f["claim_ids"]) <= claim_ids
            and (allowed_facet_ids is None or f["id"] in allowed_facet_ids)
        ],
        "evidence_projection": {
            "contract": CONTEXT_EVIDENCE_PROJECTION_CONTRACT
            if context_contract
            else EVIDENCE_PROJECTION_CONTRACT,
            "selection_manifest_id": selection_manifest_id,
            "publication_hash": fingerprint(pack),
            "metadata_scope": dict(METADATA_SCOPE),
        },
    }


def rank_fact_candidates(
    packs: dict[str, dict], question: str, variants: list[str], *, coverage_contract=None
) -> list[dict]:
    """Current-question evidence, including its deterministic topic aliases.

    No titles, facets, raw quotes, cached ordering or model rewrite can outvote
    a current-question fact/condition match. Repeated terms cannot add score.
    Ties use fixed identities, not database iteration or cache insertion order.
    Scores are retrieval hints, never entailment or answer approval.
    """
    term_fn = _terms
    terms = {
        (pid, fact["fact_atom_id"]): term_fn(" ".join([fact["text"], *fact["conditions"]]))
        for pid, pack in packs.items()
        for fact in pack["facts"]
    }
    frequency = Counter(term for values in terms.values() for term in values)
    count = len(terms)
    current, aliases = term_fn(question), term_fn(" ".join(variants))

    def score(query: set[str], values: set[str]) -> float:
        return round(
            sum(
                math.log(1 + (count - frequency[t] + 0.5) / (frequency[t] + 0.5))
                for t in sorted(query & values)
            ),
            12,
        )

    rows = [
        {
            "publication_id": pid,
            "fact_atom_id": fid,
            "question_score": score(current, values),
            "alias_score": score(aliases, values),
        }
        for (pid, fid), values in terms.items()
    ]
    return sorted(
        rows,
        key=lambda r: (
            -r["alias_score"],
            -r["question_score"],
            packs[r["publication_id"]]["source_id"],
            r["publication_id"],
            r["fact_atom_id"],
        ),
    )


def _paired_facet_dependencies(
    packs: dict[str, dict],
    permitted: dict[str, set[str]],
    topic_keys: list[str],
    source_ids: list[str],
    max_project_samples: int,
) -> list[dict[str, tuple[set[str], set[str]]]]:
    dimensions: dict[tuple[str, str], dict[str, dict[str, tuple[set[str], set[str]]]]] = {}
    for pid, pack in sorted(packs.items()):
        source = pack["source_id"]
        if source_ids and source not in source_ids:
            continue
        claims = {
            fact["claim_id"]: fact["fact_atom_id"]
            for fact in pack["facts"]
            if pid not in permitted or fact["fact_atom_id"] in permitted[pid]
        }
        reviewed_topics = {
            link["topic_key"] for link in pack["topic_links"] if link["relation"] != "mentions"
        }
        for facet in pack["facets"]:
            if (
                facet["topic_key"] not in topic_keys
                or facet["topic_key"] not in reviewed_topics
                or facet["value_status"] != "known"
                or not facet["claim_ids"]
                or not set(facet["claim_ids"]) <= claims.keys()
            ):
                continue
            dimension = dimensions.setdefault((facet["topic_key"], facet["facet_key"]), {})
            facts, facets = dimension.setdefault(source, {}).setdefault(pid, (set(), set()))
            facts.update(claims[claim_id] for claim_id in facet["claim_ids"])
            facets.add(facet["id"])

    pairs = []
    for sides in (dimensions[key] for key in sorted(dimensions)):
        sources = source_ids or sorted(sides)[:max_project_samples]
        if len(sources) >= 2 and set(sources) <= sides.keys():
            pairs.append({pid: refs for source in sources for pid, refs in sides[source].items()})
    return pairs


def select_bounded_facts(
    packs: dict[str, dict],
    supports: dict[str, tuple[dict, dict]],
    *,
    question: str,
    variants: list[str],
    limit: int,
    per_project_limit: int,
    max_project_samples: int,
    selection_manifest_id: str,
    consumer_context_contract: str | None = None,
    paired_topic_keys: list[str] | None = None,
    paired_source_ids: list[str] | None = None,
    coverage_contract: str | None = None,
    preferred_fact_ids: list[str] | None = None,
) -> tuple[list[dict], dict]:
    validate_context_contract(consumer_context_contract)
    for pack in packs.values():
        if pack_context_contract(pack) != consumer_context_contract:
            raise DomainError("CONSUMER_CONTEXT_CONTRACT", "证据包与冻结的使用合同不一致。", 422)
    permitted = (
        {pid: standalone_fact_ids(p) for pid, p in packs.items()}
        if consumer_context_contract
        else {}
    )
    if limit < 0 or per_project_limit < 1 or max_project_samples < 1:
        raise DomainError("EVIDENCE_BUDGET", "证据预算或项目限制无效。", 422)
    if coverage_contract not in {None, QUESTION_COVERAGE_CONTRACT}:
        raise DomainError("QUESTION_COVERAGE_CONTRACT", "未知问题覆盖分配合同。", 422)
    ranked = rank_fact_candidates(packs, question, variants, coverage_contract=coverage_contract)
    if preferred_fact_ids and not coverage_contract:
        raise DomainError("QUESTION_COVERAGE_CONTRACT", "事实覆盖提案缺少显式版本。", 422)
    preference = {
        fid: index
        for index, fid in enumerate(
            validate_preferred_facts(preferred_fact_ids or [], {r["fact_atom_id"] for r in ranked})
        )
    }
    has_match = any(r["question_score"] or r["alias_score"] for r in ranked)
    selected: dict[str, set[str]] = {}
    units: dict[str, dict] = {}
    costs: dict[str, int] = {}
    groups: dict[str, set[str]] = {}
    total = 0
    omissions = []
    selected_facets: dict[str, set[str]] = {}

    # Reserve complete dimensions before lexical ranking. Keep all known values
    # for each selected side so comparison can still detect reviewed conflicts.
    if paired_topic_keys is not None:
        for pair in _paired_facet_dependencies(
            packs, permitted, paired_topic_keys, paired_source_ids or [], max_project_samples
        ):
            pair_groups = {source: set(pids) for source, pids in groups.items()}
            for pid in pair:
                pair_groups.setdefault(packs[pid]["source_id"], set()).add(pid)
            if len(pair_groups) > max_project_samples or any(
                len(pids) > per_project_limit for pids in pair_groups.values()
            ):
                continue
            proposals = {}
            proposed_total = total
            for pid, (fact_ids, facet_ids) in pair.items():
                facts = selected.get(pid, set()) | fact_ids
                facets = selected_facets.get(pid, set()) | facet_ids
                unit = project_pack(
                    packs[pid],
                    facts,
                    *supports[pid],
                    selection_manifest_id,
                    allowed_facet_ids=facets,
                )
                size = byte_size(unit)
                proposed_total += size - costs.get(pid, 0)
                proposals[pid] = facts, facets, unit, size
            if proposed_total <= limit:
                for pid, (facts, facets, unit, size) in proposals.items():
                    selected[pid], selected_facets[pid], units[pid], costs[pid] = (
                        facts,
                        facets,
                        unit,
                        size,
                    )
                total, groups = proposed_total, pair_groups

    def candidate_projection(pid, ids, available):
        candidate = project_pack(
            packs[pid],
            ids,
            *supports[pid],
            selection_manifest_id,
            allowed_facet_ids=selected_facets.get(pid, set())
            if paired_topic_keys is not None
            else None,
        )
        if paired_topic_keys is None and candidate["facets"] and byte_size(candidate) > available:
            # Facet summaries are optional metadata, not required support for
            # these exact facts. Omit them with an audit entry before declaring
            # that the complete fact/support itself exceeds the byte budget.
            lean = project_pack(
                packs[pid],
                ids,
                *supports[pid],
                selection_manifest_id,
                force_projection=True,
                allowed_facet_ids=set(),
            )
            if byte_size(lean) < byte_size(candidate):
                candidate = lean
        return candidate

    pending = []
    for row in ranked:
        if row["fact_atom_id"] in selected.get(row["publication_id"], set()):
            continue
        if (
            consumer_context_contract
            and row["fact_atom_id"] not in permitted[row["publication_id"]]
        ):
            omissions.append({**row, "reason": "standalone_review_required"})
        else:
            pending.append(row)
    while pending:
        if units:
            # After reserving the strongest match, account for shared support
            # and publication overhead. Another small complete boundary fact
            # should not lose to a slightly stronger but much larger marginal
            # addition. This is deterministic bounded allocation, not semantic
            # approval or an assertion of globally optimal set coverage.
            def allocation_key(row, remaining=limit - total):
                pid, fid = row["publication_id"], row["fact_atom_id"]
                candidate = candidate_projection(
                    pid, selected.get(pid, set()) | {fid}, remaining + costs.get(pid, 0)
                )
                marginal = max(1, byte_size(candidate) - costs.get(pid, 0))
                return (
                    -row["alias_score"] / marginal,
                    -row["alias_score"],
                    -row["question_score"],
                    packs[pid]["source_id"],
                    pid,
                    fid,
                )

            pending.sort(key=allocation_key)
        if preference:
            pending.sort(key=lambda row: preference.get(row["fact_atom_id"], len(preference)))
        row = pending.pop(0)
        pid, fid = row["publication_id"], row["fact_atom_id"]
        pack = packs[pid]
        group = pack["source_id"]
        reason = None
        if (
            has_match
            and fid not in preference
            and not (row["question_score"] or row["alias_score"])
        ):
            reason = "no_current_question_fact_match"
        elif group not in groups and len(groups) >= max_project_samples:
            reason = "project_sample_limit"
        elif pid not in selected and len(groups.get(group, set())) >= per_project_limit:
            reason = "per_project_publication_limit"
        if reason:
            omissions.append({**row, "reason": reason})
            continue
        proposed_ids = selected.get(pid, set()) | {fid}
        previous = costs.get(pid, 0)
        proposed = candidate_projection(pid, proposed_ids, limit - total + previous)
        size = byte_size(proposed)
        if total - previous + size > limit:
            standalone = byte_size(candidate_projection(pid, {fid}, limit))
            omissions.append(
                {
                    **row,
                    "reason": "single_fact_exceeds_budget"
                    if standalone > limit
                    else "remaining_budget",
                    "complete_fact_bytes": standalone,
                    "proposed_publication_bytes": size,
                    "remaining_bytes": limit - total,
                }
            )
            continue
        selected[pid], units[pid], costs[pid] = proposed_ids, proposed, size
        groups.setdefault(group, set()).add(pid)
        total += size - previous
    records = []
    for pid, unit in units.items():
        binding, _ = supports[pid]
        ids = [f["fact_atom_id"] for f in unit["facts"]]
        records.append(
            {
                "publication_id": pid,
                "unit_version_id": unit["unit_version_id"],
                "source_id": unit["source_id"],
                "publication_hash": fingerprint(packs[pid]),
                "selected_fact_ids": ids,
                "selected_citation_ids": [c["citation_id"] for c in unit["citations"]],
                "fact_support": {fid: binding[fid] for fid in ids},
                "selected_facet_ids": [f["id"] for f in unit["facets"]],
                "omitted_facet_ids": [
                    f["id"] for f in packs[pid]["facets"] if f not in unit["facets"]
                ],
                "omitted_facets": [
                    {
                        "facet_id": f["id"],
                        "reason": "paired_dimension_not_allocated"
                        if paired_topic_keys is not None
                        else "metadata_budget"
                        if set(f["claim_ids"]) <= {fact["claim_id"] for fact in unit["facts"]}
                        else "referenced_claim_omitted",
                    }
                    for f in packs[pid]["facets"]
                    if f not in unit["facets"]
                ],
                "projection_hash": fingerprint(unit),
                "selected_bytes": costs[pid],
                "mode": "fact_projection"
                if "evidence_projection" in unit
                else "complete_publication",
            }
        )
    return list(units.values()), {
        "contract": CONTEXT_EVIDENCE_PROJECTION_CONTRACT
        if consumer_context_contract
        else EVIDENCE_PROJECTION_CONTRACT,
        **(
            {"consumer_context_contract": consumer_context_contract}
            if consumer_context_contract
            else {}
        ),
        "ranking_contract": FACT_SELECTION_CONTRACT,
        **({"question_coverage_contract": coverage_contract} if coverage_contract else {}),
        "facts_approved": False,
        "budget_accounting": "sum_canonical_knowledge_units_utf8_bytes",
        "allocation": "paired_reviewed_facets_then_marginal_relevance"
        if paired_topic_keys is not None
        else "question_fact_proposal_then_marginal_relevance"
        if coverage_contract
        else "strongest_match_then_marginal_relevance_per_byte",
        "selected_bytes": total,
        "metadata_scope": dict(METADATA_SCOPE),
        "units": records,
        "fact_ranking": ranked,
        "omitted_facts": omissions,
    }


def refresh_selected_fact(
    knowledge, db, actor: str, pack: dict, fact_id: str, selection_id: str
) -> dict | None:
    """A hash-checked manifest limits identities; current publication is authority.

    Returns None for historical manifests so paid legacy requests stay identical.
    No cached projection body, claim text, citation text or hash is trusted.
    """
    check_closure(db, actor, [selection_id])
    manifest = require_object(db, actor, selection_id, "selection_manifest")
    projection = manifest.payload.get("evidence_projection")
    if not projection:
        return None
    marker = projection.get("contract")
    expected_context = (
        CONSUMER_CONTEXT_CONTRACT if marker == CONTEXT_EVIDENCE_PROJECTION_CONTRACT else None
    )
    if marker not in {EVIDENCE_PROJECTION_CONTRACT, CONTEXT_EVIDENCE_PROJECTION_CONTRACT}:
        raise DomainError("EVIDENCE_PROJECTION", "未知的证据投影合同。")
    if pack_context_contract(pack) != expected_context:
        raise DomainError("CONSUMER_CONTEXT_CONTRACT", "证据包与选择合同的独立使用版本不一致。")
    if expected_context:
        if projection.get("consumer_context_contract") != expected_context:
            raise DomainError("CONSUMER_CONTEXT_CONTRACT", "证据选择缺少独立使用合同。")
        require_standalone_facts(pack, {fact_id})
    record = next(
        (r for r in projection["units"] if r["publication_id"] == pack["publication_id"]), None
    )
    if record is None or fact_id not in record["selected_fact_ids"]:
        raise DomainError("ANSWER_SCOPE", "事实不属于该次有界证据选择。")
    if record["publication_hash"] != fingerprint(pack):
        raise DomainError("EVIDENCE_PROJECTION_CHANGED", "已审核资料发生变化，请重新检索。")
    binding, citations = fact_support(knowledge, db, actor, pack)
    if binding[fact_id] != record["fact_support"][fact_id]:
        raise DomainError("EVIDENCE_PROJECTION_CHANGED", "事实支持发生变化，请重新检索。")
    return project_pack(
        pack,
        {fact_id},
        binding,
        citations,
        selection_id,
        force_projection=True,
        allowed_facet_ids=set(record["selected_facet_ids"]),
    )


def selection_context_contract(db, actor: str, selection_id: str | None) -> str | None:
    """Resolve the frozen consumer version before rebuilding any provider input."""
    if selection_id is None:
        return None
    check_closure(db, actor, [selection_id])
    manifest = require_object(db, actor, selection_id, "selection_manifest")
    projection = manifest.payload.get("evidence_projection")
    if not projection:
        return None
    marker = projection.get("contract")
    if marker == EVIDENCE_PROJECTION_CONTRACT:
        if projection.get("consumer_context_contract") is not None:
            raise DomainError("CONSUMER_CONTEXT_CONTRACT", "旧选择不得隐式升级独立使用合同。")
        return None
    if (
        marker == CONTEXT_EVIDENCE_PROJECTION_CONTRACT
        and projection.get("consumer_context_contract") == CONSUMER_CONTEXT_CONTRACT
    ):
        return CONSUMER_CONTEXT_CONTRACT
    raise DomainError("EVIDENCE_PROJECTION", "未知或不完整的证据投影合同。")


def refresh_context_pack(knowledge, db, actor: str, publication_id: str, selection_id: str) -> dict:
    """Rebuild the selected new-contract unit without widening facts or facets."""
    marker = selection_context_contract(db, actor, selection_id)
    if marker != CONSUMER_CONTEXT_CONTRACT:
        raise DomainError("CONSUMER_CONTEXT_CONTRACT", "此消费者需要明确的独立事实合同。")
    pack = knowledge.publication_pack(db, actor, publication_id, consumer_context_contract=marker)
    manifest = require_object(db, actor, selection_id, "selection_manifest")
    records = manifest.payload["evidence_projection"]["units"]
    record = next((r for r in records if r["publication_id"] == publication_id), None)
    if record is None or record["publication_hash"] != fingerprint(pack):
        raise DomainError("EVIDENCE_PROJECTION_CHANGED", "已审核资料或选择范围发生变化。")
    bindings, citations = fact_support(knowledge, db, actor, pack)
    ids = set(record["selected_fact_ids"])
    require_standalone_facts(pack, ids)
    if {fid: bindings[fid] for fid in ids} != record["fact_support"]:
        raise DomainError("EVIDENCE_PROJECTION_CHANGED", "事实支持发生变化。")
    result = project_pack(
        pack,
        ids,
        bindings,
        citations,
        selection_id,
        force_projection=record["mode"] == "fact_projection",
        allowed_facet_ids=set(record["selected_facet_ids"]),
    )
    if fingerprint(result) != record["projection_hash"]:
        raise DomainError("EVIDENCE_PROJECTION_CHANGED", "事实投影已变化。")
    return result
