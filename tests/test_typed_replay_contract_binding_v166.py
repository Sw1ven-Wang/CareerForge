"""Synthetic compatibility/release checks, never evidence of current r3 gains."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_bounded_fact_evidence_v166 import multi_fixture, spec
from test_typed_execution_replay_v16 import execute, retrieval_setup, routing_setup

from careerforge.contracts_learning_ext import ReplayInput
from careerforge.errors import DomainError, LeaseLost
from careerforge.models import (
    AgentRun,
    EvalDataset,
    EvolutionEvaluation,
    FundingAgreement,
    ModelReceipt,
    ObjectRow,
)
from careerforge.models_learning_ext import PolicyReplayManifest
from careerforge.services.bounded_evidence import byte_size
from careerforge.services.evolution_replay import engine_versions
from careerforge.services.evolution_typed import (
    check_typed_execution_trace,
    check_typed_retrieval_release,
    execute_typed_case,
    score_typed_case,
    typed_execution_contract,
)
from careerforge.services.orchestration import Coordinator
from careerforge.services.retrieval import RetrievalService
from careerforge.services.retrieval_context import RETRIEVAL_CONTEXT_CONTRACT
from careerforge.util import fingerprint


def release_report(provenance="fixture"):
    return {
        "typed_execution_contract": typed_execution_contract(),
        "retrieval_evaluation_scope": "historical_whole_publication_selection_only",
        "admitted_retrieval_context_contract": RETRIEVAL_CONTEXT_CONTRACT,
        "current_retrieval_quality_validated": False,
        "fixture_process_only": provenance == "fixture",
    }


def test_unmarked_and_explicit_typed_contract_resolve_to_identical_historical_semantics():
    contract = typed_execution_contract()
    assert typed_execution_contract(contract) == contract
    assert contract["retrieval_context_contract"] is None
    contract["version"] = "tampered"
    assert typed_execution_contract()["version"] == "whole-publication-typed-execution-1.6.6"


@pytest.mark.parametrize(
    "marker", [{}, "r3", {"retrieval_context_contract": RETRIEVAL_CONTEXT_CONTRACT}]
)
def test_unknown_or_bounded_fact_typed_marker_cannot_reuse_whole_publication_scorer(marker):
    with pytest.raises(DomainError) as failure:
        typed_execution_contract(marker)
    assert failure.value.code == "TYPED_EXECUTION_CONTRACT"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_typed_executor_never_inherits_mutable_retrieval_default(monkeypatch, explicit):
    seen = []

    async def retrieve(self, *args, **kwargs):
        seen.append(kwargs)
        return {"knowledge_units": []}

    monkeypatch.setattr(RetrievalService, "retrieve", retrieve)
    service = SimpleNamespace(db=None, app=SimpleNamespace(knowledge=None, cache=None))
    case = {
        "task_type": "explain_implementation",
        "question": "Alpha",
        "topic_keys": [],
        "publication_ids": [],
        "frozen_units": [],
    }
    await execute_typed_case(
        service,
        None,
        ("run", 1, "worker"),
        "replay:0:baseline:",
        {"per_project_limit": 3},
        "policy",
        case,
        "retrieval_recipe",
        "actor",
        **({"execution_contract": typed_execution_contract()} if explicit else {}),
    )
    assert len(seen) == 1
    assert "retrieval_context_contract" in seen[0]
    assert seen[0]["retrieval_context_contract"] is None


@pytest.mark.parametrize("mode", ["test", "development", "production"])
@pytest.mark.parametrize("provenance", ["fixture", "human_gold"])
def test_historical_retrieval_evidence_cannot_certify_current_r3_release(mode, provenance):
    kwargs = {
        "mode": mode,
        "provenance": provenance,
        "admitted_retrieval_context_contract": RETRIEVAL_CONTEXT_CONTRACT,
    }
    if mode == "test" and provenance == "fixture":
        check_typed_retrieval_release(
            typed_execution_contract(), release_report(provenance), **kwargs
        )
    else:
        with pytest.raises(DomainError) as failure:
            check_typed_retrieval_release(
                typed_execution_contract(), release_report(provenance), **kwargs
            )
        assert failure.value.code == "REPLAY_DEPLOYMENT_CONTRACT"


@pytest.mark.parametrize(
    "changes",
    [
        {"current_retrieval_quality_validated": True},
        {"admitted_retrieval_context_contract": None},
        {"retrieval_evaluation_scope": "current_r3"},
        {"fixture_process_only": False},
    ],
)
def test_fixture_process_exception_does_not_allow_relabeling_historical_evidence(changes):
    with pytest.raises(DomainError) as failure:
        check_typed_retrieval_release(
            typed_execution_contract(),
            {**release_report(), **changes},
            mode="test",
            provenance="fixture",
            admitted_retrieval_context_contract=RETRIEVAL_CONTEXT_CONTRACT,
        )
    assert failure.value.code == "SEMANTIC_REPLAY_REQUIRED"


def test_engine_identity_binds_executor_scorer_and_current_admission_contract(monkeypatch):
    import careerforge.services.evolution_replay as replay

    before = engine_versions()
    assert before["typed_execution_contract"] == fingerprint(typed_execution_contract())
    assert before["admitted_retrieval_context_contract"] == RETRIEVAL_CONTEXT_CONTRACT
    monkeypatch.setattr(replay, "RETRIEVAL_CONTEXT_CONTRACT", "unsupported-next-version")
    assert engine_versions() != before


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_dataset,legacy_run", [(False, False), (True, True)])
async def test_typed_admission_and_reexecution_keep_contract_pairing_and_receipts(
    env, monkeypatch, legacy_dataset, legacy_run
):
    app, actor = env["app"], env["actor"]
    candidate, _, dataset, _ = await routing_setup(env)
    with app.db.transaction() as db:
        row = db.get(EvalDataset, dataset["dataset_id"])
        assert row.threshold_policy["typed_execution_contract"] == typed_execution_contract()
        if legacy_dataset:
            row.threshold_policy = {
                key: value
                for key, value in row.threshold_policy.items()
                if key != "typed_execution_contract"
            }
            row.frozen_hash = fingerprint({"cases": row.cases, "policy": row.threshold_policy})
    started = app.evolution.start_replay(
        actor,
        candidate,
        ReplayInput(
            dataset_id=dataset["dataset_id"], funding_id=env["funding"], idempotency_key="binding"
        ),
    )
    with app.db.transaction() as db:
        run = db.get(AgentRun, started["run_id"])
        assert run.input_manifest["typed_execution_contract"] == typed_execution_contract()
        if legacy_run:
            # Emulate an unmarked durable admission, without weakening the
            # independent engine/source-hash checks or changing either arm.
            frozen = {
                key: value
                for key, value in run.input_manifest.items()
                if key != "typed_execution_contract"
            }
            run.input_manifest, run.input_hash = frozen, fingerprint(frozen)
            manifest = db.get(PolicyReplayManifest, started["replay_id"])
            manifest.frozen_input, manifest.frozen_hash = frozen, run.input_hash
    seen = []
    original = Coordinator.decide

    async def decide(self, *args, **kwargs):
        seen.append(kwargs.get("retrieval_context_contract", "MISSING"))
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(Coordinator, "decide", decide)
    await app.drain()
    result = app.runtime.stage(started["run_id"], "result")
    assert result["decision"] == "passed", result
    assert seen == [None] * 8
    assert result["typed_execution_contract"] == typed_execution_contract()
    detail = app.evolution.replay_detail(actor, started["replay_id"])
    for case_id in {trace["case_id"] for trace in detail["trace"]}:
        pair = [trace for trace in detail["trace"] if trace["case_id"] == case_id]
        assert len(pair) == 2 and pair[0]["frozen_input_hash"] == pair[1]["frozen_input_hash"]
        assert all(
            trace["typed_execution_contract"] == typed_execution_contract() for trace in pair
        )
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
        receipts = list(db.scalars(select(ModelReceipt.id).where(ModelReceipt.run_id == run.id)))
    # A completed retry is receipt-idempotent, not another paid evaluation.
    await app.evolution.execute_replay(run, run.epoch, "unused-completed-worker")
    with app.db.read() as db:
        assert (
            list(db.scalars(select(ModelReceipt.id).where(ModelReceipt.run_id == run.id)))
            == receipts
        )
    assert seen == [None] * 8
    # A tampered scope/contract cannot hide behind rehashed trace storage.
    with app.db.transaction() as db:
        row = db.get(PolicyReplayManifest, started["replay_id"])
        row.trace = deepcopy(row.trace)
        row.trace[0]["typed_execution_contract"] = {"version": "r3"}
        row.trace_hash = fingerprint(row.trace)
        evaluation = db.get(EvolutionEvaluation, result["evaluation_id"])
        evaluation.report = {**evaluation.report, "replay_trace_manifest_hash": row.trace_hash}
    with pytest.raises(DomainError) as failure:
        app.evolution.promote(actor, candidate, result["evaluation_id"], 1)
    assert failure.value.code == "SEMANTIC_REPLAY_REQUIRED"


@pytest.mark.asyncio
async def test_legacy_typed_executor_and_new_r3_keep_distinct_evidence_semantics(env):
    app, actor = env["app"], env["actor"]
    source, publication = await multi_fixture(env)
    with app.db.read() as db:
        full = app.knowledge.publication_pack(db, actor, publication)
    case = {
        "task_type": "explain_implementation",
        "question": "Alpha",
        "topic_keys": [],
        "publication_ids": [publication],
        "frozen_units": [full],
        "expected": {"relevant_publication_ids": [publication], "relevance_complete": True},
    }
    legacy = await execute_typed_case(
        app.evolution,
        app.gateway,
        None,
        "legacy:",
        {"per_project_limit": 3, "vector_enabled": False, "rerank_enabled": False},
        "historical",
        case,
        "retrieval_recipe",
        actor,
    )
    current = await app.retrieval.retrieve(actor, spec(source))
    assert legacy["knowledge_units"] == [full]
    assert score_typed_case(case, legacy, None, "retrieval_recipe")["hard_gate_violations"] == 0
    assert (
        current["query_normalization"]["retrieval_context_contract"] == RETRIEVAL_CONTEXT_CONTRACT
    )
    unit = current["knowledge_units"][0]
    assert len(unit["facts"]) == 1 and unit["facts"][0] in full["facts"]
    assert unit["facts"][0]["conditions"] == ["Only this synthetic condition."]
    assert unit["limitations"] == full["limitations"]
    with app.db.read() as db:
        manifest = db.get(ObjectRow, current["retrieval_manifest_id"]).payload
    assert manifest["evidence_budget"]["limit"] == 12000
    assert manifest["evidence_budget"]["selected_bytes"] == byte_size(unit) <= 12000
    # The historical scorer is deliberately not relaxed to accept projections.
    assert score_typed_case(case, current, None, "retrieval_recipe")["hard_gate_violations"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["production", "development"])
async def test_existing_fixture_production_fence_still_blocks_admission(env, mode):
    app, actor = env["app"], env["actor"]
    _, _, _, value = await routing_setup(env)
    app.db.settings = replace(app.settings, mode=mode)
    with pytest.raises(DomainError) as failure:
        app.evolution.register_typed_replay_dataset(actor, value.model_copy(update={"name": mode}))
    assert failure.value.code == "FIXTURE_NOT_GOLD"


@pytest.mark.asyncio
async def test_unknown_typed_dataset_contract_rejects_before_run_or_budget_admission(env):
    app, actor = env["app"], env["actor"]
    candidate, _, dataset, _ = await routing_setup(env)
    with app.db.transaction() as db:
        row = db.get(EvalDataset, dataset["dataset_id"])
        row.threshold_policy = {
            **row.threshold_policy,
            "typed_execution_contract": {
                **typed_execution_contract(),
                "retrieval_context_contract": RETRIEVAL_CONTEXT_CONTRACT,
            },
        }
        row.frozen_hash = fingerprint({"cases": row.cases, "policy": row.threshold_policy})
        funding = db.get(FundingAgreement, env["funding"])
        before_budget = funding.calls_held, funding.calls_consumed
        before_runs = list(db.scalars(select(AgentRun.id)))
    with pytest.raises(DomainError) as failure:
        app.evolution.start_replay(
            actor,
            candidate,
            ReplayInput(
                dataset_id=dataset["dataset_id"],
                funding_id=env["funding"],
                idempotency_key="unknown-typed-contract",
            ),
        )
    assert failure.value.code == "TYPED_EXECUTION_CONTRACT"
    with app.db.read() as db:
        funding = db.get(FundingAgreement, env["funding"])
        assert (funding.calls_held, funding.calls_consumed) == before_budget
        assert list(db.scalars(select(AgentRun.id))) == before_runs


@pytest.mark.asyncio
async def test_historical_retrieval_gain_is_fixture_only_and_cannot_promote_in_other_modes(env):
    app, actor = env["app"], env["actor"]
    candidate, dataset, _ = await retrieval_setup(env)
    started, result = await execute(env, candidate, dataset, key="contract-release", cap=3)
    assert result["decision"] == "passed", result
    assert result["baseline_score"] == 0 and result["candidate_score"] == pytest.approx(7 / 9)
    assert result["typed_execution_contract"] == typed_execution_contract()
    assert result["retrieval_evaluation_scope"] == "historical_whole_publication_selection_only"
    assert result["current_retrieval_quality_validated"] is False
    assert result["fixture_process_only"] is True
    assert (
        "current_bounded_fact_retrieval_requires_compatible_execution_and_scoring"
        in result["external_acceptance_blockers"]
    )
    detail = app.evolution.replay_detail(actor, started["replay_id"])
    assert len(detail["trace"]) == 8
    assert all(
        "retrieval_context_contract" not in trace["typed_result"]["query_normalization"]
        for trace in detail["trace"]
    )
    for mode in ("production", "development"):
        app.db.settings = replace(app.settings, mode=mode)
        with pytest.raises(DomainError) as failure:
            app.evolution.promote(actor, candidate, result["evaluation_id"], 1)
        assert failure.value.code == "GOLD_NOT_READY"
    app.db.settings = app.settings
    with app.db.transaction() as db:
        evaluation = db.get(EvolutionEvaluation, result["evaluation_id"])
        evaluation.report = {**evaluation.report, "current_retrieval_quality_validated": True}
    with pytest.raises(DomainError) as failure:
        app.evolution.promote(actor, candidate, result["evaluation_id"], 1)
    assert failure.value.code == "SEMANTIC_REPLAY_REQUIRED"


@pytest.mark.asyncio
async def test_r3_marker_cannot_invoke_historical_typed_executor_or_spend_calls(env):
    before = len(env["model"].calls)
    with pytest.raises(DomainError) as failure:
        await execute_typed_case(
            env["app"].evolution,
            env["app"].gateway,
            ("not-admitted", 1, "worker"),
            "replay:0:candidate:",
            {},
            "policy",
            {},
            "retrieval_recipe",
            env["actor"],
            execution_contract={
                **typed_execution_contract(),
                "retrieval_context_contract": RETRIEVAL_CONTEXT_CONTRACT,
            },
        )
    assert failure.value.code == "TYPED_EXECUTION_CONTRACT"
    assert len(env["model"].calls) == before


@pytest.mark.parametrize("marked", [False, True])
def test_actual_r3_stage_cannot_be_relabeled_as_historical_whole_publication_execution(marked):
    trace = {
        "typed_result": {"query_normalization": {}},
        "error_code": None,
        **({"typed_execution_contract": typed_execution_contract()} if marked else {}),
    }
    check_typed_execution_trace(trace, "retrieval_recipe")
    trace["typed_result"]["query_normalization"] = {
        "retrieval_context_contract": RETRIEVAL_CONTEXT_CONTRACT
    }
    with pytest.raises(DomainError) as failure:
        check_typed_execution_trace(trace, "retrieval_recipe")
    assert failure.value.code == "TYPED_EXECUTION_CONTRACT"


@pytest.mark.parametrize("result", [None, [], {"query_normalization": []}])
def test_malformed_cached_typed_output_fails_closed(result):
    with pytest.raises(DomainError) as failure:
        check_typed_execution_trace({"typed_result": result}, "retrieval_recipe")
    assert failure.value.code == "TYPED_EXECUTION_CONTRACT"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_stage", [False, True])
async def test_unmarked_partial_replay_resumes_exact_stage_without_repeating_receipts(
    env, monkeypatch, bad_stage
):
    app, actor = env["app"], env["actor"]
    candidate, _, dataset, _ = await routing_setup(env)
    started = app.evolution.start_replay(
        actor,
        candidate,
        ReplayInput(
            dataset_id=dataset["dataset_id"],
            funding_id=env["funding"],
            idempotency_key="partial-contract",
        ),
    )
    with app.db.transaction() as db:
        run = db.get(AgentRun, started["run_id"])
        frozen = {
            key: value
            for key, value in run.input_manifest.items()
            if key != "typed_execution_contract"
        }
        run.input_manifest, run.input_hash = frozen, fingerprint(frozen)
        manifest = db.get(PolicyReplayManifest, started["replay_id"])
        manifest.frozen_input, manifest.frozen_hash = frozen, run.input_hash
    epoch = app.runtime.claim(started["run_id"], "partial-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
    original = app.runtime.save_stage

    def stop_after_stage(run_id, epoch, worker, key, trace):
        if key == "replay:0:baseline:result":
            trace = {
                key: value for key, value in trace.items() if key != "typed_execution_contract"
            }
            if bad_stage:
                trace["typed_execution_contract"] = {"version": "r3"}
            original(run_id, epoch, worker, key, trace)
            raise LeaseLost()
        return original(run_id, epoch, worker, key, trace)

    monkeypatch.setattr(app.runtime, "save_stage", stop_after_stage)
    with pytest.raises(LeaseLost):
        await app.evolution.execute_replay(run, epoch, "partial-worker")
    saved = app.runtime.stage(run.id, "replay:0:baseline:result")
    first_case = frozen["cases"][0]
    expected_old_input_hash = fingerprint(
        {
            "question": first_case["question"],
            "evidence": {
                "knowledge_units": first_case["frozen_units"],
                "selection_manifest_id": frozen["selection_manifest_id"],
                "retrieval_manifest_id": frozen["selection_manifest_id"],
                "uncovered_questions": [],
                "source_observations": [],
                "task_type": "explain_implementation",
                "total_project_groups": 0,
            },
            "corpus": [],
            "typed_snapshot": first_case["typed_snapshot"],
        }
    )
    assert saved["frozen_input_hash"] == expected_old_input_hash
    before_calls = len(env["model"].calls)
    monkeypatch.setattr(app.runtime, "save_stage", original)
    if bad_stage:
        with pytest.raises(DomainError) as failure:
            await app.evolution.execute_replay(run, epoch, "partial-worker")
        assert failure.value.code == "TYPED_EXECUTION_CONTRACT"
        assert len(env["model"].calls) == before_calls
        assert app.runtime.stage(run.id, "result") is None
    else:
        await app.evolution.execute_replay(run, epoch, "partial-worker")
        app.runtime.finish(run.id, epoch, "partial-worker", "succeeded")
        result = app.runtime.stage(run.id, "result")
        assert result["decision"] == "passed", result
        assert result["model_calls"] == 4
        assert app.runtime.stage(run.id, "replay:0:baseline:result") == saved
        with app.db.read() as db:
            receipts = db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id)).all()
            assert len(receipts) == 4
            assert sum(r.stage_key.startswith("replay:0:baseline:") for r in receipts) == 1
        assert app.evolution.promote(actor, candidate, result["evaluation_id"], 1)
