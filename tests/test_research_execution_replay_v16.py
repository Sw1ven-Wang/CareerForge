"""Paid-call orchestration is exercised with explicit fixtures, not real search quality."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_evolution import candidate

from careerforge.contracts import ModelOutput, PolicyPatch
from careerforge.contracts_learning_ext import (
    ReplayInput,
    ReplayThresholds,
    ResearchReplayCaseInput,
    ResearchReplayDatasetInput,
)
from careerforge.errors import DomainError
from careerforge.models import (
    EvalDataset,
    ExternalRequest,
    PolicyVersion,
    ResearchNode,
    SearchObservation,
)
from careerforge.services.policies import ensure_policies
from careerforge.util import fingerprint


async def capture_corpus(env):
    app, actor = env["app"], env["actor"]
    observed_runs = {}
    calls = []

    def collect_plan(request):
        family = "a" if "family-a" in request["context"]["question"] else "b"
        return ModelOutput(
            synthetic=True,
            structured={
                "queries": [
                    {
                        "query": f"{language} {family}",
                        "question": f"Research {language} {family}",
                        "topic_keys": ["agent.tool_calling"],
                        "channel": "repository",
                    }
                    for language in ["english", "bilingual"]
                ],
                "stop_reason": None,
            },
        )

    async def fixture_search(query, run_id, request_cap, channel="repository"):
        calls.append((query, run_id))
        language, family = query.split()
        identity = f"wanted-{family}" if language == "bilingual" else f"irrelevant-{family}"
        # These are explicitly synthetic audit rows, exercising the production
        # capture hook's actual ID/count linkage without issuing external HTTP.
        with app.db.transaction() as db:
            db.add(
                ExternalRequest(
                    run_id=run_id,
                    operation="fixture_search",
                    request_hash=fingerprint(query),
                    status="completed",
                    response_metadata={"synthetic": True},
                )
            )
        return {
            "candidates": [
                {
                    "repository_id": identity,
                    "name": "fixtures/" + identity,
                    "url": "https://github.com/fixtures/" + identity,
                }
            ],
            "incomplete_results": False,
            "synthetic": True,
        }

    env["model"].overrides["planner"] = collect_plan
    env["repos"].search = fixture_search
    for family in ["a", "b"]:
        run = app.runtime.create(
            actor,
            env["ns"],
            env["funding"],
            "research",
            {
                "question": f"family-{family}",
                "model_profile": env["model"].profile_version,
                "github_request_cap": 4,
            },
            1,
            "capture-" + family,
        )
        epoch = app.runtime.claim(run, "corpus-worker")
        await app.research.run(
            actor,
            run,
            epoch,
            "corpus-worker",
            f"family-{family}",
            ["agent.tool_calling"],
            3,
            1,
            1,
            4,
        )
        app.runtime.finish(run, epoch, "corpus-worker", "succeeded")
        observed_runs[family] = run
    return observed_runs, calls


async def setup_research(env):
    app, actor = env["app"], env["actor"]
    _, _, conv, _, feedback = await candidate(env)
    with app.db.transaction() as db:
        baseline = ensure_policies(db, actor, env["ns"])["research_heuristic"]["policy_id"]
        # Champion English, Candidate bilingual. Initial production default itself is
        # bilingual; fixture deliberately models a frozen earlier English champion.
        base = db.get(PolicyVersion, baseline)
        base.content = {**base.content, "query_language": "english"}
        base.content_hash = fingerprint(base.content)
    c = app.evolution.create_candidate(
        actor,
        env["ns"],
        baseline,
        PolicyPatch(
            target="research_heuristic",
            content={"query_language": "bilingual"},
            experience_ids=[feedback["experience_id"]],
            training_group_ids=["conversation:" + conv],
        ),
    )
    runs, calls = await capture_corpus(env)
    cases = [
        ResearchReplayCaseInput(
            case_id=f"{family}-{variant}",
            group_id="group-" + family,
            user_question_group="heldout-" + family,
            time_partition="2020-01",
            question=f"请研究family-{family}变体{variant}",
            topic_keys=["agent.tool_calling"],
            observation_run_ids=[runs[family]],
            relevant_repository_ids=["wanted-" + family],
        )
        for family in ["a", "b"]
        for variant in range(2)
    ]
    value = ResearchReplayDatasetInput(
        namespace_id=env["ns"],
        name="captured-search",
        version="1",
        split="holdout",
        provenance="fixture",
        cases=cases,
        thresholds=ReplayThresholds(min_cases=4, min_groups=2, max_latency_ratio=5),
    )
    dataset = app.evolution.register_research_replay_dataset(actor, value)

    def replay_plan(request):
        family = "a" if "family-a" in request["context"]["question"] else "b"
        language = request["context"]["research_heuristic"]["query_language"]
        return ModelOutput(
            synthetic=True,
            structured={
                "queries": [
                    {
                        "query": f"{language} {family}",
                        "question": "固定查询",
                        "topic_keys": ["agent.tool_calling"],
                        "channel": "repository",
                    }
                ],
                "stop_reason": None,
            },
        )

    env["model"].overrides["planner"] = replay_plan
    return c, dataset, runs, calls, value


def isolate_replay_clock(monkeypatch, durations):
    """Control only replay arm measurement, never gateway/lease/global clocks."""
    from careerforge.services import evolution_replay

    values = []
    elapsed = 0.0
    for duration in durations:
        values.extend([elapsed, elapsed + duration])
        elapsed += duration + 0.01
    ticks = iter(values)
    monkeypatch.setattr(evolution_replay, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))


async def execute(env, c, dataset, key="research-replay"):
    app = env["app"]
    started = app.evolution.start_replay(
        env["actor"],
        c,
        ReplayInput(
            dataset_id=dataset["dataset_id"],
            funding_id=env["funding"],
            idempotency_key=key,
            per_arm_call_cap=1,
            per_arm_request_cap=1,
            max_research_nodes=3,
            max_research_depth=1,
        ),
    )
    await app.drain()
    result = app.runtime.stage(started["run_id"], "result")
    assert result is not None
    return started, result


@pytest.mark.asyncio
async def test_actual_query_generation_replays_audited_corpus_without_fresh_network(
    env, monkeypatch
):
    app, actor = env["app"], env["actor"]
    c, dataset, _, calls, _ = await setup_research(env)
    before = list(calls)
    isolate_replay_clock(monkeypatch, [0.02] * 8)
    started, result = await execute(env, c, dataset)
    assert result["decision"] == "passed", result
    assert result["target"] == "research_heuristic" and result["model_calls"] == 8
    assert (
        not result["live_search_effectiveness_validated"] and result["new_external_requests"] == 0
    )
    assert calls == before
    trace = app.evolution.replay_detail(actor, started["replay_id"])["trace"]
    assert all(t["research_result"]["requests_used"] == 1 for t in trace)
    assert all(t["research_result"]["request_cap"] == 1 for t in trace)
    assert all(
        t["receipt_ids"] and t["research_result"]["trace"][0]["observation_id"] for t in trace
    )
    promoted = app.evolution.promote(actor, c, result["evaluation_id"], 1)
    assert promoted["active_from"] == "next_task_only"


@pytest.mark.asyncio
async def test_unseen_query_is_inconclusive_not_invented_empty_or_live_search(env):
    app = env["app"]
    c, dataset, _, calls, _ = await setup_research(env)
    original = env["model"].overrides["planner"]

    def unseen(request):
        reply = original(request)
        if request["context"]["research_heuristic"]["query_language"] == "bilingual":
            reply.structured["queries"][0]["query"] = "unobserved newquery"
        return reply

    env["model"].overrides["planner"] = unseen
    before = list(calls)
    started, result = await execute(env, c, dataset)
    assert result["decision"] == "inconclusive" and "unobserved_query" in result["reason"]
    assert calls == before
    with pytest.raises(DomainError):
        app.evolution.promote(env["actor"], c, result["evaluation_id"], 1)
    trace = app.evolution.replay_detail(env["actor"], started["replay_id"])["trace"]
    assert any(t["research_result"]["unobserved_queries"] for t in trace)


@pytest.mark.asyncio
async def test_research_queries_cannot_expand_privacy_scope_even_in_replay(env):
    c, dataset, _, calls, _ = await setup_research(env)

    def private(request):
        return ModelOutput(
            synthetic=True,
            structured={
                "queries": [
                    {
                        "query": "is:private token",
                        "question": "搜索隐私",
                        "topic_keys": ["agent.tool_calling"],
                    }
                ],
                "stop_reason": None,
            },
        )

    env["model"].overrides["planner"] = private
    before = list(calls)
    _, result = await execute(env, c, dataset)
    assert result["decision"] == "failed" and not result["hard_gate_passed"]
    assert calls == before


@pytest.mark.asyncio
async def test_research_dataset_only_captures_authorized_persisted_observations(env):
    app = env["app"]
    _, _, _, _, value = await setup_research(env)
    altered = value.model_copy(deep=True, update={"name": "invented-corpus"})
    altered.cases[0].observation_run_ids = ["invented-run"]
    with pytest.raises(DomainError) as exc:
        app.evolution.register_research_replay_dataset(env["actor"], altered)
    assert exc.value.code == "RESEARCH_CORPUS_AUTHORITY"
    invalid_gold = value.model_copy(deep=True, update={"name": "invented-label"})
    invalid_gold.cases[0].relevant_repository_ids = ["not-observed"]
    with pytest.raises(DomainError) as exc:
        app.evolution.register_research_replay_dataset(env["actor"], invalid_gold)
    assert exc.value.code == "RESEARCH_GOLD_SCOPE"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["observation", "receipt"])
async def test_changed_research_corpus_or_cost_receipt_blocks_exact_promotion(
    env, target, monkeypatch
):
    app = env["app"]
    c, dataset, runs, _, _ = await setup_research(env)
    # Receipt tampering must be checked after an otherwise promotable replay.
    isolate_replay_clock(monkeypatch, [0.02] * 8)
    _, result = await execute(env, c, dataset)
    assert result["decision"] == "passed"
    with app.db.transaction() as db:
        if target == "observation":
            row = db.scalar(
                select(SearchObservation).join(ResearchNode).where(ResearchNode.run_id == runs["a"])
            )
            row.payload = {**row.payload, "candidates": []}
        else:
            row = db.scalar(select(ExternalRequest).where(ExternalRequest.run_id == runs["a"]))
            row.response_metadata = {"tampered": True}
    with pytest.raises(DomainError):
        app.evolution.promote(env["actor"], c, result["evaluation_id"], 1)


@pytest.mark.asyncio
async def test_missing_historical_cost_cannot_establish_same_budget_gain(env, monkeypatch):
    app = env["app"]
    c, dataset, _, calls, _ = await setup_research(env)
    before_calls = list(calls)
    with app.db.transaction() as db:
        row = db.get(EvalDataset, dataset["dataset_id"])
        cases = deepcopy(row.cases)
        for case in cases:
            for entry in case["corpus"]:
                entry["request_cost"] = None
        row.cases = cases
        row.frozen_hash = fingerprint({"cases": cases, "policy": row.threshold_policy})
    # This case isolates unknown cost. Real wall-clock jitter must not turn it
    # into the separately tested, legitimate over-threshold latency rejection.
    isolate_replay_clock(monkeypatch, [0.02] * 8)
    started, result = await execute(env, c, dataset)
    assert result["decision"] == "inconclusive" and "unverified_cost" in result["reason"]
    assert result["latency_ratio"] == pytest.approx(1.0)
    assert result["threshold_policy"]["max_latency_ratio"] == 5
    assert result["new_external_requests"] == 0 and result["published"] is False
    assert calls == before_calls
    trace = app.evolution.replay_detail(env["actor"], started["replay_id"])["trace"]
    assert all(t["metrics"]["incomplete_counterfactual"] for t in trace)
    assert all(t["research_result"]["request_cost_unverified"] for t in trace)
    with pytest.raises(DomainError):
        app.evolution.promote(env["actor"], c, result["evaluation_id"], 1)


@pytest.mark.asyncio
async def test_slow_candidate_still_fails_frozen_latency_gate_and_cannot_promote(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    c, dataset, _, calls, _ = await setup_research(env)
    before_calls = list(calls)
    with app.db.transaction() as db:
        before_policy = ensure_policies(db, actor, env["ns"])["research_heuristic"]["policy_id"]
    # Replay deliberately counterbalances arm order for successive cases.
    # Baseline=20ms, candidate=200ms makes the frozen 5x limit fail at 10x.
    isolate_replay_clock(monkeypatch, [0.02, 0.2, 0.2, 0.02, 0.02, 0.2, 0.2, 0.02])
    started, result = await execute(env, c, dataset)
    assert result["threshold_policy"]["max_latency_ratio"] == 5
    assert result["latency_ratio"] == pytest.approx(10.0)
    assert result["decision"] == "failed"
    assert result["reason"] == "frozen_cost_or_latency_regression"
    assert result["new_external_requests"] == 0 and result["published"] is False
    assert calls == before_calls
    trace = app.evolution.replay_detail(actor, started["replay_id"])["trace"]
    assert all(
        t["latency_ms"] == pytest.approx(20 if t["arm"] == "baseline" else 200) for t in trace
    )
    with pytest.raises(DomainError):
        app.evolution.promote(actor, c, result["evaluation_id"], 1)
    with app.db.transaction() as db:
        assert (
            ensure_policies(db, actor, env["ns"])["research_heuristic"]["policy_id"]
            == before_policy
        )
