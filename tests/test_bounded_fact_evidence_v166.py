"""Synthetic mechanism/security checks, not claims of native answer quality."""

from __future__ import annotations

from copy import deepcopy
from itertools import permutations

import pytest
from conftest import publish_fixture

from careerforge.contracts import ModelOutput, RetrievalTaskSpec, TaskType, ToolCall
from careerforge.errors import DomainError
from careerforge.models import FactAtom, ObjectRow
from careerforge.services.bounded_evidence import (
    byte_size,
    project_pack,
    select_bounded_facts,
)
from careerforge.services.consumer_context import CONTEXT_EVIDENCE_PROJECTION_CONTRACT
from careerforge.services.retrieval_context import (
    LEGACY_RETRIEVAL_CONTEXT_CONTRACT,
    RETRIEVAL_CONTEXT_CONTRACT,
    WHOLE_PACK_RETRIEVAL_CONTEXT_CONTRACT,
)
from careerforge.services.security import create_principal, set_grant
from careerforge.util import fingerprint


def pack(pid="p", *, source="s", support_size=20):
    facts = [
        {
            "fact_atom_id": pid + "-a",
            "claim_id": pid + "-ca",
            "text": "Alpha rejects invalid input.",
            "conditions": ["Only with the circuit open."],
        },
        {
            "fact_atom_id": pid + "-b",
            "claim_id": pid + "-cb",
            "text": "Beta returns an unrelated result.",
            "conditions": ["Not a runtime observation."],
        },
    ]
    citations = [
        {
            "citation_id": pid + "-e" + str(i),
            "source_id": source,
            "repository": "synthetic/project",
            "commit": "a" * 40,
            "path": "source.py",
            "start_line": i + 1,
            "end_line": i + 1,
            "quote": "多字节" * support_size,
            "file_hash": "file-hash",
            "quote_hash": "quote-hash-" + str(i),
            "read_id": "read-" + str(i),
        }
        for i in range(3)
    ]
    value = {
        "publication_id": pid,
        "unit_version_id": pid + "-v",
        "unit_id": pid + "-u",
        "unit_type": "implementation",
        "origin_kind": "repository",
        "title": "Synthetic unrelated publication label",
        "source_id": source,
        "repository": "synthetic/project",
        "commit": "a" * 40,
        "namespace_id": "namespace",
        "topic_links": [
            {
                "id": pid + "-topic",
                "topic_key": "code.python",
                "relation": "illustrates",
                "topic_version_id": "topic-v1",
            }
        ],
        "facets": [
            {"id": pid + "-facet-a", "claim_ids": [pid + "-ca"], "value": "Alpha boundary"},
            {
                "id": pid + "-facet-ab",
                "claim_ids": [pid + "-ca", pid + "-cb"],
                "value": "Whole-unit summary",
            },
        ],
        "facts": facts,
        "citations": citations[:2],
        "limitations": ["Complete publication boundary; no execution."],
        "runtime_observed": False,
        "synthetic": True,
    }
    bindings = {
        pid + "-a": {"support_refs": [pid + "-e0"], "counterevidence_refs": [pid + "-e2"]},
        pid + "-b": {"support_refs": [pid + "-e1"], "counterevidence_refs": []},
    }
    return value, (bindings, {c["citation_id"]: c for c in citations})


def choose(packs, supports, *, question="Alpha circuit", limit=12000, **changes):
    return select_bounded_facts(
        packs,
        supports,
        question=question,
        variants=[question],
        limit=limit,
        per_project_limit=changes.get("per_project_limit", 3),
        max_project_samples=changes.get("max_project_samples", 3),
        selection_manifest_id="selection-0000000000000000000000000000",
    )


