"""Evaluation evidence cannot approve a different live environment or consent scope."""

import pytest
from sqlalchemy import select
from test_evolution import candidate, dataset

from careerforge.errors import DomainError
from careerforge.models import (
    EvalDataset,
    EvolutionCandidate,
    EvolutionEvaluation,
    Namespace,
    PolicyHead,
    PolicyRollout,
    Principal,
)


@pytest.mark.parametrize("action", ["promote", "canary"])
@pytest.mark.parametrize("drift", ["provider", "authorization", "namespace", "dataset_split"])
@pytest.mark.asyncio
async def test_release_requires_current_evaluation_environment(env, action, drift):
    app, actor = env["app"], env["actor"]
    identifier, base, _, _, _ = await candidate(env)
    dataset_id = dataset(env)
    report = await app.evolution.evaluate(actor, identifier, dataset_id)
    assert report["decision"] == "passed"
    if drift == "provider":
        env["model"].profile_version = "fixture-profile-changed-after-evaluation"
    else:
        with app.db.transaction() as db:
            if drift == "authorization":
                db.get(Principal, actor).auth_revision += 1
            elif drift == "namespace":
                db.get(Namespace, env["ns"]).revision += 1
            else:
                db.get(EvalDataset, dataset_id).split = "training"
    with pytest.raises(DomainError):
        if action == "promote":
            app.evolution.promote(actor, identifier, report["evaluation_id"], 1)
        else:
            app.rollouts.start(actor, identifier, report["evaluation_id"], 1, 50, 1, 20)
    with app.db.read() as db:
        assert (
            db.scalar(
                select(PolicyHead).where(
                    PolicyHead.namespace_id == env["ns"], PolicyHead.target == "teaching_skill"
                )
            ).policy_id
            == base
        )
        assert not db.scalars(select(PolicyRollout)).all()


@pytest.mark.asyncio
async def test_consent_withdrawn_during_evaluation_is_not_resurrected(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    identifier, _, _, _, feedback = await candidate(env)
    dataset_id = dataset(env)
    original = app.evolution._score
    withdrawn = False

    async def score(*args):
        nonlocal withdrawn
        value = await original(*args)
        if not withdrawn:
            withdrawn = True
            app.evolution.revoke_feedback(actor, feedback["feedback_id"])
        return value

    monkeypatch.setattr(app.evolution, "_score", score)
    with pytest.raises(DomainError):
        await app.evolution.evaluate(actor, identifier, dataset_id)
    with app.db.read() as db:
        assert db.get(EvolutionCandidate, identifier).status == "consent_revoked"
        assert not db.scalars(select(EvolutionEvaluation)).all()


@pytest.mark.asyncio
async def test_promoted_candidate_cannot_be_downgraded_by_re_evaluation(env):
    app, actor = env["app"], env["actor"]
    identifier, _, _, _, _ = await candidate(env)
    dataset_id = dataset(env)
    report = await app.evolution.evaluate(actor, identifier, dataset_id)
    app.evolution.promote(actor, identifier, report["evaluation_id"], 1)
    with pytest.raises(DomainError):
        await app.evolution.evaluate(actor, identifier, dataset_id)
    with app.db.read() as db:
        assert db.get(EvolutionCandidate, identifier).status == "promoted"
