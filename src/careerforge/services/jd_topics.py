"""Source-anchored JD topic links and pending proposals (no taxonomy writes)."""

import re

from ..util import fingerprint
from .taxonomy import term_matches

JD_TOPIC_MAPPING = "jd-topic-links-v2"
TOOL_CALLING = "agent.tool_calling"
_TOOL_FACETS = {
    "argument_validation": ("参数校验", "参数验证", "argument validation", "parameter validation"),
    "result_feedback": ("工具结果回填", "结果回填", "tool result feedback"),
    "failure_boundary": ("错误处理", "异常处理", "tool error handling"),
    "registration": ("工具注册", "tool registration"),
    "dispatch": ("工具分发", "执行分发", "tool dispatch"),
}
_LEXICAL_EQUIVALENTS = {
    "rag.hybrid": ("排名融合", "排序融合", "倒数排名融合", "rank fusion", "reciprocal rank fusion"),
    "runtime.async": ("异步 I/O", "async I/O"),
    "code.testing": ("自动化测试", "automated testing"),
}
_TOOL_CONTEXT = ("工具调用", "tool calling", "function calling")


_RANK_FUSION = re.compile(
    r"排名融合|排序融合|倒数排名|倒数秩|rank[_ -]?fusion|\brrf\b|reciprocal[_ -]?rank", re.I
)


def requires_rank_fusion(goal):
    return any(
        row.get("modality") != "not_required"
        and _RANK_FUSION.search(row.get("technology", ""))
        and _RANK_FUSION.search(row.get("quote", ""))
        for row in goal.get("requirements", [])
    )


def explains_rank_fusion(pack, approved):
    return any(
        fact.get("fact_atom_id") in approved and _RANK_FUSION.search(fact["text"])
        for fact in pack["facts"]
    )


def _anchored(term, technology, quote):
    return term_matches(technology, term) and term_matches(quote, term)


def _specific_candidates(technology, quote, versions, aliases):
    hits = []
    for key, terms in aliases.items():
        if key not in versions:
            continue
        for term in [*terms, *_LEXICAL_EQUIVALENTS.get(key, ())]:
            term = term.strip()
            if len(term) < 3 or not _anchored(term, technology, quote):
                continue
            left = r"(?<![A-Za-z0-9_])" if term[0].isascii() and term[0].isalnum() else ""
            right = r"(?![A-Za-z0-9_])" if term[-1].isascii() and term[-1].isalnum() else ""
            hits.extend(
                (key, match.start(), match.end())
                for match in re.finditer(left + re.escape(term) + right, technology, re.I)
            )
    # Generic words nested in a protocol/product name do not create extra
    # topics. Separate occurrences remain ambiguous and need confirmation.
    return {
        key
        for key, start, end in hits
        if not any(
            other_key != key and lo <= start and end <= hi and hi - lo > end - start
            for other_key, lo, hi in hits
        )
    }