def test_comparison_reserves_both_sides_and_all_facet_dependencies():
    a, sa = pack("a", source="first")
    b, sb = pack("b", source="second")
    for value in (a, b):
        for facet, key in zip(value["facets"], ("argument_validation", "dispatch"), strict=True):
            facet.update(topic_key="code.python", facet_key=key, value_status="known")
    units, manifest = select_bounded_facts(
        {"a": a, "b": b},
        {"a": sa, "b": sb},
        question="Alpha circuit",
        variants=[],
        limit=12000,
        per_project_limit=1,
        max_project_samples=2,
        selection_manifest_id="paired-selection",
        paired_topic_keys=["code.python"],
        paired_source_ids=["first", "second"],
    )
    assert {unit["source_id"] for unit in units} == {"first", "second"}
    assert all(len(unit["facts"]) == len(unit["facets"]) == 2 for unit in units)
    assert manifest["selected_bytes"] == sum(byte_size(unit) for unit in units)


def test_complete_fact_support_counterevidence_conditions_and_hashes_are_identical():
    full, support = pack()
    before = deepcopy(full)
    units, manifest = choose({"p": full}, {"p": support})
    unit = units[0]
    assert unit["facts"] == [full["facts"][0]]
    assert unit["citations"] == [support[1]["p-e0"], support[1]["p-e2"]]
    assert unit["facets"] == [full["facets"][0]]
    assert unit["limitations"] == full["limitations"]
    assert unit["evidence_projection"]["publication_hash"] == fingerprint(full)
    assert manifest["units"][0]["fact_support"] == {"p-a": support[0]["p-a"]}
    assert manifest["units"][0]["omitted_facet_ids"] == ["p-facet-ab"]
    assert manifest["selected_bytes"] == byte_size(unit)
    assert full == before
    unit["evidence_projection"]["metadata_scope"]["facets"] = "TAMPERED"
    manifest["metadata_scope"]["facets"] = "TAMPERED"
    again, audit = choose({"p": full}, {"p": support})
    assert (
        again[0]["evidence_projection"]["metadata_scope"]["facets"]
        == "all_referenced_claims_selected"
    )
    assert audit["metadata_scope"]["facets"] == "all_referenced_claims_selected"


def test_exact_byte_boundary_includes_every_support_and_one_byte_less_omits_whole_fact():
    full, support = pack()
    full["facets"] = []  # No optional annotation can be omitted instead of support.
    units, _ = choose({"p": full}, {"p": support})
    exact = byte_size(units[0])
    assert choose({"p": full}, {"p": support}, limit=exact)[0] == units
    omitted, audit = choose({"p": full}, {"p": support}, limit=exact - 1)
    assert omitted == [] and audit["selected_bytes"] == 0
    assert audit["omitted_facts"][0]["reason"] == "single_fact_exceeds_budget"
    assert audit["omitted_facts"][0]["complete_fact_bytes"] == exact


@pytest.mark.parametrize("budget", [0, 1, 100])
def test_zero_and_small_budgets_have_no_negative_or_unaccounted_costs(budget):
    full, support = pack()
    units, audit = choose({"p": full}, {"p": support}, limit=budget)
    assert units == [] and audit["selected_bytes"] == 0
    assert audit["omitted_facts"][0]["reason"] == "single_fact_exceeds_budget"
    assert audit["omitted_facts"][0]["remaining_bytes"] == budget


def test_one_oversize_fact_is_not_trimmed_and_does_not_block_a_smaller_other_publication():
    large, ls = pack("large", support_size=2000)
    small, ss = pack("small")
    units, audit = choose({"large": large, "small": small}, {"large": ls, "small": ss}, limit=4000)
    assert [u["publication_id"] for u in units] == ["small"]
    assert audit["selected_bytes"] <= 4000
    assert any(
        r["publication_id"] == "large" and r["reason"] == "single_fact_exceeds_budget"
        for r in audit["omitted_facts"]
    )


