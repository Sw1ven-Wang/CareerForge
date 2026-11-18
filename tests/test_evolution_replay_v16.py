"""Real service/gateway/guard execution using explicitly synthetic model responses."""

import asyncio

import httpx
import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_evolution import candidate, dataset

from careerforge.api import create_app
from careerforge.contracts import ModelOutput, PolicyPatch, TurnInput
from careerforge.contracts_learning_ext import (
    DatasetAttestationInput,
    ReplayCase,
    ReplayDatasetInput,
    ReplayGold,
    ReplayInput,
    ReplayThresholds,
)
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    EvalDataset,
    EvolutionCandidate,
    EvolutionEvaluation,
    ModelReceipt,
    PolicyHead,
    PolicyVersion,
    Turn,
)
from careerforge.models_learning_ext import PolicyReplayManifest
from careerforge.services.evolution_replay import independence_groups
from careerforge.services.policies import render_teaching_skill, validate_policy
from careerforge.services.security import create_principal, set_grant


async def semantic_candidate(env):
    app, actor = env["app"], env["actor"]
    _, base, conv, _, feedback = await candidate(env)
    patch = PolicyPatch(
        target="teaching_skill",
        content={
            "explanation_order": "code_first",
            "check_understanding": True,
            "teaching_steps": [
                {"kind": "trace_path", "instruction": "先给出已审核事实，再请学习者追踪路径。"}
            ],
            "explanation_templates": [
                {
                    "purpose": "boundary",
                    "template": "对照原文 {fact_text}，单独讨论条件 {conditions}。",
                }
            ],
            "exercise_prompts": [{"kind": "explain_conditions", "instruction": "请解释本题条件。"}],
        },
        experience_ids=[feedback["experience_id"]],
        training_group_ids=["conversation:" + conv],
    )
    return app.evolution.create_candidate(actor, env["ns"], base, patch), base, feedback


async def replay_data(
    env,
    split="holdout",
    name="actual-replay",
    provenance="fixture",
    annotators=None,
    thresholds=None,
):
    app, actor = env["app"], env["actor"]
    cases = []
    for family in ["fixtures/holdout-a", "fixtures/holdout-b"]:
        _, pub, _ = await publish_fixture(env, key=family + name, repository=family)
        with app.db.read() as db:
            pack = app.knowledge.publication_pack(db, actor, pub)
        for variant in range(2):
            cases.append(
                ReplayCase(
                    case_id=family + ":" + str(variant),
                    group_id=family,
                    repository_family=family,
                    user_question_group="heldout:" + family,
                    time_partition="2020-01" if split == "holdout" else "2019-01",
                    question=f"针对 {family} 的样例 {variant}，解释该固定返回路径。",
                    publication_ids=[pub],
                    expected=ReplayGold(
                        required_fact_ids=[pack["facts"][0]["fact_atom_id"]],
                        required_action_keys=["question", "practice"],
                        preferred_order="code_first",
                    ),
                )
            )
    value = ReplayDatasetInput(
        namespace_id=env["ns"],
        name=name,
        version="1",
        split=split,
        provenance=provenance,
        cases=cases,
        annotators=annotators or [],
        thresholds=thresholds or ReplayThresholds(min_cases=4, min_groups=2, max_latency_ratio=5),
    )
    return app.evolution.register_replay_dataset(actor, value), value


async def run_replay(env, c, ds, key="actual-execution", cap=2):
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
    assert result is not None
    return started, result


@pytest.mark.asyncio
async def test_semantic_skill_cannot_pass_layout_only_evaluator(env):
    c, _, _ = await semantic_candidate(env)
    ds = dataset(env)
    result = await env["app"].evolution.evaluate(env["actor"], c, ds)
    assert result["decision"] == "inconclusive"
    assert "execution_replay" in result["reason"]
    with pytest.raises(DomainError):
        env["app"].evolution.promote(env["actor"], c, result["evaluation_id"], 1)