def topic_links(requirements, versions, aliases, facets):
    result = []
    for row in requirements:
        technology = row["technology"].strip()
        quote = row["quote"]
        exact = {
            key
            for key, terms in aliases.items()
            if key in versions
            and any(
                technology.casefold() == term.strip().casefold() and term_matches(quote, term)
                for term in [*terms, *_LEXICAL_EQUIVALENTS.get(key, ())]
            )
        }
        # Descriptive model labels may add qualifiers to a named technology.
        # A short term must occur in both the label and its literal JD quote.
        candidates = exact or _specific_candidates(technology, quote, versions, aliases)
        tool_context = any(term_matches(quote, term) for term in _TOOL_CONTEXT)
        phase_keys = [
            facet
            for facet, terms in _TOOL_FACETS.items()
            if facet in facets.get(TOOL_CALLING, ())
            and any(_anchored(term, technology, quote) for term in terms)
            and (facet != "failure_boundary" or tool_context)
        ]
        if phase_keys and TOOL_CALLING in versions:
            candidates = {TOOL_CALLING}
        framework = "framework.langgraph"
        routing = "agent.conditional_routing"
        secondary = []
        if (
            routing in candidates
            and framework in versions
            and term_matches(quote, "LangGraph")
            and (row["group_operator"] == "SINGLE" or framework in candidates)
        ):
            secondary = [framework]
            candidates.discard(framework)
        primary = next(iter(candidates)) if len(candidates) == 1 else None
        links = [
            {"topic_key": key, "version": versions[key], "role": role}
            for key, role in [(primary, "primary"), *((key, "framework") for key in secondary)]
            if key and primary
        ]
        excluded = row["modality"] == "not_required"
        proposal = None
        if not primary and not excluded:
            proposal = {
                "proposal_id": "jd-topic-" + fingerprint(row["requirement_id"])[:24],
                "state": "pending_confirmation",
                "label": technology,
                "reason": "ambiguous_authorized_alias"
                if candidates
                else "no_anchored_authorized_alias",
                "candidate_topics": [
                    {"topic_key": key, "version": versions[key]} for key in sorted(candidates)
                ],
                "paragraph_id": row["paragraph_id"],
                "span_start": row["span_start"],
                "span_end": row["span_end"],
            }
        result.append(
            {
                "requirement_id": row["requirement_id"],
                "status": "excluded"
                if excluded
                else "mapped"
                if primary
                else "pending_confirmation",
                "topics": links,
                "facet_keys": sorted(phase_keys) if primary == TOOL_CALLING else [],
                "proposal": proposal,
            }
        )
    return result


def mapped_learning_topics(payload):
    """Current mapped positive topics. Pending and NOT carry no learning scope."""
    if payload.get("topic_link_contract") == JD_TOPIC_MAPPING:
        return list(
            dict.fromkeys(
                link["topic_key"]
                for row in payload["requirement_topic_links"]
                if row["status"] == "mapped"
                for link in row["topics"]
            )
        )
    return list(
        dict.fromkeys(
            row["topic_key"]
            for row in payload["requirements"]
            if row.get("topic_key") and row["modality"] != "not_required"
        )
    )


def plan_topic_coverage(frozen, modules):
    goal = frozen.get("goal") or {}
    payload = goal.get("requirements")
    if not isinstance(payload, dict) or payload.get("topic_link_contract") != JD_TOPIC_MAPPING:
        return {}
    packs = {pack["publication_id"]: pack for pack in frozen["units"]}
    rows = []
    for mapping in payload["requirement_topic_links"]:
        anchors = []
        if mapping["status"] == "mapped":
            for module in modules:
                pack = packs.get(module.publication_id)
                if not pack:
                    continue
                focus = set(module.focus_fact_ids)
                for target in mapping["topics"]:
                    if target["role"] != "primary":
                        continue
                    key, version = target["topic_key"], target["version"]
                    if not any(
                        link["topic_key"] == key
                        and link["topic_version"] == version
                        and link["relation"] != "mentions"
                        for link in pack["topic_links"]
                    ):
                        continue
                    if mapping["facet_keys"]:
                        claims = {
                            claim_id
                            for facet in pack["facets"]
                            if facet["topic_key"] == key
                            and facet["schema_version"] == version
                            and facet["value_status"] == "known"
                            and facet["facet_key"] in mapping["facet_keys"]
                            for claim_id in facet["claim_ids"]
                        }
                        focus = {
                            fact["fact_atom_id"]
                            for fact in pack["facts"]
                            if fact["fact_atom_id"] in focus and fact["claim_id"] in claims
                        }
                    if focus:
                        anchors.append(
                            {
                                "module_key": module.key,
                                "publication_id": module.publication_id,
                                "fact_atom_ids": sorted(focus),
                            }
                        )
        rows.append(
            {
                **mapping,
                "plan_status": (
                    "reviewed_study_material" if anchors else "no_selected_reviewed_material"
                )
                if mapping["status"] == "mapped"
                else mapping["status"],
                "anchors": anchors,
            }
        )
    return {
        "jd_topic_coverage": {
            "contract": JD_TOPIC_MAPPING,
            "goal_version_id": goal["goal_version_id"],
            "requirements": rows,
            "groups": payload["groups"],
            "competence_verified": False,
        }
    }