def test_multiple_facts_multiple_publications_and_shared_citations_have_exact_bounded_cost():
    full, support = pack("one")
    other, more = pack("two", source="s2")
    # Shared support is retained once; counters remain separately required.
    support[0]["one-b"]["support_refs"] = ["one-e0"]
    units, audit = choose(
        {"one": full, "two": other},
        {"one": support, "two": more},
        question="Alpha Beta circuit",
        limit=7000,
    )
    assert len(units) == 2 and sum(len(u["facts"]) for u in units) == 4
    assert audit["selected_bytes"] == sum(byte_size(u) for u in units) <= 7000
    one = next(u for u in units if u["publication_id"] == "one")
    assert [c["citation_id"] for c in one["citations"]] == ["one-e0", "one-e2"]


def test_selection_ties_are_stable_for_all_input_permutations_and_enforce_project_limits():
    data = [pack(p, source=s) for p, s in [("a", "s1"), ("b", "s1"), ("c", "s2")]]
    results = []
    for order in permutations(data):
        units, audit = choose(
            {p["publication_id"]: p for p, _ in order},
            {p["publication_id"]: s for p, s in order},
            per_project_limit=1,
            max_project_samples=1,
        )
        results.append((units, audit))
    assert all(r == results[0] for r in results)
    assert [u["publication_id"] for u in results[0][0]] == ["a"]
    assert {r["reason"] for r in results[0][1]["omitted_facts"]} >= {
        "project_sample_limit",
        "per_project_publication_limit",
    }


def test_metadata_lure_and_repetition_cannot_overrule_reviewed_evidence():
    full, support = pack("focus")
    lure, ls = pack("lure")
    lure["title"] = "Alpha circuit " * 200
    for f in lure["facts"]:
        f["text"], f["conditions"] = "Unrelated " * 300, []
    units, audit = choose({"lure": lure, "focus": full}, {"lure": ls, "focus": support}, limit=4000)
    assert [u["publication_id"] for u in units] == ["focus"]
    assert audit["fact_ranking"][0]["publication_id"] == "focus"


def test_large_optional_facet_is_omitted_before_declaring_a_complete_fact_oversize():
    full, support = pack()
    full["facets"][0]["value"] = "Optional annotation " * 1200
    assert byte_size(full) > 12000
    units, audit = choose({"p": full}, {"p": support}, limit=4000)
    assert len(units) == 1 and units[0]["facts"] == [full["facts"][0]]
    assert units[0]["facets"] == []
    assert units[0]["citations"] == [support[1]["p-e0"], support[1]["p-e2"]]
    assert audit["units"][0]["omitted_facet_ids"] == ["p-facet-a", "p-facet-ab"]
    assert audit["selected_bytes"] == byte_size(units[0]) <= 4000


async def multi_fixture(env):
    def analysis(request):
        if not request.get("schema"):
            return ModelOutput(
                synthetic=True,
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish_investigation",
                        arguments={"reason": "evidence_sufficient"},
                    )
                ],
                finish_reason="tool_calls",
            )
        r = next(
            r for r in request["context"]["reading"]["reads"] if r["path"].endswith("tools.py")
        )
        return ModelOutput(
            synthetic=True,
            structured={
                "units": [
                    {
                        "subject_key": "two_synthetic_facts",
                        "unit_type": "function",
                        "title": "Synthetic selector test",
                        "claims": [
                            {
                                "key": name,
                                "statement": name + " synthetic fixture returns value.",
                                "conditions": ["Only this synthetic condition."],
                                "support": [{"read_id": r["read_id"], "quote": "return value"}],
                            }
                            for name in ["Alpha", "Beta"]
                        ],
                        "topics": [{"topic_key": "agent.tool_calling", "relation": "illustrates"}],
                        "limitations": ["Synthetic validation only."],
                    }
                ],
                "unanswered_questions": [],
            },
        )

    env["model"].overrides["analysis"] = analysis
    source, publication, _ = await publish_fixture(env)
    return source, publication


def spec(source):
    return RetrievalTaskSpec(
        turn_id="bounded-test",
        task_type=TaskType.IMPLEMENTATION,
        user_question="Alpha",
        focus_revision=1,
        project_source_ids=[source],
    )