@pytest.mark.asyncio
async def test_real_paired_tutor_execution_freezes_inputs_receipts_and_promotes_only_exact_eval(
    env,
):
    app, actor = env["app"], env["actor"]
    c, base, _ = await semantic_candidate(env)
    ds, _ = await replay_data(env)
    started, result = await run_replay(env, c, ds)
    assert result["decision"] == "passed", result
    # New 1.6.1 runs reserve an independent relevance/semantic check even for
    # exact factual text. The frozen total cap is unchanged, never expanded.
    assert result["model_calls"] == 16 and result["call_cap"] == 16
    with app.db.read() as db:
        replay_receipts = db.scalars(
            select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"])
        ).all()
        assert sum(r.role == "tutor" for r in replay_receipts) == 8
        assert sum(r.role == "answer_checker" for r in replay_receipts) == 8
    assert result["same_inputs"] and result["hard_gate_passed"]
    assert not result["human_effectiveness_validated"]
    replay = app.evolution.replay_detail(actor, started["replay_id"])
    assert len(replay["trace"]) == 8
    for i in range(4):
        arms = [t for t in replay["trace"] if t["case_id"] == replay["trace"][i * 2]["case_id"]]
        assert len(arms) == 2 and arms[0]["frozen_input_hash"] == arms[1]["frozen_input_hash"]
        assert all(t["receipt_ids"] and t["blocks"] for t in arms)
    requests = [r for r in env["model"].calls if r["role"] == "tutor"]
    assert len(requests) == 8
    assert all("expected" not in r["context"] for r in requests)
    assert any("解释模板" in " ".join(r["context"]["teaching_instructions"]) for r in requests)
    # Explicit user promotion; this does not alter running/frozen turns.
    conv = app.conversations.create(actor)
    frozen = app.conversations.submit(
        actor,
        conv,
        TurnInput(question="继续", expected_revision=1, idempotency_key="before-promotion"),
    )
    promoted = app.evolution.promote(actor, c, result["evaluation_id"], 1)
    with app.db.read() as db:
        assert (
            db.get(Turn, frozen["turn_id"]).policy_versions["teaching_skill"]["policy_id"] == base
        )
        assert db.get(PolicyVersion, promoted["policy_id"]).candidate_id == c


@pytest.mark.asyncio
async def test_semantic_template_output_still_crosses_independent_guard(env):
    app, actor = env["app"], env["actor"]
    c, base, _ = await semantic_candidate(env)
    ds, _ = await replay_data(env)

    def malicious(request):
        unit = request["context"]["evidence_pack"]["knowledge_units"][0]
        fact = unit["facts"][0]
        bad = bool(request["context"]["teaching_skill"].get("teaching_steps"))
        return ModelOutput(
            synthetic=True,
            structured={
                "blocks": [
                    {
                        "kind": "repository_fact",
                        "text": "此函数永远不会失败并保证线程安全。" if bad else fact["text"],
                        "fact_atom_ids": [fact["fact_atom_id"]],
                        "publication_ids": [unit["publication_id"]],
                        "conditions": fact["conditions"],
                        "transformation": "paraphrase" if bad else "exact",
                    }
                ]
            },
        )

    env["model"].overrides["tutor"] = malicious
    started, result = await run_replay(env, c, ds)
    assert result["decision"] == "failed" and not result["hard_gate_passed"]
    details = app.evolution.replay_detail(actor, started["replay_id"])
    candidate_blocks = [b for t in details["trace"] if t["arm"] == "candidate" for b in t["blocks"]]
    assert any(b.get("blocked_reason") == "ANSWER_HIGH_RISK" for b in candidate_blocks)
    assert not any("线程安全" in b["text"] for b in candidate_blocks)
    with pytest.raises(DomainError):
        app.evolution.promote(actor, c, result["evaluation_id"], 1)
    with app.db.read() as db:
        assert (
            db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill")).policy_id
            == base
        )


