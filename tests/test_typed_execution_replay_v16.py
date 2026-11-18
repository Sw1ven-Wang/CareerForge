"""Explicit synthetic models exercise the actual production router/retriever.

These labels and corpus are fixture process tests, never real relevance evidence.
"""

from dataclasses import replace

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_evolution import candidate

from careerforge.contracts import ModelOutput, PolicyPatch
from careerforge.contracts_learning_ext import (
    DatasetAttestationInput,
    ReplayInput,
    ReplayThresholds,
    TypedReplayCaseInput,
    TypedReplayDatasetInput,
    TypedReplayGold,
)
from careerforge.errors import DomainError
from careerforge.models import (
    EvalDataset,
    EvolutionCandidate,
    KnowledgeDocument,
    ModelReceipt,
    PolicyVersion,
)
from careerforge.services.policies import ensure_policies
from careerforge.services.security import create_principal, set_grant
from careerforge.util import fingerprint


async def typed_candidate(env, target, content):
    app, actor = env["app"], env["actor"]
    _, _, conv, _, feedback = await candidate(env)
    with app.db.transaction() as db:
        baseline = ensure_policies(db, actor, env["ns"])[target]["policy_id"]
    identifier = app.evolution.create_candidate(
        actor,
        env["ns"],
        baseline,
        PolicyPatch(
            target=target,
            content=content,
            experience_ids=[feedback["experience_id"]],
            training_group_ids=["conversation:" + conv],
        ),
    )
    return identifier, baseline


async def routing_setup(env):
    c, base = await typed_candidate(
        env,
        "routing_policy",
        {"keyword_rules": [{"keyword": "bottleneck", "task_type": "explain_project_architecture"}]},
    )
    cases = [
        TypedReplayCaseInput(
            case_id=f"{group}-{i}",
            group_id=group,
            user_question_group="heldout-" + group,
            time_partition="2020-01",
            question=f"bottleneck {group} {i}",
            expected=TypedReplayGold(
                task_type="explain_project_architecture", action="invoke_tutor"
            ),
        )
        for group in ["a", "b"]
        for i in range(2)
    ]
    value = TypedReplayDatasetInput(
        namespace_id=env["ns"],
        name="routing",
        version="1",
        target="routing_policy",
        split="holdout",
        provenance="fixture",
        cases=cases,
        thresholds=ReplayThresholds(min_cases=4, min_groups=2, max_latency_ratio=5),
    )
    return c, base, env["app"].evolution.register_typed_replay_dataset(env["actor"], value), value


async def execute(env, c, ds, key="typed", cap=2):
    app = env["app"]
    started = app.evolution.start_replay(
        env["actor"],
        c,
        ReplayInput(
            dataset_id=ds["dataset_id"],
            funding_id=env["funding"],
            per_arm_call_cap=cap,
            idempotency_key=key,
        ),
    )
    await app.drain()
    result = app.runtime.stage(started["run_id"], "result")
    assert result is not None, app.runtime.detail(env["actor"], started["run_id"])
    return started, result


@pytest.mark.asyncio
async def test_actual_coordinator_execution_uses_same_frozen_catalog_and_exact_release_gate(env):
    app, actor = env["app"], env["actor"]
    c, _, ds, _ = await routing_setup(env)
    started, result = await execute(env, c, ds, cap=1)
    assert result["decision"] == "passed", result
    assert result["target"] == "routing_policy" and result["model_calls"] == 4
    detail = app.evolution.replay_detail(actor, started["replay_id"])
    assert len(detail["trace"]) == 8
    assert all(t["typed_result"]["executed_action"] is False for t in detail["trace"])
    assert all(
        "expected" not in r["context"] for r in env["model"].calls if r["role"] == "coordinator"
    )
    with app.db.read() as db:
        receipts = db.scalars(
            select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"])
        ).all()
        assert len(receipts) == 4 and all(r.stage_key.startswith("replay:") for r in receipts)
    assert (
        app.evolution.promote(actor, c, result["evaluation_id"], 1)["active_from"]
        == "next_task_only"
    )


