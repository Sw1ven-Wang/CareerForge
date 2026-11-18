"""Bounded candidate decisions, not independent relevance-quality evaluation."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from careerforge.contracts import KnowledgeBuildInput, ModelOutput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, ModelReceipt
from careerforge.services.build import (
    ANALYSIS_TOPIC_CONTRACT,
    RESEARCH_ANALYSIS_SYSTEM,
    BuildService,
)
from careerforge.services.build_execution import plan_execution, validate_execution_manifest
from careerforge.services.candidate_selection import (
    CANDIDATE_SELECTION_VERSION,
    CandidateDecisionOutput,
    make_catalog,
    select_candidates,
    validate_decision,
)
from careerforge.services.security import create_principal


def catalog():
    return {
        "candidates": [
            {
                "candidate_id": "candidate-one",
                "observations": [{"observation_id": "observation-one"}],
                "source_reads": [],
            }
        ],
        "requirements": [{"requirement_id": "requirement-one", "modality": "required"}],
    }


def proposal():
    return {
        "selected_candidate_ids": ["candidate-one"],
        "decisions": [
            {
                "candidate_id": "candidate-one",
                "decision": "select",
                "reason_code": "possible_requirement_match",
                "observation_ids": ["observation-one"],
                "possible_related_requirements": [
                    {
                        "requirement_id": "requirement-one",
                        "basis": "metadata_hypothesis",
                        "observation_ids": ["observation-one"],
                        "source_quotes": [],
                    }
                ],
                "uncertainties": ["implementation_not_verified"],
            }
        ],
    }


def test_metadata_candidate_is_only_a_hypothesis_not_verified_implementation():
    result = validate_decision(CandidateDecisionOutput.model_validate(proposal()), catalog(), 1)
    assert result["selected_candidate_ids"] == ["candidate-one"]
    assert not result["semantic_quality_verified"] and not result["requirements_satisfied"]
    assert result["decisions"][0]["verified_implementation"] is False


@pytest.mark.parametrize(
    "change",
    ["candidate", "observation", "requirement", "source_quote", "limit", "missing_decision"],
)
def test_unknown_or_unsupported_selection_fails_closed(change):
    raw, data, limit = proposal(), catalog(), 1
    if change == "candidate":
        raw["selected_candidate_ids"] = ["candidate-forged"]
    elif change == "observation":
        raw["decisions"][0]["observation_ids"] = ["observation-forged"]
    elif change == "requirement":
        raw["decisions"][0]["possible_related_requirements"][0]["requirement_id"] = (
            "requirement-forged"
        )
    elif change == "source_quote":
        raw["decisions"][0]["possible_related_requirements"][0].update(
            basis="source_observed",
            source_quotes=[{"read_id": "read-forged", "quote": "fabricated code"}],
        )
    elif change == "limit":
        limit = 0
    else:
        raw["decisions"] = []
    with pytest.raises(DomainError):
        validate_decision(CandidateDecisionOutput.model_validate(raw), data, limit)


def test_observation_from_another_candidate_cannot_justify_selection():
    raw, data = proposal(), catalog()
    data["candidates"].append(
        {
            "candidate_id": "candidate-two",
            "observations": [{"observation_id": "observation-two"}],
            "source_reads": [],
        }
    )
    raw["decisions"].append(
        {
            "candidate_id": "candidate-two",
            "decision": "defer",
            "reason_code": "insufficient_observation",
            "observation_ids": [],
            "possible_related_requirements": [],
            "uncertainties": ["requirement_relevance_unverified"],
        }
    )
    raw["decisions"][0]["observation_ids"] = ["observation-two"]
    with pytest.raises(DomainError):
        validate_decision(CandidateDecisionOutput.model_validate(raw), data, 1)


def test_explicit_nonrequirement_cannot_be_used_as_a_positive_selection_target():
    data = deepcopy(catalog())
    data["requirements"][0]["modality"] = "not_required"
    with pytest.raises(DomainError):
        validate_decision(CandidateDecisionOutput.model_validate(proposal()), data, 1)


def test_source_observed_link_requires_a_unique_quote_from_this_candidate():
    raw, data = proposal(), catalog()
    data["candidates"][0]["source_reads"] = [
        {"read_id": "read-one", "content": "return bounded_result"}
    ]
    link = raw["decisions"][0]["possible_related_requirements"][0]
    link.update(
        basis="source_observed", source_quotes=[{"read_id": "read-one", "quote": "bounded_result"}]
    )
    result = validate_decision(CandidateDecisionOutput.model_validate(raw), data, 1)
    assert result["decisions"][0]["verified_implementation"] is False
    for quote in ("unobserved_result", ""):
        link["source_quotes"][0]["quote"] = quote
        with pytest.raises((DomainError, ValueError)):
            validate_decision(CandidateDecisionOutput.model_validate(raw), data, 1)


def request(env, **changes):
    return KnowledgeBuildInput.model_validate(
        {
            "namespace_id": env["ns"],
            "funding_id": env["funding"],
            "question": "Inspect candidates for a bounded tool execution example.",
            "max_projects": 1,
            "max_units_per_project": 1,
            "call_cap": 20,
            "idempotency_key": "candidate-choice",
            "candidate_selection_version": CANDIDATE_SELECTION_VERSION,
            **changes,
        }
    )


def test_selector_budget_is_reserved_only_for_new_automatic_discovery(env):
    enabled = request(env)
    new = plan_execution(enabled, False)
    old = plan_execution(enabled.model_copy(update={"candidate_selection_version": None}), False)
    assert new["candidate_selection_call_reserve"] == 1
    assert "candidate_selection_call_reserve" not in old
    assert new["minimum_call_count"] == old["minimum_call_count"] + 1
    direct = plan_execution(enabled.model_copy(update={"source_id": "explicit-source"}), False)
    assert "candidate_selection_call_reserve" not in direct
    with pytest.raises(DomainError, match="调用"):
        env["app"].build.start(
            env["actor"], enabled.model_copy(update={"call_cap": new["minimum_call_count"] - 1})
        )
    assert env["model"].calls == []


def test_old_analysis_prompt_is_not_rewritten_on_replay():
    old = SimpleNamespace(input_manifest={})
    new = SimpleNamespace(input_manifest={"analysis_topic_contract": ANALYSIS_TOPIC_CONTRACT})
    assert BuildService._analysis_system(old, True) == RESEARCH_ANALYSIS_SYSTEM
    assert "不是每个知识单元必须附带" in BuildService._analysis_system(new, True)


@pytest.mark.asyncio
async def test_automatic_build_consumes_ordered_candidate_choice_once(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    original = env["model"].complete
    snapshots = []
    snapshot = env["repos"].snapshot

    async def search(*args, **kwargs):
        return {
            "candidates": [
                {
                    "repository_id": name,
                    "name": "fixtures/" + name,
                    "url": "https://github.com/fixtures/" + name,
                }
                for name in ("first", "second")
            ],
            "synthetic": True,
        }

    async def download(url, *args, **kwargs):
        snapshots.append(url)
        return await snapshot(url, *args, **kwargs)

    async def choose(request):
        context = request["context"]
        if "candidate_catalog" not in context:
            return await original(request)
        candidates = context["candidate_catalog"]
        selected = candidates[-1]["candidate_id"]
        return ModelOutput(
            synthetic=True,
            structured={
                "selected_candidate_ids": [selected],
                "decisions": [
                    {
                        "candidate_id": row["candidate_id"],
                        "decision": "select" if row["candidate_id"] == selected else "defer",
                        "reason_code": "question_relevance_hypothesis"
                        if row["candidate_id"] == selected
                        else "budget_deferred",
                        "observation_ids": [obs["observation_id"] for obs in row["observations"]],
                        "possible_related_requirements": [],
                        "uncertainties": ["implementation_not_verified"],
                    }
                    for row in candidates
                ],
            },
        )

    monkeypatch.setattr(env["repos"], "search", search)
    monkeypatch.setattr(env["repos"], "snapshot", download)
    monkeypatch.setattr(env["model"], "complete", choose)
    body = request(env)
    run_id = app.build.start(actor, body)
    with app.db.read() as db:
        row = db.get(AgentRun, run_id)
        assert (
            validate_execution_manifest(row.input_manifest, row.call_cap)[
                "candidate_selection_call_reserve"
            ]
            == 1
        )
    await app.drain()
    result = app.runtime.stage(run_id, "result")
    assert result and result["publication_ids"]
    assert snapshots == ["https://github.com/fixtures/second"]
    selection = app.runtime.stage(run_id, "candidate_selection")
    assert selection["selected_candidates"][0]["name"] == "fixtures/second"
    assert not selection["semantic_quality_verified"]
    assert app.build.start(actor, body) == run_id
    with app.db.read() as db:
        receipts = list(
            db.scalars(
                select(ModelReceipt).where(
                    ModelReceipt.run_id == run_id, ModelReceipt.stage_key == "candidate.selection"
                )
            )
        )
        assert len(receipts) == 1


def observed_run(env, *, actor=None, namespace=None, funding=None, candidate=None):
    from careerforge.models import ResearchNode, SearchObservation

    app = env["app"]
    actor, namespace = actor or env["actor"], namespace or env["ns"]
    rid = app.runtime.create(
        actor,
        namespace,
        funding or env["funding"],
        "research",
        {"question": "Inspect a bounded observed candidate."},
        1,
        "selection-unit",
    )
    candidate = candidate or {
        "repository_id": "observed",
        "name": "fixtures/observed",
        "url": "https://github.com/fixtures/observed",
    }
    with app.db.transaction() as db:
        node = ResearchNode(
            run_id=rid, parent_id=None, depth=0, question="Observe", node_key="observed", payload={}
        )
        db.add(node)
        db.flush()
        db.add(
            SearchObservation(
                node_id=node.id, query_hash="0" * 64, payload={"candidates": [candidate]}
            )
        )
    epoch = app.runtime.claim(rid, "selection-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, rid)
    return run, epoch, {"candidates": [candidate], "access_dependencies": []}


@pytest.mark.asyncio
async def test_candidate_selection_respects_role_authorization_before_spending(env):
    app = env["app"]
    funding = app.runtime.create_funding(env["actor"], env["ns"], 2, roles=["extract"])
    run, epoch, result = observed_run(env, funding=funding)
    with pytest.raises(DomainError) as error:
        await select_candidates(
            app.runtime,
            app.gateway,
            run,
            epoch,
            "selection-worker",
            result,
            [],
            1,
            [],
            ["learning_state", "private_source"],
        )
    assert error.value.code == "EGRESS_OR_ROLE_DENIED"
    assert env["model"].calls == []
    with app.db.read() as db:
        assert db.get(AgentRun, run.id).calls_consumed == 0


@pytest.mark.asyncio
async def test_selection_replay_reuses_one_receipt_and_rejects_changed_observations(
    env, monkeypatch
):
    from careerforge.models import ResearchNode, SearchObservation

    app = env["app"]
    run, epoch, result = observed_run(env)
    calls = []

    async def choose(request):
        calls.append(request)
        candidate = request["context"]["candidate_catalog"][0]
        return ModelOutput(
            synthetic=True,
            structured={
                "selected_candidate_ids": [candidate["candidate_id"]],
                "decisions": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "decision": "select",
                        "reason_code": "question_relevance_hypothesis",
                        "observation_ids": [
                            row["observation_id"] for row in candidate["observations"]
                        ],
                        "uncertainties": ["implementation_not_verified"],
                    }
                ],
            },
        )

    monkeypatch.setattr(env["model"], "complete", choose)
    args = (
        app.runtime,
        app.gateway,
        run,
        epoch,
        "selection-worker",
        result,
        [],
        1,
        [],
        ["learning_state", "private_source"],
    )
    first = await select_candidates(*args)
    assert await select_candidates(*args) == first
    assert len(calls) == 1
    with app.db.transaction() as db:
        observation = db.scalar(
            select(SearchObservation).join(ResearchNode).where(ResearchNode.run_id == run.id)
        )
        observation.payload = {
            **observation.payload,
            "candidates": [{**result["candidates"][0], "description": "changed evidence"}],
        }
    with pytest.raises(DomainError) as error:
        await select_candidates(*args)
    assert error.value.code == "CANDIDATE_SELECTION_HASH"
    assert len(calls) == 1


def test_foreign_private_candidate_source_cannot_enter_the_catalog(env):
    from test_ingestion_build_v163 import import_fixture_source

    app = env["app"]
    source_id = import_fixture_source(env)
    with app.db.transaction() as db:
        other, namespace, _ = create_principal(db, "candidate-outsider")
    funding = app.runtime.create_funding(other.id, namespace.id, 2)
    candidate = {
        "repository_id": "private-source",
        "name": "fixtures/ingestion-v163",
        "url": "https://github.com/fixtures/ingestion-v163",
        "source_id": source_id,
        "commit": "a" * 40,
    }
    run, _, result = observed_run(
        env, actor=other.id, namespace=namespace.id, funding=funding, candidate=candidate
    )
    with pytest.raises(DomainError):
        make_catalog(app.runtime, run, result, [])
    assert env["model"].calls == []


def test_observed_repository_identity_does_not_authorize_a_different_pinned_source(env):
    from test_ingestion_build_v163 import import_fixture_source

    app = env["app"]
    source_id = import_fixture_source(env)
    candidate = {
        "repository_id": "pin",
        "name": "fixtures/ingestion-v163",
        "url": "https://github.com/fixtures/ingestion-v163",
    }
    run, _, result = observed_run(env, candidate=candidate)
    result["candidates"] = [{**candidate, "source_id": source_id, "commit": "a" * 40}]
    current, _, _ = make_catalog(app.runtime, run, result, [])
    assert current["candidates"][0]["observations"] == []
    identifier = current["candidates"][0]["candidate_id"]
    output = CandidateDecisionOutput.model_validate(
        {
            "selected_candidate_ids": [identifier],
            "decisions": [
                {
                    "candidate_id": identifier,
                    "decision": "select",
                    "reason_code": "question_relevance_hypothesis",
                    "observation_ids": [],
                }
            ],
        }
    )
    with pytest.raises(DomainError, match="缺少"):
        validate_decision(output, current, 1)


def test_new_jd_discovery_auto_enables_selection_but_direct_source_does_not(env):
    from test_ingestion_build_v163 import import_fixture_source

    from careerforge.contracts_jd_v166 import JDImportInput

    app = env["app"]
    document = app.jd.import_jd(
        env["actor"],
        JDImportInput(namespace_id=env["ns"], jd_id="bounded-role", raw_text="熟悉工具调用。"),
    )
    body = request(
        env,
        candidate_selection_version=None,
        jd_revision_id=document["jd_revision_id"],
        call_cap=24,
    )
    rid = app.build.start(env["actor"], body)
    with app.db.read() as db:
        manifest = db.get(AgentRun, rid).input_manifest
    assert manifest["candidate_selection_version"] == CANDIDATE_SELECTION_VERSION
    assert manifest["execution_plan"]["candidate_selection_call_reserve"] == 1
    assert app.build.start(env["actor"], body) == rid
    source = import_fixture_source(env)
    direct = app.build.start(
        env["actor"],
        body.model_copy(update={"source_id": source, "idempotency_key": "direct-source"}),
    )
    with app.db.read() as db:
        manifest = db.get(AgentRun, direct).input_manifest
    assert manifest["candidate_selection_version"] is None
    assert "candidate_selection_call_reserve" not in manifest["execution_plan"]


def test_bounded_analysis_schema_uses_exact_snapshot_dependency_paths():
    from careerforge.services.build import BuildService

    paths = ["packages/core/entry.ts", "packages/core/engine.ts"]
    schema = BuildService._analysis_schema([], source_paths=paths)
    item = schema["$defs"]["UnitDraft"]["properties"]["dependency_paths"]["items"]
    assert item["enum"] == sorted(paths)
    legacy = BuildService._analysis_schema([])
    assert "enum" not in legacy["$defs"]["UnitDraft"]["properties"]["dependency_paths"]["items"]
