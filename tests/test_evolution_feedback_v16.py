"""Trace collection reads persisted server observations, not client labels."""

import httpx
import pytest
from conftest import publish_fixture
from pydantic import ValidationError
from sqlalchemy import select

from careerforge.api import create_app
from careerforge.contracts import FeedbackInput, ModelOutput, PolicyPatch, TurnInput
from careerforge.contracts_learning_ext import TraceCaptureInput
from careerforge.errors import DomainError
from careerforge.models import Experience, Feedback, Turn
from careerforge.services.policies import ensure_policies
from careerforge.services.security import create_principal


async def complete_turn(env, conv, revision=1, question="请解释 Tool Calling", funding=False):
    turn = env["app"].conversations.submit(
        env["actor"],
        conv,
        TurnInput(
            question=question,
            expected_revision=revision,
            idempotency_key=str(revision),
            funding_id=env["funding"] if funding else None,
        ),
    )
    if funding:
        await env["app"].drain()
    else:
        await env["app"].conversations.process(turn["turn_id"])
    return turn["turn_id"]


@pytest.mark.asyncio
async def test_gap_and_reask_have_distinct_origins_without_truth_or_mastery_labels(env):
    app, actor = env["app"], env["actor"]
    conv = app.conversations.create(actor)
    first = await complete_turn(env, conv, question="请解释 Tool Calling")
    second = await complete_turn(env, conv, 2, question="请解释 Tool Calling")
    with app.db.read() as db:
        assert not db.scalars(select(Experience)).all()  # No ambient collection without consent.
    value = TraceCaptureInput(
        turn_id=second, signals=["reask", "retrieval_gap", "guard_failure"], consent=True
    )
    result = app.evolution.capture_trace_feedback(actor, value)
    assert (
        result["not_observed"] == ["guard_failure"]
        and not result["correctness_or_ability_inferred"]
    )
    assert {x["signal_origin"] for x in result["collected"]} == {
        "behavior_inferred",
        "system_verified",
    }
    assert result["new_model_calls"] == 0
    with app.db.read() as db:
        records = db.scalars(select(Experience)).all()
        assert len(records) == 2
        for record in records:
            assert record.payload["no_correctness_label"] and record.payload["observed_not_mastery"]
            assert record.payload["trace_manifest_hash"] and record.payload["private_scope_only"]
            assert "请解释 Tool Calling" not in str(record.payload)
        reask = next(r for r in records if r.payload["signal"] == "reask")
        assert reask.payload["measurements"]["matching_prior_turn_ids"] == [first]
    repeated = app.evolution.capture_trace_feedback(actor, value)
    assert all(x["replayed"] for x in repeated["collected"])
    assert {x["experience_id"] for x in result["collected"]} == {
        x["experience_id"] for x in repeated["collected"]
    }


@pytest.mark.asyncio
async def test_real_guard_failure_is_system_event_not_source_correction(env):
    app, actor = env["app"], env["actor"]
    await publish_fixture(env)

    def malicious(request):
        unit = request["context"]["evidence_pack"]["knowledge_units"][0]
        fact = unit["facts"][0]
        return ModelOutput(
            synthetic=True,
            structured={
                "blocks": [
                    {
                        "kind": "repository_fact",
                        "text": "永久安全且永远不会失败。",
                        "fact_atom_ids": [fact["fact_atom_id"]],
                        "publication_ids": [unit["publication_id"]],
                        "conditions": fact["conditions"],
                        "transformation": "paraphrase",
                    }
                ]
            },
        )

    env["model"].overrides["tutor"] = malicious
    conv = app.conversations.create(actor)
    turn = await complete_turn(env, conv, question="解释Tool Calling返回路径", funding=True)
    result = app.evolution.capture_trace_feedback(
        actor, TraceCaptureInput(turn_id=turn, signals=["guard_failure"], consent=True)
    )
    assert len(result["collected"]) == 1
    item = result["collected"][0]
    assert item["signal_origin"] == "system_verified" and not result["knowledge_changed"]
    with app.db.read() as db:
        episode = db.get(Experience, item["experience_id"])
        assert episode.payload["measurements"]["guard_failure_count"] >= 1
        assert "ANSWER_HIGH_RISK" in episode.payload["measurements"]["reason_codes"]
        assert episode.diagnosis == "uncertain"


@pytest.mark.asyncio
async def test_trace_consent_private_scope_and_revocation_are_not_bypassable(env):
    app, actor = env["app"], env["actor"]
    conv = app.conversations.create(actor)
    turn = await complete_turn(env, conv)
    with pytest.raises(ValidationError):
        TraceCaptureInput(turn_id=turn, signals=["retrieval_gap"], consent=False)
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "another")
    value = TraceCaptureInput(turn_id=turn, signals=["retrieval_gap"], consent=True)
    with pytest.raises(DomainError):
        app.evolution.capture_trace_feedback(other.id, value)
    result = app.evolution.capture_trace_feedback(actor, value)["collected"][0]
    app.evolution.revoke_feedback(actor, result["feedback_id"])
    with pytest.raises(DomainError) as exc:
        app.evolution.capture_trace_feedback(actor, value)
    assert exc.value.code == "TRACE_CONSENT_WITHDRAWN"
    with app.db.read() as db:
        assert db.get(Feedback, result["feedback_id"]).consent_revision == 0
        assert db.get(Experience, result["experience_id"]).payload["deleted"]


@pytest.mark.asyncio
async def test_frozen_trace_change_blocks_candidate_generation(env):
    app, actor = env["app"], env["actor"]
    conv = app.conversations.create(actor)
    turn = await complete_turn(env, conv)
    result = app.evolution.capture_trace_feedback(
        actor, TraceCaptureInput(turn_id=turn, signals=["retrieval_gap"], consent=True)
    )["collected"][0]
    with app.db.transaction() as db:
        base = ensure_policies(db, actor, env["ns"])["teaching_skill"]["policy_id"]
        db.get(Turn, turn).question = "tampered history"
    with pytest.raises(DomainError) as exc:
        app.evolution.create_candidate(
            actor,
            env["ns"],
            base,
            PolicyPatch(
                target="teaching_skill",
                content={"explanation_order": "code_first"},
                experience_ids=[result["experience_id"]],
                training_group_ids=["conversation:" + conv],
            ),
        )
    assert exc.value.code == "TRACE_CHANGED"


@pytest.mark.asyncio
async def test_trace_api_rejects_invented_labels_and_unobserved_signals(env):
    app, actor = env["app"], env["actor"]
    conv = app.conversations.create(actor)
    turn = await complete_turn(env, conv)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=app)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        wrong = await client.post(
            "/api/v1/evolution/trace-feedback",
            json={"turn_id": turn, "signals": ["guard_failure"], "consent": True, "correct": True},
        )
        assert wrong.status_code == 422
        ok = await client.post(
            "/api/v1/evolution/trace-feedback",
            json={"turn_id": turn, "signals": ["reask", "guard_failure"], "consent": True},
        )
        assert ok.status_code == 201 and ok.json()["collected"] == []
        assert ok.json()["not_observed"] == ["reask", "guard_failure"]
    explicit = app.evolution.feedback(
        actor,
        FeedbackInput(turn_id=turn, signal="reask", content="用户本人说明重复提问", consent=True),
    )
    with app.db.read() as db:
        assert db.get(Feedback, explicit["feedback_id"]).origin == "user_explicit"