@pytest.mark.asyncio
async def test_routing_catalog_change_after_capture_blocks_replay(env):
    c, _, ds, _ = await routing_setup(env)
    await publish_fixture(env, key="new-source-after-capture")
    with pytest.raises(DomainError) as exc:
        env["app"].evolution.start_replay(
            env["actor"],
            c,
            ReplayInput(
                dataset_id=ds["dataset_id"], funding_id=env["funding"], idempotency_key="drift"
            ),
        )
    assert exc.value.code == "REPLAY_INDEX_CHANGED"


async def retrieval_setup(env):
    app, actor = env["app"], env["actor"]
    c, base = await typed_candidate(
        env, "retrieval_recipe", {"rerank_enabled": True, "vector_enabled": True}
    )
    with app.db.transaction() as db:
        baseline = db.get(PolicyVersion, base)
        baseline.content = {**baseline.content, "rerank_enabled": False}
        baseline.content_hash = fingerprint(baseline.content)
    env["model"].supports_embeddings = True
    env["model"].supports_reranking = True
    env["model"].overrides["embedding"] = lambda req: ModelOutput(
        synthetic=True,
        structured={
            "model": "fixture-constant-not-semantic",
            "vectors": [[1.0] + [0.0] * 7 for _ in req["context"]["texts"]],
        },
    )
    env["model"].overrides["reranker"] = lambda req: ModelOutput(
        synthetic=True,
        structured={
            "scores": [
                {"index": i, "score": float(i)} for i in range(len(req["context"]["documents"]))
            ]
        },
    )
    cases = []
    all_ids = []
    for family in ["a", "b"]:
        pool = []
        for i in range(4):
            _, pub, _ = await publish_fixture(
                env, key=f"typed-{family}-{i}", repository=f"fixtures/typed-{family}-{i}"
            )
            pool.append(pub)
        all_ids.extend(pool)
        for i in range(2):
            cases.append(
                TypedReplayCaseInput(
                    case_id=f"{family}-{i}",
                    group_id=family,
                    user_question_group="heldout-" + family,
                    time_partition="2020-01",
                    question=f"Tool Calling fixture {family} {i}",
                    task_type="explain_project_architecture",
                    publication_ids=pool,
                    topic_keys=["agent.tool_calling"],
                    expected=TypedReplayGold(
                        relevant_publication_ids=[max(pool)], relevance_complete=True
                    ),
                )
            )
    # Actual index-vector service and durable receipts, with only explicit fixture vectors.
    index_run = app.runtime.create(
        actor, env["ns"], env["funding"], "retrieval_index", {}, 1, "fixture-index"
    )
    epoch = app.runtime.claim(index_run, "index-worker")
    await app.retrieval.index_vectors(
        actor, all_ids, (index_run, epoch, "index-worker"), batch_size=16
    )
    app.runtime.finish(index_run, epoch, "index-worker", "succeeded")
    value = TypedReplayDatasetInput(
        namespace_id=env["ns"],
        name="retrieval",
        version="1",
        target="retrieval_recipe",
        split="holdout",
        provenance="fixture",
        cases=cases,
        thresholds=ReplayThresholds(
            min_cases=4, min_groups=2, max_latency_ratio=5, max_call_ratio=2
        ),
    )
    return c, app.evolution.register_typed_replay_dataset(actor, value), value


@pytest.mark.asyncio
async def test_actual_retrieval_vector_and_reranker_execute_per_arm_not_unfunded_surrogate(env):
    app, actor = env["app"], env["actor"]
    c, ds, _ = await retrieval_setup(env)
    started, result = await execute(env, c, ds, cap=3)
    assert result["decision"] == "passed", result
    assert result["model_calls"] == 12 and result["call_ratio"] == 2
    detail = app.evolution.replay_detail(actor, started["replay_id"])
    assert all(t["typed_result"]["knowledge_units"] for t in detail["trace"])
    assert all(
        t["metrics"]["publication_recall"] == (1.0 if t["arm"] == "candidate" else 0.0)
        for t in detail["trace"]
    )
    with app.db.read() as db:
        receipts = db.scalars(
            select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"])
        ).all()
        assert {r.role for r in receipts} == {"embedding", "reranker"}
        assert all(r.stage_key.startswith("replay:") for r in receipts)
    assert app.evolution.promote(actor, c, result["evaluation_id"], 1)


