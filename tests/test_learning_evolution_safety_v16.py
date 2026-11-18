"""Adversarial boundaries for personal learning and bounded evolution."""

import pytest
from sqlalchemy import select
from test_evolution import candidate, dataset
from test_evolution_replay_v16 import replay_data, run_replay, semantic_candidate
from test_learning_loop_v16 import accepted_plan, create_check
from test_privacy_v16 import confirm, service

from careerforge.contracts import FeedbackInput, ModelOutput, TurnInput
from careerforge.contracts_learning_ext import AttemptInput, AttemptSubmission, ReplayInput
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    EvolutionCandidate,
    EvolutionEvaluation,
    Feedback,
    PolicyHead,
    Turn,
    Workspace,
)
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.security import private_namespace
from careerforge.util import fingerprint, manifest


@pytest.mark.asyncio
async def test_queued_replay_and_reflection_block_independent_attempt(env):
    app, actor = env["app"], env["actor"]
    c, _, feedback = await semantic_candidate(env)
    ds, _ = await replay_data(env)
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan)
    replay = app.evolution.start_replay(
        actor,
        c,
        ReplayInput(
            dataset_id=ds["dataset_id"], funding_id=env["funding"], idempotency_key="pending"
        ),
    )
    with pytest.raises(DomainError) as exc:
        app.learning.start_attempt(
            actor,
            activity["activity_id"],
            AttemptInput(assistance_mode="independent", idempotency_key="attempt"),
        )
    assert exc.value.code == "ASSISTANCE_ACTIVE"
    app.runtime.cancel(actor, replay["run_id"], 1)
    reflection = app.evolution.start_candidate(
        actor,
        env["ns"],
        env["funding"],
        [feedback["experience_id"]],
        "teaching_skill",
        "pending-reflect",
    )
    with pytest.raises(DomainError) as exc:
        app.learning.start_attempt(
            actor,
            activity["activity_id"],
            AttemptInput(assistance_mode="independent", idempotency_key="attempt"),
        )
    assert exc.value.code == "ASSISTANCE_ACTIVE"
    app.runtime.cancel(actor, reflection, 1)
    assert (
        app.learning.start_attempt(
            actor,
            activity["activity_id"],
            AttemptInput(assistance_mode="independent", idempotency_key="attempt"),
        )["state"]
        == "started"
    )


@pytest.mark.asyncio
async def test_independent_attempt_hides_completed_replay_and_blocks_new_assistance(env):
    app, actor = env["app"], env["actor"]
    c, _, _ = await semantic_candidate(env)
    ds, _ = await replay_data(env)
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan)
    replay, _ = await run_replay(env, c, ds)
    assert app.evolution.replay_detail(actor, replay["replay_id"])["trace"]
    app.learning.start_attempt(
        actor,
        activity["activity_id"],
        AttemptInput(assistance_mode="independent", idempotency_key="attempt"),
    )
    hidden = app.evolution.replay_detail(actor, replay["replay_id"])
    assert hidden["trace"] == [] and not hidden["evidence_available"]
    with pytest.raises(DomainError) as exc:
        app.evolution.start_replay(
            actor,
            c,
            ReplayInput(
                dataset_id=ds["dataset_id"],
                funding_id=env["funding"],
                idempotency_key="after-attempt",
            ),
        )
    assert exc.value.code == "INDEPENDENT_ATTEMPT"


