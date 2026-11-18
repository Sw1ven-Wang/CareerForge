"""Real native counterexample plus synthetic protocol/authority regressions.

The recorded public source and role verdicts are not an answer-quality oracle.
No test calls a live model, modifies native evidence, or approves a dependency.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from conftest import publish_fixture
from test_bounded_fact_evidence_v166 import pack as synthetic_pack
from test_learning_loop_v16 import configure_planner, plan_input

from careerforge.contracts import RetrievalTaskSpec, TaskType
from careerforge.contracts_learning_ext import ActivityInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, ObjectRow, Publication, ReviewDecision
from careerforge.services.bounded_evidence import (
    EVIDENCE_PROJECTION_CONTRACT,
    byte_size,
    project_pack,
    select_bounded_facts,
)
from careerforge.services.consumer_context import (
    CONSUMER_CONTEXT_CONTRACT,
    CONTEXT_EVIDENCE_PROJECTION_CONTRACT,
    require_standalone_facts,
    standalone_fact_ids,
)
from careerforge.services.learning_projection import text_catalog
from careerforge.services.retrieval_context import (
    LEGACY_FACT_RETRIEVAL_CONTEXT_CONTRACT,
    RETRIEVAL_CONTEXT_CONTRACT,
)

NATIVE_PATH = Path(__file__).parent / "fixtures/native_consumer_context/file_search.json"
NATIVE_SHA256 = "9c61dc151934a8ae45f38086aee7660e42a68c276b43c7def2645e72676243cc"
NATIVE_TARGET = "223d6cf8-9952-4bb6-a7a0-96bbf228d3dc"


def witness(pack, permitted):
    return {
        **pack,
        "consumer_context": {
            "contract": CONSUMER_CONTEXT_CONTRACT,
            "publication_id": pack["publication_id"],
            "unit_version_id": pack["unit_version_id"],
            "facts": {
                f["fact_atom_id"]: {
                    "claim_id": f["claim_id"],
                    "standalone": f["fact_atom_id"] in permitted,
                }
                for f in pack["facts"]
            },
        },
    }


def native():
    data = NATIVE_PATH.read_bytes()
    assert hashlib.sha256(data).hexdigest() == NATIVE_SHA256
    raw = json.loads(data)
    permitted = {
        row["fact_atom_id"]
        for row in raw["coverage_elements"]
        if row["standalone"]
        and row["fact_bound"]
        and row["conditions_preserved"]
        and row["claim_id"] in raw["reviewer_standalone_claim_ids"]
    }
    return raw["pack"], witness(raw["pack"], permitted), (raw["bindings"], raw["citations"])


def choose(pack, support, *, question="max_num_results ranking_options", limit=20000, new=True):
    return select_bounded_facts(
        {pack["publication_id"]: pack},
        {pack["publication_id"]: support},
        question=question,
        variants=[question],
        limit=limit,
        per_project_limit=2,
        max_project_samples=1,
        selection_manifest_id="pure-test-selection",
        consumer_context_contract=CONSUMER_CONTEXT_CONTRACT if new else None,
    )


def test_native_original_failure_is_preserved_and_new_contract_omits_target():
    legacy, scoped, support = native()
    before = deepcopy(scoped)
    old, old_audit = choose(legacy, support, new=False)
    assert [f["fact_atom_id"] for u in old for f in u["facts"]] == [NATIVE_TARGET]
    assert old_audit["contract"] == EVIDENCE_PROJECTION_CONTRACT
    units, audit = choose(scoped, support)
    assert units == []
    assert audit["contract"] == CONTEXT_EVIDENCE_PROJECTION_CONTRACT
    assert any(
        r["fact_atom_id"] == NATIVE_TARGET and r["reason"] == "standalone_review_required"
        for r in audit["omitted_facts"]
    )
    assert scoped == before  # No rewrite, inferred dependency, or repaired pronoun.


def test_native_approved_fact_keeps_exact_conditions_and_complete_support():
    _, scoped, support = native()
    units, audit = choose(scoped, support, question="FileSearchTool")
    assert len(units) == 1
    ids = {f["fact_atom_id"] for f in units[0]["facts"]}
    assert ids == standalone_fact_ids(scoped) and NATIVE_TARGET not in ids
    assert units[0]["facts"] == [f for f in scoped["facts"] if f["fact_atom_id"] in ids]
    expected_refs = {
        r
        for fid in ids
        for kind in ("support_refs", "counterevidence_refs")
        for r in support[0][fid][kind]
    }
    assert {c["citation_id"] for c in units[0]["citations"]} == expected_refs
    assert audit["selected_bytes"] == sum(byte_size(u) for u in units)


@pytest.mark.parametrize("budget", [0, 1, 100, 500, 12000, 20000, 1000000])
def test_no_budget_can_make_a_contextual_fact_standalone(budget):
    _, scoped, support = native()
    units, audit = choose(scoped, support, limit=budget)
    assert units == [] and audit["selected_bytes"] == 0


def test_new_contract_byte_boundary_counts_independence_metadata():
    full, support = synthetic_pack()
    full["facets"] = []
    scoped = witness(full, {"p-a"})
    units, _ = choose(scoped, support, question="Alpha")
    exact = byte_size(units[0])
    assert choose(scoped, support, question="Alpha", limit=exact)[0] == units
    assert choose(scoped, support, question="Alpha", limit=exact - 1)[0] == []


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "unknown",
        "other_publication",
        "other_unit",
        "other_claim",
        "missing_fact",
        "not_boolean",
    ],
)
def test_missing_unknown_or_cross_unit_context_cannot_authorize_projection(change):
    _, scoped, support = native()
    fid = next(iter(standalone_fact_ids(scoped)))
    if change == "missing":
        scoped.pop("consumer_context")
    elif change == "unknown":
        scoped["consumer_context"]["contract"] = "future-unknown"
    elif change == "other_publication":
        scoped["consumer_context"]["publication_id"] = "foreign"
    elif change == "other_unit":
        scoped["consumer_context"]["unit_version_id"] = "foreign"
    elif change == "other_claim":
        scoped["consumer_context"]["facts"][fid]["claim_id"] = "foreign"
    elif change == "missing_fact":
        del scoped["consumer_context"]["facts"][fid]
    else:
        scoped["consumer_context"]["facts"][fid]["standalone"] = "true"
    with pytest.raises(DomainError):
        choose(scoped, support, question="FileSearchTool")


def test_projection_and_text_catalog_reject_indirect_context_bypass():
    _, scoped, support = native()
    with pytest.raises(DomainError, match="原单元"):
        project_pack(scoped, {NATIVE_TARGET}, *support, "selection")
    frozen = {"consumer_context_contract": CONSUMER_CONTEXT_CONTRACT, "units": [scoped]}
    catalog = text_catalog(frozen)
    assert not any(x.get("fact_atom_id") == NATIVE_TARGET for x in catalog.values())
    assert any(x.get("fact_atom_id") in standalone_fact_ids(scoped) for x in catalog.values())
    with pytest.raises(DomainError):
        text_catalog({**frozen, "consumer_context_contract": "unknown"})


async def contextual_fixture(env, monkeypatch, *, missing=False, one_sided=False):
    original = env["model"].complete

    async def complete(request):
        output = await original(request)
        if not request.get("schema"):
            return output
        value = deepcopy(output.structured)
        role, context = request["role"], request["context"]
        if role == "analysis":
            first = value["units"][0]["claims"][0]
            first["statement"] = "Alpha is the named configuration."
            second = deepcopy(first)
            second.update(key="contextual", statement="该配置包含 BetaOption。")
            value["units"][0]["claims"].append(second)
        elif role == "reviewer":
            if missing:
                value.pop("scope_review", None)
            elif not one_sided:
                value["scope_review"]["standalone_claim_ids"] = [
                    c["claim_id"]
                    for c in context["subject"]["claims"]
                    if c["statement"].startswith("Alpha")
                ]
        elif role == "coverage" and "scope_binding" in context:
            if missing:
                value.pop("scope_audit", None)
            else:
                safe = {
                    e["fact_atom_id"]
                    for e in context["subject"]["scope_elements"]
                    if e["text"].startswith("Alpha")
                }
                for check in value["scope_audit"]["elements"]:
                    check["standalone"] = check["fact_atom_id"] in safe
        return output.model_copy(update={"structured": value})

    monkeypatch.setattr(env["model"], "complete", complete)
    source, pub, run = await publish_fixture(env)
    with env["app"].db.read() as db:
        pack = env["app"].knowledge.publication_pack(
            db, env["actor"], pub, consumer_context_contract=CONSUMER_CONTEXT_CONTRACT
        )
    return source, pub, run, pack


def spec(source, question="BetaOption"):
    return RetrievalTaskSpec(
        turn_id="context-test",
        task_type=TaskType.IMPLEMENTATION,
        user_question=question,
        focus_revision=1,
        project_source_ids=[source],
        max_evidence_tokens=20000,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("one_sided", [False, True])
async def test_new_retrieval_and_direct_answer_refresh_require_both_role_approvals(
    env, monkeypatch, one_sided
):
    app, actor = env["app"], env["actor"]
    source, pub, _, pack = await contextual_fixture(env, monkeypatch, one_sided=one_sided)
    target = next(f["fact_atom_id"] for f in pack["facts"] if "BetaOption" in f["text"])
    calls = len(env["model"].calls)
    result = await app.retrieval.retrieve(actor, spec(source))
    assert result["knowledge_units"] == [] and len(env["model"].calls) == calls
    with pytest.raises(DomainError) as exc:
        app.answers._fresh_unit(actor, pack, target)
    assert exc.value.code == "FACT_CONTEXT_REQUIRED"
    # Historical r3 remains explicit and byte-shape compatible, not silently fixed.
    old = await app.retrieval.retrieve(
        actor, spec(source), retrieval_context_contract=LEGACY_FACT_RETRIEVAL_CONTEXT_CONTRACT
    )
    assert old["knowledge_units"][0]["facts"][0]["fact_atom_id"] == target
    assert "consumer_context" not in old["knowledge_units"][0]
    with app.db.read() as db:
        manifest = db.get(ObjectRow, result["retrieval_manifest_id"]).payload
        assert manifest["retrieval_context_contract"] == RETRIEVAL_CONTEXT_CONTRACT
        assert (
            manifest["evidence_projection"]["consumer_context_contract"]
            == CONSUMER_CONTEXT_CONTRACT
        )
        assert (
            db.get(ReviewDecision, db.get(Publication, pub).review_id).decision == "accept_scoped"
        )


@pytest.mark.asyncio
async def test_missing_legacy_standalone_fields_remain_unknown_for_new_admission(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    source, pub, _, pack = await contextual_fixture(env, monkeypatch, missing=True)
    assert standalone_fact_ids(pack) == set()
    assert {v["reason"] for v in pack["consumer_context"]["facts"].values()} == {
        "standalone_review_unavailable"
    }
    assert (await app.retrieval.retrieve(actor, spec(source, "Alpha")))["knowledge_units"] == []

    from test_session_plans_v166 import request

    call_count = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        app.learning.start_session_plan(actor, request(env, [pub], key="no-teachable-facts"))
    assert error.value.code == "FACT_CONTEXT_REQUIRED"
    assert len(env["model"].calls) == call_count


@pytest.mark.asyncio
async def test_learning_plan_activity_and_refreshed_answer_keep_same_scope(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    source, pub, _, pack = await contextual_fixture(env, monkeypatch)
    good = next(iter(standalone_fact_ids(pack)))
    bad = next(f["fact_atom_id"] for f in pack["facts"] if f["fact_atom_id"] != good)
    configure_planner(env, lambda v: v["modules"][0].update(focus_fact_ids=[good]))
    started = app.learning.start_personalized_plan(actor, plan_input(env, pub))
    with app.db.read() as db:
        frozen = db.get(AgentRun, started["run_id"]).input_manifest
        assert frozen["consumer_context_contract"] == CONSUMER_CONTEXT_CONTRACT
        assert not any(x.get("fact_atom_id") == bad for x in text_catalog(frozen).values())
    await app.drain()
    result = app.runtime.stage(started["run_id"], "result")
    assert result
    app.learning.accept_plan(actor, result["plan_id"], result["version_id"], 1)
    activity = app.learning.create_activity(actor, ActivityInput(module_id=result["module_ids"][0]))
    assert activity["specification"]["consumer_context_contract"] == CONSUMER_CONTEXT_CONTRACT
    assert [f["fact_atom_id"] for f in activity["specification"]["facts"]] == [good]
    assert (
        app.learning.activity_detail(actor, activity["activity_id"])["specification"]
        == activity["specification"]
    )
    with pytest.raises(DomainError):
        app.learning.create_activity(
            actor, ActivityInput(module_id=result["module_ids"][0], focus_fact_ids=[bad])
        )
    selected = await app.retrieval.retrieve(actor, spec(source, "Alpha"))
    candidates = app.answers._answer_candidates(
        actor, selected["knowledge_units"], selection_manifest_id=selected["retrieval_manifest_id"]
    )
    assert [f["fact_atom_id"] for u in candidates for f in u["facts"]] == [good]
    assert all(u["consumer_context"]["contract"] == CONSUMER_CONTEXT_CONTRACT for u in candidates)
    require_standalone_facts(candidates[0], {good})


@pytest.mark.asyncio
async def test_planner_cannot_select_contextual_fact_even_if_membership_is_valid(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    _, pub, _, pack = await contextual_fixture(env, monkeypatch)
    bad = next(
        f["fact_atom_id"]
        for f in pack["facts"]
        if f["fact_atom_id"] not in standalone_fact_ids(pack)
    )
    configure_planner(env, lambda v: v["modules"][0].update(focus_fact_ids=[bad]))
    started = app.learning.start_personalized_plan(actor, plan_input(env, pub))
    epoch = app.runtime.claim(started["run_id"], "test-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
    with pytest.raises(DomainError) as exc:
        await app.learning.execute_plan(run, epoch, "test-worker")
    assert exc.value.code == "FACT_CONTEXT_REQUIRED"
    assert app.runtime.stage(run.id, "result") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["response", "missing_receipt", "missing_witness"])
async def test_whole_unit_standalone_scope_requires_actual_completed_role_receipts(
    env, monkeypatch, corruption
):
    from sqlalchemy import select

    from careerforge.models import ModelReceipt, StageArtifact

    app, actor = env["app"], env["actor"]
    _, pub, _, _ = await contextual_fixture(env, monkeypatch)
    with app.db.transaction() as db:
        review = db.get(ReviewDecision, db.get(Publication, pub).review_id)
        receipt = db.scalar(
            select(ModelReceipt).where(
                ModelReceipt.run_id == review.reviewer_run_id, ModelReceipt.role == "coverage"
            )
        )
        if corruption == "response":
            changed = deepcopy(receipt.response)
            changed["structured"]["scope_audit"]["elements"][0]["standalone"] = False
            receipt.response = changed
        elif corruption == "missing_receipt":
            db.delete(receipt)
        else:
            witness_row = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == review.reviewer_run_id,
                    StageArtifact.stage_key == "model-access:" + receipt.id,
                )
            )
            db.delete(witness_row)
    # The old complete-publication wire contract is not silently rewritten.
    with app.db.read() as db:
        assert len(app.knowledge.publication_pack(db, actor, pub)["facts"]) == 2
        with pytest.raises(DomainError):
            app.knowledge.publication_pack(
                db, actor, pub, consumer_context_contract=CONSUMER_CONTEXT_CONTRACT
            )


def test_multiple_projects_and_publication_limits_cannot_supply_an_inferred_antecedent():
    packs, supports = {}, {}
    for name, source in [("a", "s1"), ("b", "s1"), ("c", "s2")]:
        p, support = synthetic_pack(name, source=source)
        packs[name] = witness(p, {name + "-a"})
        supports[name] = support
    units, audit = select_bounded_facts(
        packs,
        supports,
        question="Alpha Beta",
        variants=["Alpha Beta"],
        limit=20000,
        per_project_limit=1,
        max_project_samples=1,
        selection_manifest_id="bounded-project-test",
        consumer_context_contract=CONSUMER_CONTEXT_CONTRACT,
    )
    assert len(units) == 1
    assert all(f["fact_atom_id"].endswith("-a") for u in units for f in u["facts"])
    reasons = {r["reason"] for r in audit["omitted_facts"]}
    assert {
        "standalone_review_required",
        "project_sample_limit",
        "per_project_publication_limit",
    } <= reasons
    assert audit["selected_bytes"] <= 20000


@pytest.mark.asyncio
async def test_new_turn_commit_and_citation_navigation_cannot_reintroduce_contextual_fact(
    env, monkeypatch
):
    from careerforge.contracts import TurnInput
    from careerforge.models import Turn
    from careerforge.services.dependencies import require_object

    app, actor = env["app"], env["actor"]
    source, pub, _, pack = await contextual_fixture(env, monkeypatch)
    good = next(iter(standalone_fact_ids(pack)))
    bad = next(f for f in pack["facts"] if f["fact_atom_id"] != good)
    selected = await app.retrieval.retrieve(actor, spec(source, "Alpha"))
    fresh = app.answers._fresh_unit(
        actor,
        selected["knowledge_units"][0],
        good,
        selection_manifest_id=selected["retrieval_manifest_id"],
    )
    conv = app.conversations.create(actor)
    submitted = app.conversations.submit(
        actor,
        conv,
        TurnInput(
            question="Explain Alpha", expected_revision=1, idempotency_key="consumer-context-commit"
        ),
    )
    block = {
        "kind": "repository_fact",
        "text": fresh["facts"][0]["text"],
        "fact_atom_ids": [good],
        "publication_ids": [pub],
        "conditions": fresh["facts"][0]["conditions"],
        "citations": fresh["citations"],
        "checked": "exact",
    }
    with pytest.raises(DomainError):
        app.answers.commit_block(
            actor,
            submitted["turn_id"],
            {**block, "text": bad["text"], "fact_atom_ids": [bad["fact_atom_id"]]},
            selected["retrieval_manifest_id"],
            0,
        )
    block_id = app.answers.commit_block(
        actor, submitted["turn_id"], block, selected["retrieval_manifest_id"], 0
    )
    with app.db.read() as db:
        _, target = app.learning._navigation_target(db, actor, block_id, 0)
        assert target["source_id"] == source
        assert (
            db.get(Turn, submitted["turn_id"]).focus["retrieval_context_contract"]
            == RETRIEVAL_CONTEXT_CONTRACT
        )
        assert require_object(db, actor, block_id, "answer_block").payload["fact_atom_ids"] == [
            good
        ]


@pytest.mark.asyncio
async def test_new_deterministic_plan_also_filters_contextual_facts(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    _, pub, _, pack = await contextual_fixture(env, monkeypatch)
    result = app.learning.propose_plan(actor, [pub], "function", "Alpha")
    app.learning.accept_plan(actor, result["plan_id"], result["version_id"], 1)
    activity = app.learning.create_activity(actor, ActivityInput(module_id=result["module_ids"][0]))
    assert {f["fact_atom_id"] for f in activity["specification"]["facts"]} == standalone_fact_ids(
        pack
    )