@pytest.mark.asyncio
async def test_same_budget_per_arm_cannot_borrow_and_is_inconclusive(env):
    app = env["app"]
    c, _, _ = await semantic_candidate(env)
    ds, _ = await replay_data(env)

    def paraphrase(request):
        unit = request["context"]["evidence_pack"]["knowledge_units"][0]
        fact = unit["facts"][0]
        return ModelOutput(
            synthetic=True,
            structured={
                "blocks": [
                    {
                        "kind": "repository_fact",
                        "text": "在这个片段中返回传入值。",
                        "fact_atom_ids": [fact["fact_atom_id"]],
                        "publication_ids": [unit["publication_id"]],
                        "conditions": fact["conditions"],
                        "transformation": "paraphrase",
                    }
                ]
            },
        )

    env["model"].overrides["tutor"] = paraphrase
    started, result = await run_replay(env, c, ds, cap=1)
    assert result["decision"] == "inconclusive"
    replay = app.evolution.replay_detail(env["actor"], started["replay_id"])
    assert all(
        t["model_calls"] == 0
        and t["metrics"]["incomplete_execution"]
        and any(block.get("gap_category") == "budget" for block in t["blocks"])
        for t in replay["trace"]
    )
    with app.db.read() as db:
        assert not db.scalars(
            select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"])
        ).all()


@pytest.mark.asyncio
async def test_replay_is_idempotent_and_cancelled_paid_results_do_not_write_evaluation(env):
    app, actor = env["app"], env["actor"]
    c, _, _ = await semantic_candidate(env)
    ds, _ = await replay_data(env)
    value = ReplayInput(
        dataset_id=ds["dataset_id"], funding_id=env["funding"], idempotency_key="cancel"
    )
    started = app.evolution.start_replay(actor, c, value)
    assert app.evolution.start_replay(actor, c, value)["run_id"] == started["run_id"]
    called, release = asyncio.Event(), asyncio.Event()
    original = env["model"].complete

    async def delayed(request):
        if request["role"] == "tutor":
            called.set()
            await release.wait()
        return await original(request)

    env["model"].complete = delayed
    task = asyncio.create_task(app.execute(started["run_id"]))
    await asyncio.wait_for(called.wait(), 5)
    with app.db.read() as db:
        revision = db.get(AgentRun, started["run_id"]).revision
    app.runtime.cancel(actor, started["run_id"], revision)
    release.set()
    await task
    with app.db.read() as db:
        assert not db.scalars(
            select(EvolutionEvaluation).where(EvolutionEvaluation.candidate_id == c)
        ).all()
        assert db.get(AgentRun, started["run_id"]).status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["trace", "template", "dataset", "authorization", "source"])
async def test_release_rechecks_exact_manifest_trace_authority_and_live_dependencies(env, tamper):
    from careerforge.models import Principal
    from careerforge.services.dependencies import invalidate

    app, actor = env["app"], env["actor"]
    c, _, _ = await semantic_candidate(env)
    ds, value = await replay_data(env)
    started, result = await run_replay(env, c, ds)
    assert result["decision"] == "passed"
    with app.db.transaction() as db:
        if tamper == "trace":
            row = db.get(PolicyReplayManifest, started["replay_id"])
            row.trace = []
        elif tamper == "template":
            row = db.get(EvolutionCandidate, c)
            row.content = {**row.content, "check_understanding": False}
        elif tamper == "dataset":
            db.get(EvalDataset, ds["dataset_id"]).split = "development"
        elif tamper == "authorization":
            db.get(Principal, actor).auth_revision += 1
        else:
            invalidate(db, actor, value.cases[0].publication_ids[0], "revoked", "withdrawn")
    with pytest.raises(DomainError):
        app.evolution.promote(actor, c, result["evaluation_id"], 1)


@pytest.mark.asyncio
async def test_development_replay_is_not_holdout_and_relabelled_repositories_cannot_fake_independence(
    env,
):
    app = env["app"]
    c, _, _ = await semantic_candidate(env)
    ds, _ = await replay_data(env, split="development")
    _, result = await run_replay(env, c, ds)
    assert result["decision"] == "inconclusive"
    assert "development" in result["reason"]
    heldout, _ = await replay_data(env, name="relabeled", split="holdout")
    with pytest.raises(DomainError) as exc:
        app.evolution.start_replay(
            env["actor"],
            c,
            ReplayInput(
                dataset_id=heldout["dataset_id"], funding_id=env["funding"], idempotency_key="leaky"
            ),
        )
    assert exc.value.code == "HOLDOUT_LEAKAGE"


