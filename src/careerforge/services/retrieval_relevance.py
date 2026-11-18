"""Rank current reviewed evidence above incidental publication metadata hits.

This is bounded local lexical relevance, not entailment or fact approval. The
caller supplies only fully validated, source/ACL/topic-eligible publications.
Independent answer validation remains necessary even for the highest score.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from ..util import tokens

REVIEWED_RELEVANCE_CONTRACT = "reviewed-fact-idf-1.6.6"
_STOP_WORDS = frozenset(
    "a an and are as at be by can could did do does explain for from how i in is it "
    "me of on or please should that the their then there these this to was what when "
    "where which why will with would you your".split()
)


def _terms(text: str) -> set[str]:
    return {term for term in tokens(text) if len(term) > 1 and term not in _STOP_WORDS}


def rank_reviewed_facts(
    packs: dict[str, dict[str, Any]], lexical: list[str], query_terms: list[str]
) -> tuple[list[str], list[dict[str, Any]]]:
    """Prefer a focused fact match; metadata remains a recall-only fallback.

    Conditions are part of the reviewed assertion, not optional search metadata.
    Inverse fact frequency discounts ubiquitous terms. The strongest complete
    fact determines primary relevance, so many weak hits across a large unit do
    not beat one focused assertion. Distinct-term coverage breaks genuine ties;
    repeated facts/words cannot accumulate a score. Existing lexical order is
    retained only when evidence scores are equal or no fact matches.
    """
    query = set(query_terms) - _STOP_WORDS
    query = {term for term in query if len(term) > 1}
    fact_terms = {
        (publication_id, fact["fact_atom_id"]): _terms(
            " ".join([fact["text"], *fact["conditions"]])
        )
        for publication_id, pack in packs.items()
        for fact in pack["facts"]
    }
    frequency = Counter(term for terms in fact_terms.values() for term in terms)
    count = len(fact_terms)

    def score(terms: set[str]) -> float:
        return round(
            sum(
                math.log(1 + (count - frequency[term] + 0.5) / (frequency[term] + 0.5))
                for term in sorted(query & terms)
            ),
            12,
        )

    scores: dict[str, tuple[float, float]] = {}
    matches: dict[str, list[str]] = {}
    for publication_id, pack in packs.items():
        terms = [fact_terms[publication_id, fact["fact_atom_id"]] for fact in pack["facts"]]
        scores[publication_id] = (
            max((score(value) for value in terms), default=0.0),
            score(set().union(*terms)),
        )
        matches[publication_id] = [
            fact["fact_atom_id"]
            for fact in pack["facts"]
            if query & fact_terms[publication_id, fact["fact_atom_id"]]
        ]
    # A freshly validated condition may not yet occur in an older search index.
    # Add its positive match within the same eligible publication set only.
    candidates = list(
        dict.fromkeys(
            [p for p in lexical if p in packs]
            + sorted(p for p in packs if scores[p][0] > 0 and p not in lexical)
        )
    )
    prior_order = {publication_id: index for index, publication_id in enumerate(candidates)}
    ranked = sorted(
        candidates,
        key=lambda p: (-scores[p][0], -scores[p][1], prior_order[p]),
    )
    return ranked, [
        {
            "publication_id": p,
            "strongest_fact_score": scores[p][0],
            "distinct_term_score": scores[p][1],
            "matching_fact_ids": matches[p],
        }
        for p in ranked
    ]