@pytest.mark.asyncio
async def test_projected_cache_and_stage_bodies_are_never_authority(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    source, publication = await multi_fixture(env)
    before = len(env["model"].calls)

    async def poison(*args, **kwargs):
        return {
            "ids": [],
            "trace": {},
            "query_rewrite": {"variants": ["Beta"]},
            "knowledge_units": [{"publication_id": publication, "facts": [{"text": "INJECTED"}]}],
        }

    monkeypatch.setattr(app.cache, "singleflight", poison)
    result = await app.retrieval.retrieve(actor, spec(source))
    assert len(result["knowledge_units"]) == 1
    unit = result["knowledge_units"][0]
    assert len(unit["facts"]) == 1 and unit["facts"][0]["text"].startswith("Alpha")
    assert result["query_normalization"]["variants"] == ["Alpha"]
    assert unit["evidence_projection"]["contract"] == CONTEXT_EVIDENCE_PROJECTION_CONTRACT
    tampered = deepcopy(unit)
    tampered["facts"][0]["text"] = "INJECTED"
    tampered["facts"][0]["conditions"] = []
    tampered["citations"][0]["quote"] = "INJECTED"
    fresh = app.answers._answer_candidates(
        actor, [tampered], selection_manifest_id=result["retrieval_manifest_id"]
    )[0]
    assert fresh["facts"] == unit["facts"] and fresh["citations"] == unit["citations"]
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_marker", [False, True])
async def test_projection_cannot_add_a_canonical_but_omitted_fact(env, remove_marker):
    app, actor = env["app"], env["actor"]
    source, publication = await multi_fixture(env)
    result = await app.retrieval.retrieve(actor, spec(source))
    with app.db.read() as db:
        full = app.knowledge.publication_pack(db, actor, publication)
    unit = deepcopy(result["knowledge_units"][0])
    unit["facts"] = [next(f for f in full["facts"] if f["text"].startswith("Beta"))]
    if remove_marker:
        unit.pop("evidence_projection")
    with pytest.raises(DomainError) as failure:
        app.answers._answer_candidates(
            actor, [unit], selection_manifest_id=result["retrieval_manifest_id"]
        )
    assert failure.value.code == "ANSWER_SCOPE"


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["fact", "manifest"])
async def test_stale_or_tampered_authority_blocks_projection_refresh(env, corruption):
    app, actor = env["app"], env["actor"]
    source, _ = await multi_fixture(env)
    result = await app.retrieval.retrieve(actor, spec(source))
    with app.db.transaction() as db:
        if corruption == "fact":
            db.get(
                FactAtom, result["knowledge_units"][0]["facts"][0]["fact_atom_id"]
            ).approved_text = "TAMPERED"
        else:
            row = db.get(ObjectRow, result["retrieval_manifest_id"])
            row.payload = {**row.payload, "evidence_projection": {"contract": "fake"}}
    with pytest.raises(DomainError):
        app.answers._answer_candidates(
            actor, result["knowledge_units"], selection_manifest_id=result["retrieval_manifest_id"]
        )


@pytest.mark.asyncio
async def test_authorization_revocation_blocks_cached_and_selected_projection(env):
    app, owner = env["app"], env["actor"]
    source, _ = await multi_fixture(env)
    with app.db.transaction() as db:
        reader, _, _ = create_principal(db, "bounded-reader")
        reader_id = reader.id
        set_grant(db, owner, env["ns"], reader_id, "reader", True)
    result = await app.retrieval.retrieve(reader_id, spec(source))
    with app.db.transaction() as db:
        set_grant(db, owner, env["ns"], reader_id, "reader", False)
    with pytest.raises(DomainError):
        await app.retrieval.retrieve(reader_id, spec(source))
    with pytest.raises(DomainError):
        app.answers._answer_candidates(
            reader_id,
            result["knowledge_units"],
            selection_manifest_id=result["retrieval_manifest_id"],
        )