@pytest.mark.asyncio
async def test_replay_worker_rechecks_independent_attempt_before_any_model_call(env):
    app, actor = env["app"], env["actor"]
    c, _, _ = await semantic_candidate(env)
    ds, _ = await replay_data(env)
    replay = app.evolution.start_replay(
        actor,
        c,
        ReplayInput(
            dataset_id=ds["dataset_id"],
            funding_id=env["funding"],
            idempotency_key="late-independent",
        ),
    )
    # Deliberate internal corruption models an old queued task crossing a new attempt.
    # Public starters reject this state; the worker must independently fail closed.
    with app.db.transaction() as db:
        db.add(
            Workspace(
                principal_id=actor,
                namespace_id=env["ns"],
                files={},
                manifest_hash=fingerprint(manifest({})),
                assistance_mode="independent",
            )
        )
    before = len(env["model"].calls)
    await app.execute(replay["run_id"])
    assert len(env["model"].calls) == before
    with app.db.read() as db:
        assert db.get(AgentRun, replay["run_id"]).status == "failed"
        assert not db.scalars(
            select(EvolutionEvaluation).where(EvolutionEvaluation.candidate_id == c)
        ).all()


@pytest.mark.asyncio
async def test_reflection_cancel_after_paid_gateway_result_cannot_create_candidate(
    env, monkeypatch
):
    app, actor = env["app"], env["actor"]
    _, base, conv, _, feedback = await candidate(env)
    env["model"].overrides["reflection"] = lambda request: ModelOutput(
        synthetic=True,
        structured={
            "target": "teaching_skill",
            "content": {"explanation_order": "comparison_first"},
            "experience_ids": [feedback["experience_id"]],
            "training_group_ids": ["conversation:" + conv],
        },
    )
    run = app.evolution.start_candidate(
        actor,
        env["ns"],
        env["funding"],
        [feedback["experience_id"]],
        "teaching_skill",
        "cancel-late",
    )
    original = app.gateway.call

    async def cancel_after_result(*args, **kwargs):
        result = await original(*args, **kwargs)
        with app.db.read() as db:
            revision = db.get(AgentRun, run).revision
        app.runtime.cancel(actor, run, revision)
        return result

    monkeypatch.setattr(app.gateway, "call", cancel_after_result)
    await app.execute(run)
    assert app.runtime.stage(run, "result") is None
    with app.db.read() as db:
        assert len(db.scalars(select(EvolutionCandidate)).all()) == 1
        assert (
            db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill")).policy_id
            == base
        )
        assert db.get(AgentRun, run).status == "cancelled"


@pytest.mark.asyncio
async def test_canary_hard_gate_failure_stops_before_minimum_sample_count(env):
    app, actor = env["app"], env["actor"]
    c, _, _, _, _ = await candidate(env)
    evaluated = await app.evolution.evaluate(actor, c, dataset(env))
    trial = app.rollouts.start(
        actor, c, evaluated["evaluation_id"], 1, minimum_per_arm=10, maximum_turns=20
    )
    conv = app.conversations.create(actor)
    turn = app.conversations.submit(
        actor,
        conv,
        TurnInput(question="学习Tool Calling", expected_revision=1, idempotency_key="turn"),
    )
    # Internal monitor receives the same structured event shape as real Answer Guard.
    app.rollouts.observe_turn(
        turn["turn_id"], [{"blocked_reason": "ANSWER_HIGH_RISK"}], 10, "completed"
    )
    detail = app.rollouts.detail(actor, trial["rollout_id"])
    assert detail["status"] == "reverted" and detail["reason"] == "hard_gate_violation"


@pytest.mark.asyncio
async def test_canary_assignment_limit_counts_pending_not_only_observed_turns(env):
    app, actor = env["app"], env["actor"]
    c, _, _, _, _ = await candidate(env)
    evaluated = await app.evolution.evaluate(actor, c, dataset(env))
    trial = app.rollouts.start(
        actor, c, evaluated["evaluation_id"], 1, minimum_per_arm=1, maximum_turns=2
    )
    assigned = []
    for i in range(3):
        conv = app.conversations.create(actor)
        turn = app.conversations.submit(
            actor,
            conv,
            TurnInput(question="学习Tool Calling", expected_revision=1, idempotency_key=str(i)),
        )
        with app.db.read() as db:
            assigned.append(db.get(Turn, turn["turn_id"]).policy_versions["teaching_skill"])
    assert sum(p.get("rollout_id") == trial["rollout_id"] for p in assigned) == 2
    assert app.rollouts.detail(actor, trial["rollout_id"])["status"] == "stopped"


