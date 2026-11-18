"""Public native input regressions; local scoring is not answer approval.

The three questions and reviewed source packs come from the same authorized
continuous trial, not Gold/holdout. Expected IDs are test assertions only.
"""

from __future__ import annotations

import json
from copy import deepcopy
from itertools import permutations
from pathlib import Path

import pytest

from careerforge.services.retrieval_relevance import rank_reviewed_facts
from careerforge.util import canonical, tokens

NATIVE = json.loads(
    (Path(__file__).parent / "fixtures/native_public_retrieval_inputs.json").read_text()
)
EXPECTED_FACTS = [
    "1e4b3ead-38f7-4c3e-88d5-748830a3255b",
    "1e4b3ead-38f7-4c3e-88d5-748830a3255b",
    "899ca4f7-92a6-479e-9170-178dc72d3afc",
]


def bounded_whole_packs(packs, ranked, cap=12000):
    selected, used = [], 0
    for publication_id in ranked:
        size = len(canonical(packs[publication_id]).encode("utf-8"))
        if used + size <= cap:
            selected.append(packs[publication_id])
            used += size
    return selected, used


@pytest.mark.parametrize("index", range(3))
def test_original_three_questions_retrieve_known_facts_under_same_byte_cap(index):
    packs = {p["publication_id"]: p for p in NATIVE["publications"]}
    # Evidence relevance must not depend on random publication UUID tie-breaks
    # or prior lexical candidate order, including the observed incorrect order.
    for order in permutations(packs):
        ranked, scores = rank_reviewed_facts(
            packs, list(order), tokens(" ".join(NATIVE["questions"][index]["aliases"]))
        )
        selected, used = bounded_whole_packs(packs, ranked)
        returned = {fact["fact_atom_id"] for unit in selected for fact in unit["facts"]}
        assert EXPECTED_FACTS[index] in returned
        assert used <= 12000
        assert all(unit == packs[unit["publication_id"]] for unit in selected)
        assert scores[0]["strongest_fact_score"] > scores[1]["strongest_fact_score"]
        assert bounded_whole_packs(packs, ranked, 100) == ([], 0)


@pytest.mark.parametrize("index", range(3))
def test_renaming_candidate_ids_or_metadata_cannot_choose_the_answer(index):
    packs = {
        "renamed-" + str(2 - position): {
            **deepcopy(pack),
            "publication_id": "renamed-" + str(2 - position),
        }
        for position, pack in enumerate(NATIVE["publications"])
    }
    question = NATIVE["questions"][index]
    # A leading metadata-only lure is not a reviewed assertion about the query.
    lure = deepcopy(next(iter(packs.values())))
    lure.update(
        publication_id="000-metadata-lure",
        title=question["question"],
        facts=[
            {
                "fact_atom_id": "lure-fact",
                "claim_id": "lure-claim",
                "text": "Unrelated archival inventory.",
                "conditions": [],
            }
        ],
    )
    packs[lure["publication_id"]] = lure
    ranked, _ = rank_reviewed_facts(
        packs, [lure["publication_id"], *sorted(packs)], tokens(" ".join(question["aliases"]))
    )
    assert ranked[0] != lure["publication_id"]
    selected, _ = bounded_whole_packs(packs, ranked)
    assert EXPECTED_FACTS[index] in {f["fact_atom_id"] for p in selected for f in p["facts"]}


def test_conditions_are_reviewed_evidence_even_when_old_index_misses_them():
    packs = {
        "metadata": {
            "facts": [{"fact_atom_id": "other", "text": "An unrelated record.", "conditions": []}]
        },
        "condition": {
            "facts": [
                {
                    "fact_atom_id": "condition-fact",
                    "text": "The operation raises Failure.",
                    "conditions": ["Only while the circuit breaker is open."],
                }
            ]
        },
    }
    ranked, scores = rank_reviewed_facts(
        packs, ["metadata"], tokens("Explain circuit breaker open condition")
    )
    assert ranked == ["condition", "metadata"]
    assert scores[0]["matching_fact_ids"] == ["condition-fact"]