@pytest.mark.asyncio
async def test_stale_optional_metadata_requires_a_fresh_projection(env):
    app, actor = env["app"], env["actor"]
    source, _ = await multi_fixture(env)
    result = await app.retrieval.retrieve(actor, spec(source))
    link_id = result["knowledge_units"][0]["topic_links"][0]["id"]
    with app.db.transaction() as db:
        db.get(ObjectRow, link_id).state = "revoked"
    with pytest.raises(DomainError) as failure:
        app.answers._answer_candidates(
            actor, result["knowledge_units"], selection_manifest_id=result["retrieval_manifest_id"]
        )
    assert failure.value.code == "EVIDENCE_PROJECTION_CHANGED"
    refreshed = await app.retrieval.retrieve(actor, spec(source))
    assert refreshed["knowledge_units"][0]["topic_links"] == []
    assert app.answers._answer_candidates(
        actor,
        refreshed["knowledge_units"],
        selection_manifest_id=refreshed["retrieval_manifest_id"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contract", [None, LEGACY_RETRIEVAL_CONTEXT_CONTRACT, WHOLE_PACK_RETRIEVAL_CONTEXT_CONTRACT]
)
async def test_old_contracts_keep_complete_original_pack_and_new_marker_is_distinct(env, contract):
    app, actor = env["app"], env["actor"]
    source, publication = await multi_fixture(env)
    old = await app.retrieval.retrieve(actor, spec(source), retrieval_context_contract=contract)
    new = await app.retrieval.retrieve(actor, spec(source))
    with app.db.read() as db:
        assert old["knowledge_units"] == [app.knowledge.publication_pack(db, actor, publication)]
        assert "evidence_projection" not in db.get(ObjectRow, old["retrieval_manifest_id"]).payload
        manifest = db.get(ObjectRow, new["retrieval_manifest_id"]).payload
    assert manifest["retrieval_context_contract"] == RETRIEVAL_CONTEXT_CONTRACT
    assert RETRIEVAL_CONTEXT_CONTRACT != WHOLE_PACK_RETRIEVAL_CONTEXT_CONTRACT
    assert len(new["knowledge_units"][0]["facts"]) == 1
    assert manifest["evidence_budget"]["selected_bytes"] == sum(
        byte_size(u) for u in new["knowledge_units"]
    )


def test_unknown_or_empty_selected_fact_ids_fail_closed():
    full, support = pack()
    for selected in [set(), {"missing"}]:
        with pytest.raises(DomainError):
            project_pack(full, selected, *support, "selection")


@pytest.mark.asyncio
async def test_service_zero_byte_budget_preserves_scope_omits_complete_facts_and_spends_no_calls(
    env,
):
    from careerforge.models import AgentRun

    app, actor = env["app"], env["actor"]
    source, publication = await multi_fixture(env)
    env["model"].supports_embeddings = env["model"].supports_reranking = True
    run_id = app.runtime.create(
        actor, env["ns"], env["funding"], "research", {}, 4, "zero-evidence"
    )
    epoch = app.runtime.claim(run_id, "zero-worker")
    request = RetrievalTaskSpec.model_validate(
        {
            **spec(source).model_dump(mode="json"),
            "task_type": TaskType.CONCEPT,
            "user_question": "unmatchedquux",
            "max_evidence_tokens": 0,
        }
    )
    before = len(env["model"].calls)
    result = await app.retrieval.retrieve(actor, request, (run_id, epoch, "zero-worker"))
    assert result["knowledge_units"] == [] and result["source_observations"] == []
    assert result["uncovered_questions"] == ["no_authorized_reviewed_match"]
    assert len(env["model"].calls) == before
    with app.db.read() as db:
        manifest = db.get(ObjectRow, result["retrieval_manifest_id"]).payload
        assert db.get(AgentRun, run_id).calls_consumed == 0
    assert manifest["task_spec"]["project_source_ids"] == [source]
    assert manifest["evidence_budget"] == {"unit": "utf8_bytes", "limit": 0, "selected_bytes": 0}
    assert manifest["omitted_publications"] == [publication]
    assert all(
        r["reason"] == "single_fact_exceeds_budget"
        for r in manifest["evidence_projection"]["omitted_facts"]
    )