@pytest.mark.asyncio
async def test_canary_revalidates_release_evidence_before_new_assignment(env):
    app, actor = env["app"], env["actor"]
    c, base, _, _, _ = await candidate(env)
    evaluated = await app.evolution.evaluate(actor, c, dataset(env))
    trial = app.rollouts.start(
        actor, c, evaluated["evaluation_id"], 1, minimum_per_arm=1, maximum_turns=2
    )
    with app.db.transaction() as db:
        db.get(EvolutionEvaluation, evaluated["evaluation_id"]).decision = "failed"
    conv = app.conversations.create(actor)
    turn = app.conversations.submit(
        actor,
        conv,
        TurnInput(question="学习Tool Calling", expected_revision=1, idempotency_key="turn"),
    )
    with app.db.read() as db:
        ref = db.get(Turn, turn["turn_id"]).policy_versions["teaching_skill"]
        assert "rollout_id" not in ref and ref["policy_id"] == base
    assert (
        app.rollouts.detail(actor, trial["rollout_id"])["reason"] == "release_evidence_invalidated"
    )


@pytest.mark.asyncio
async def test_feedback_and_attempt_writes_respect_pending_erasure_gate(env):
    app, actor = env["app"], env["actor"]
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan)
    attempt = app.learning.start_attempt(
        actor, activity["activity_id"], AttemptInput(idempotency_key="a")
    )
    conv = app.conversations.create(actor)
    turn = app.conversations.submit(
        actor, conv, TurnInput(question="私人问题", expected_revision=1, idempotency_key="private")
    )
    await app.conversations.process(turn["turn_id"])
    with app.db.transaction() as db:
        db.scalar(select(PrivacyState).where(PrivacyState.principal_id == actor)).blocked = True
    for operation in [
        lambda: app.evolution.feedback(
            actor,
            FeedbackInput(
                turn_id=turn["turn_id"],
                signal="explicit_feedback",
                content="MUST_NOT_WRITE",
                consent=False,
            ),
        ),
        lambda: app.learning.submit_attempt(
            actor, attempt["attempt_id"], AttemptSubmission(response="MUST_NOT_WRITE")
        ),
        lambda: app.learning.close_attempt(actor, attempt["attempt_id"]),
        lambda: app.learning.start_attempt(
            actor, activity["activity_id"], AttemptInput(idempotency_key="a")
        ),
    ]:
        with pytest.raises(DomainError) as exc:
            operation()
        assert exc.value.code == "PRIVACY_ERASURE_PENDING"
    with app.db.read() as db:
        assert not db.scalars(select(Feedback)).all()


@pytest.mark.asyncio
async def test_erasure_fresh_profile_cannot_reanimate_old_conversation_or_plan(env):
    app, actor = env["app"], env["actor"]
    _, plan = await accepted_plan(env, personalized=False)
    old_profile = app.learning.profile(actor)
    conv = app.conversations.create(actor)
    turn = app.conversations.submit(
        actor,
        conv,
        TurnInput(question="旧私人问题", expected_revision=1, idempotency_key="private"),
    )
    await app.conversations.process(turn["turn_id"])
    svc = service(env)
    await confirm(svc, actor, svc.propose(actor))
    new_profile = app.learning.profile(actor)
    assert (
        new_profile["active_plan_id"] is None and new_profile["revision"] > old_profile["revision"]
    )
    assert new_profile["preferences"]["self_reported_context"] == ""
    with app.db.read() as db:
        assert private_namespace(db, actor).id != env["ns"]
    with pytest.raises(DomainError):
        app.evolution.feedback(
            actor,
            FeedbackInput(
                turn_id=turn["turn_id"],
                signal="explicit_feedback",
                content="OLD_BODY",
                consent=True,
            ),
        )
    with pytest.raises(DomainError):
        app.learning.detail(actor, plan["plan_id"])