@pytest.mark.asyncio
async def test_human_gold_requires_actual_individual_attestations(env):
    app, actor = env["app"], env["actor"]
    c, _, _ = await semantic_candidate(env)
    with app.db.transaction() as db:
        annotators = [create_principal(db, f"annotator-{i}")[0].id for i in range(2)]
        for identifier in annotators:
            set_grant(db, actor, env["ns"], identifier, "writer", True)
    ds, _ = await replay_data(
        env, split="development", provenance="human_gold", annotators=annotators
    )
    value = ReplayInput(
        dataset_id=ds["dataset_id"], funding_id=env["funding"], idempotency_key="gold"
    )
    with pytest.raises(DomainError) as exc:
        app.evolution.start_replay(actor, c, value)
    assert exc.value.code == "GOLD_ATTESTATIONS_REQUIRED"
    with pytest.raises(DomainError):
        app.evolution.attest_dataset(
            actor,
            ds["dataset_id"],
            DatasetAttestationInput(
                frozen_hash=ds["frozen_hash"], decision="approve", notes="不能冒充他人标注"
            ),
        )
    for identifier in annotators:
        app.evolution.attest_dataset(
            identifier,
            ds["dataset_id"],
            DatasetAttestationInput(
                frozen_hash=ds["frozen_hash"],
                decision="approve",
                notes="核对版本、分组、Gold与保守目标",
            ),
        )
    start = app.evolution.start_replay(actor, c, value)
    assert start["run_id"]
    with pytest.raises(DomainError):
        app.evolution.attest_dataset(
            annotators[0],
            ds["dataset_id"],
            DatasetAttestationInput(
                frozen_hash=ds["frozen_hash"], decision="approve", notes="重复确认不可覆盖"
            ),
        )


def test_template_slots_are_bounded_and_never_become_checked_blocks():
    policy = validate_policy(
        "teaching_skill",
        {"explanation_templates": [{"purpose": "mechanism", "template": "依据 {fact_text} 解释"}]},
    )
    rendered = render_teaching_skill(policy, {"knowledge_units": []})
    assert rendered["answer_guard_required"] and "checked" not in rendered
    for slot in ["{__class__.__mro__}", "{fact_text!r}", "{fact_text:1000000000}", "{unknown}"]:
        with pytest.raises(DomainError):
            validate_policy(
                "teaching_skill",
                {"explanation_templates": [{"purpose": "mechanism", "template": slot}]},
            )


def test_duplicate_questions_and_connected_repo_families_reduce_independent_groups():
    cases = [
        {
            "case_id": str(i),
            "group_id": str(i),
            "repository_family": "repo" + str(i),
            "user_question_group": str(i),
            "question": "same question",
        }
        for i in range(4)
    ]
    assert len(set(independence_groups(cases).values())) == 1
    cases[3]["question"] = "different"
    assert len(set(independence_groups(cases).values())) == 2
    cases[3]["repository_family"] = cases[2]["repository_family"]
    assert len(set(independence_groups(cases).values())) == 1


@pytest.mark.asyncio
async def test_replay_api_requires_explicit_funding_and_returns_private_trace(env):
    app = env["app"]
    c, _, _ = await semantic_candidate(env)
    ds, _ = await replay_data(env)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        denied = await client.post(
            f"/api/v1/evolution/candidates/{c}/replays",
            json={
                "dataset_id": ds["dataset_id"],
                "funding_id": "missing",
                "idempotency_key": "api-denied",
            },
        )
        assert denied.status_code == 403
        accepted = await client.post(
            f"/api/v1/evolution/candidates/{c}/replays",
            json={
                "dataset_id": ds["dataset_id"],
                "funding_id": env["funding"],
                "idempotency_key": "api-replay",
            },
        )
        assert accepted.status_code == 202
        await app.drain()
        detail = await client.get("/api/v1/evolution/replays/" + accepted.json()["replay_id"])
        assert detail.status_code == 200 and detail.json()["trace"]
        with app.db.transaction() as db:
            _, _, token = create_principal(db, "unauthorized-replay-reader")
        hidden = await client.get(
            "/api/v1/evolution/replays/" + accepted.json()["replay_id"],
            headers={"Authorization": "Bearer " + token},
        )
        assert hidden.status_code == 404