def test_repetition_cannot_outscore_focused_reviewed_fact():
    packs = {
        "broad": {
            "facts": [{"fact_atom_id": "broad-fact", "text": "operation " * 5000, "conditions": []}]
        },
        "focused": {
            "facts": [
                {
                    "fact_atom_id": "focused-fact",
                    "text": "The operation rejects a nonce mismatch.",
                    "conditions": [],
                }
            ]
        },
    }
    ranked, _ = rank_reviewed_facts(
        packs, ["broad", "focused"], tokens("Explain nonce mismatch operation")
    )
    assert ranked == ["focused", "broad"]


def test_absent_fact_or_outside_scope_candidate_is_never_manufactured():
    packs = {
        p["publication_id"]: p
        for p in NATIVE["publications"]
        if not any(f["fact_atom_id"] == EXPECTED_FACTS[1] for f in p["facts"])
    }
    ranked, scores = rank_reviewed_facts(
        packs, ["outside-source", *packs], tokens(NATIVE["questions"][1]["question"])
    )
    assert set(ranked) <= set(packs)
    assert all(EXPECTED_FACTS[1] not in row["matching_fact_ids"] for row in scores)
    # An unrelated question gets no evidence score. Metadata fallback is not
    # labeled as evidence relevance and still requires the ordinary Answer Guard.
    ranked, scores = rank_reviewed_facts(packs, list(packs), tokens("Explain quantum entanglement"))
    assert ranked == list(packs)
    assert all(
        row["strongest_fact_score"] == 0 and row["matching_fact_ids"] == [] for row in scores
    )


@pytest.mark.asyncio
async def test_rc3_contract_retains_old_scoring_and_new_contract_partitions_cache(env, monkeypatch):
    from conftest import publish_fixture

    from careerforge.contracts import RetrievalTaskSpec, TaskType
    from careerforge.models import ObjectRow
    from careerforge.services.consumer_context import CONSUMER_CONTEXT_CONTRACT
    from careerforge.services.retrieval_context import LEGACY_RETRIEVAL_CONTEXT_CONTRACT

    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    spec = RetrievalTaskSpec(
        turn_id="rank-contract",
        task_type=TaskType.IMPLEMENTATION,
        user_question="Explain value",
        focus_revision=1,
        project_source_ids=[source],
    )
    original = app.cache.singleflight
    keys = []

    async def capture(layer, key, loader, ttl=30, *, publish_guard=None):
        keys.append(key)
        return await original(layer, key, loader, ttl, publish_guard=publish_guard)

    monkeypatch.setattr(app.cache, "singleflight", capture)
    current = await app.retrieval.retrieve(actor, spec)
    legacy = await app.retrieval.retrieve(
        actor, spec, retrieval_context_contract=LEGACY_RETRIEVAL_CONTEXT_CONTRACT
    )
    assert keys[0] != keys[1]
    with app.db.read() as db:
        current_manifest = db.get(ObjectRow, current["retrieval_manifest_id"]).payload
        legacy_manifest = db.get(ObjectRow, legacy["retrieval_manifest_id"]).payload
    assert current_manifest["reviewed_fact_relevance"]["facts_approved"] is False
    assert "reviewed_fact_relevance" not in legacy_manifest
    assert len(current["knowledge_units"]) == len(legacy["knowledge_units"]) == 1
    current_pack, legacy_pack = current["knowledge_units"][0], legacy["knowledge_units"][0]
    assert current_pack["consumer_context"]["contract"] == CONSUMER_CONTEXT_CONTRACT
    assert all(
        row["standalone"] is True for row in current_pack["consumer_context"]["facts"].values()
    )
    assert "consumer_context" not in legacy_pack
    assert {k: v for k, v in current_pack.items() if k != "consumer_context"} == legacy_pack