@pytest.mark.asyncio
async def test_typed_replay_index_tamper_and_invalid_gold_are_rejected(env):
    app, actor = env["app"], env["actor"]
    c, ds, value = await retrieval_setup(env)
    invalid = value.model_copy(deep=True, update={"name": "bad-gold"})
    invalid.cases[0].expected.relevant_publication_ids = ["outside-pool"]
    with pytest.raises(DomainError):
        app.evolution.register_typed_replay_dataset(actor, invalid)
    with app.db.transaction() as db:
        row = db.get(KnowledgeDocument, value.cases[0].publication_ids[0])
        row.search_text += " altered-index"
    with pytest.raises(DomainError) as exc:
        app.evolution.start_replay(
            actor,
            c,
            ReplayInput(
                dataset_id=ds["dataset_id"], funding_id=env["funding"], idempotency_key="drift"
            ),
        )
    assert exc.value.code == "REPLAY_INDEX_CHANGED"


@pytest.mark.asyncio
async def test_human_gold_cannot_use_legacy_typed_production_promotion_shortcut(env):
    app, actor = env["app"], env["actor"]
    c, _, _, _, _ = await candidate(env)
    with app.db.transaction() as db:
        annotators = [create_principal(db, "gold-" + str(i))[0].id for i in range(2)]
        for identifier in annotators:
            set_grant(db, actor, env["ns"], identifier, "writer", True)
    cases = [
        {
            "case_id": str(i),
            "group_id": "heldout-" + str(i % 2),
            "input": {"question": "教学布局"},
            "expected": {"preferred_order": "code_first"},
        }
        for i in range(4)
    ]
    ds = app.evolution.register_dataset(
        actor, env["ns"], "legacy-human-gold", "1", cases, "human_gold", annotators
    )
    with app.db.read() as db:
        frozen = db.get(EvalDataset, ds).frozen_hash
    for identifier in annotators:
        app.evolution.attest_dataset(
            identifier,
            ds,
            DatasetAttestationInput(
                frozen_hash=frozen, decision="approve", notes="仅流程测试，不宣称真实Gold"
            ),
        )
    evaluated = await app.evolution.evaluate(actor, c, ds)
    assert evaluated["decision"] == "passed"
    # Both approved Gold and a previously passed old report must not bypass the new
    # actual-runtime manifest gate when used in production mode.
    app.settings = replace(app.settings, mode="production")
    app.db.settings = app.settings
    with pytest.raises(DomainError) as exc:
        app.evolution.promote(actor, c, evaluated["evaluation_id"], 1)
    assert exc.value.code == "SEMANTIC_REPLAY_REQUIRED"
    retry = await app.evolution.evaluate(actor, c, ds)
    assert retry["decision"] == "inconclusive" and "actual_execution_replay" in retry["reason"]
    with app.db.read() as db:
        assert db.get(EvolutionCandidate, c).status != "promoted"


@pytest.mark.asyncio
async def test_typed_human_gold_holdout_requires_real_prior_calibration(env):
    app, actor = env["app"], env["actor"]
    _, _, _, value = await routing_setup(env)
    with app.db.transaction() as db:
        annotators = [create_principal(db, "gold-" + str(i))[0].id for i in range(2)]
    invalid = value.model_copy(
        deep=True,
        update={"name": "uncalibrated", "provenance": "human_gold", "annotators": annotators},
    )
    invalid.thresholds = invalid.thresholds.model_copy(update={"min_cases": 20, "min_groups": 10})
    with pytest.raises(DomainError) as exc:
        app.evolution.register_typed_replay_dataset(actor, invalid)
    assert exc.value.code == "BASELINE_CALIBRATION_REQUIRED"
