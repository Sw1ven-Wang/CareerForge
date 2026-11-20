"""Synthetic engineering tests, not native execution or teaching quality evidence."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_learning_loop_v16 import accepted_plan, create_check, observation_for
from test_model_assessment_v166 import submitted, submitted_with_fixture_receipt

from careerforge.contracts_learning_ext import AttemptInput, AttemptSubmission
from careerforge.errors import DomainError
from careerforge.models import CommandAuthorization, ExecutionSnapshot, Submission, Workspace
from careerforge.models_learning_ext import LearningAttempt, LearningObservationRecord
from careerforge.services.runtime_evidence import runtime_evidence_in


async def test_transactional_submit_keeps_exact_text_and_rolls_back(env):
    app, actor = env["app"], env["actor"]
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan)
    attempt = app.learning.start_attempt(
        actor, activity["activity_id"], AttemptInput(idempotency_key="raw")
    )
    answer = "  原始答案\r\nprint('☃')\t\n\u0065\u0301  "
    value = AttemptSubmission(
        response=answer, transfer_notes="  原始说明\r\n", external_help="unknown"
    )
    with pytest.raises(RuntimeError), app.db.transaction() as db:
        app.learning.submit_attempt_in(db, actor, attempt["attempt_id"], value)
        raise RuntimeError("crash before receipt commit")
    with app.db.read() as db:
        assert db.get(LearningAttempt, attempt["attempt_id"]).state == "started"
        assert not db.get(Workspace, attempt["workspace_id"]).sealed
    with app.db.transaction() as db:
        first = app.learning.submit_attempt_in(db, actor, attempt["attempt_id"], value)
    with app.db.read() as db:
        saved = db.get(LearningAttempt, attempt["attempt_id"])
        assert saved.response.encode() == answer.encode()
        assert saved.transfer_notes == value.transfer_notes
        assert saved.external_help == "unknown"
    assert not first["competence_verified"]
    assert app.learning.submit_attempt(actor, attempt["attempt_id"], value)["replayed"]
    with pytest.raises(DomainError) as failed:
        app.learning.submit_attempt(
            actor, attempt["attempt_id"], AttemptSubmission(response="changed")
        )
    assert failed.value.code == "ATTEMPT_IMMUTABLE"


async def test_public_wrapper_guard_precedes_mutation(env, monkeypatch):
    _, attempt = await submitted(env)
    calls = []

    def guard(db, actor, identifier, operation):
        calls.append(("guard", identifier, operation))
        raise DomainError("WORKFLOW_NOT_ACTIVE", "paused", 409)

    def after(db, actor, identifier, operation):
        calls.append(("after", identifier, operation))

    monkeypatch.setattr(
        env["app"],
        "practice_workflows",
        SimpleNamespace(guard_direct_attempt_in=guard, after_direct_attempt_in=after),
        raising=False,
    )
    with pytest.raises(DomainError):
        env["app"].learning.close_attempt(env["actor"], attempt["attempt_id"])
    assert calls == [("guard", attempt["attempt_id"], "close")]


def observation(activity):
    value = observation_for(activity)
    for item in value.rubric_results:
        item.evidence_excerpt = "函数返回传入的 value。"
    return value


@pytest.mark.parametrize(
    "damage",
    [
        "fake_id",
        "owner",
        "snapshot_files",
        "argv",
        "authorization",
        "snapshot_identity",
        "synthetic_flip",
        "submission_revoked",
        "started_at",
    ],
)
async def test_runtime_chain_rejects_forgery_for_assessment_and_observation(env, damage):
    app, actor = env["app"], env["actor"]
    activity, attempt, proposed, sub = await submitted_with_fixture_receipt(env)
    with app.db.transaction() as db:
        row = db.get(CommandAuthorization, proposed["command_id"])
        if damage == "fake_id":
            saved = db.get(LearningAttempt, attempt["attempt_id"])
            saved.evidence = {**saved.evidence, "execution_receipt_ids": ["fabricated"]}
        elif damage == "owner":
            from careerforge.services.security import create_principal

            other, _, _ = create_principal(db, "different-command-owner")
            row.principal_id = other.id
        elif damage == "snapshot_files":
            db.get(ExecutionSnapshot, row.execution_snapshot_id).files = {"fake.py": "pass"}
        elif damage == "argv":
            row.argv = ["python", "forged.py"]
        elif damage == "authorization":
            row.authorization_digest = "f" * 64
            row.receipt = {**row.receipt, "authorization_digest": row.authorization_digest}
        elif damage == "snapshot_identity":
            row.receipt = {**row.receipt, "execution_snapshot_id": "fabricated"}
        elif damage == "synthetic_flip":
            row.receipt = {**row.receipt, "synthetic": False}
        elif damage == "submission_revoked":
            db.get(Submission, sub["submission_id"]).revoked = True
        else:
            row.started_at = None
    with app.db.read() as db, pytest.raises(DomainError) as failed:
        app.assessments._snapshot(db, actor, attempt["attempt_id"])
    assert failed.value.code == "RUNTIME_EVIDENCE"
    with pytest.raises(DomainError) as failed:
        app.learning.record_observation(actor, attempt["attempt_id"], observation(activity))
    assert failed.value.code == "RUNTIME_EVIDENCE"
    with app.db.read() as db:
        assert db.scalar(select(LearningObservationRecord)) is None


async def test_fixture_runtime_is_not_demonstrated(env):
    app, actor = env["app"], env["actor"]
    activity, attempt, _, _ = await submitted_with_fixture_receipt(env)
    with app.db.read() as db:
        evidence = runtime_evidence_in(db, db.get(LearningAttempt, attempt["attempt_id"]))
        assert evidence[0]["synthetic"] and not evidence[0]["runtime_observed_not_mastery"]
    value = observation(activity)
    with pytest.raises(DomainError) as failed:
        app.learning.record_observation(actor, attempt["attempt_id"], value)
    assert failed.value.code == "RUNTIME_EVIDENCE"
    for item in value.rubric_results:
        if (
            next(r for r in activity["specification"]["rubric"] if r["id"] == item.rubric_item_id)[
                "key"
            ]
            == "runtime"
        ):
            item.outcome, item.evidence_excerpt = "unknown", ""
    result = app.learning.record_observation(actor, attempt["attempt_id"], value)
    assert not result["competence_verified"]
    assert result["assessment"] == "specific_gaps_or_unknown"


async def test_unpaid_pending_tutor_blocks_independent_attempt_account_wide(env):
    from test_learning_graph_admission_m4 import direct

    from careerforge.contracts_learning_ext import ActivityInput
    from careerforge.models_dialogue import WorkflowTutorRequest

    app, actor = env["app"], env["actor"]
    _, admitted, _, result = await direct(env, funding=False)
    request_id = result["action_receipts"][0]["request_id"]
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, request_id)
        assert request.status == "pending_authorization"
        assert request.turn_id is None and request.run_id is None
    activity = app.learning.create_activity(
        actor,
        ActivityInput(module_id=admitted.module_id, kind="exercise", explicitly_requested=True),
    )
    # This legacy domain endpoint has no conversation identity: the fence must
    # scan the owner account rather than rely on a caller's current conversation.
    with pytest.raises(DomainError) as failed:
        app.learning.start_attempt(
            actor,
            activity["activity_id"],
            AttemptInput(assistance_mode="independent", idempotency_key="no-unpaid-bypass"),
        )
    assert failed.value.code == "ASSISTANCE_ACTIVE"


async def test_authorized_human_review_uses_attempt_owner_runtime_chain(env):
    from careerforge.services.security import create_principal, set_grant

    app, actor = env["app"], env["actor"]
    activity, attempt, _, _ = await submitted_with_fixture_receipt(env)
    with app.db.transaction() as db:
        reviewer, _, _ = create_principal(db, "runtime-reviewer")
        reviewer_id = reviewer.id
        set_grant(db, actor, env["ns"], reviewer_id, "writer", True)
    value = observation(activity)
    value.evaluator_kind = "human_review"
    with pytest.raises(DomainError) as failed:
        app.learning.record_observation(reviewer_id, attempt["attempt_id"], value)
    # The evidence was found under the true owner, then rejected as synthetic.
    # Reusing Assessment._snapshot with reviewer identity would fail NOT_FOUND.
    assert failed.value.code == "RUNTIME_EVIDENCE"
    for item in value.rubric_results:
        if (
            next(r for r in activity["specification"]["rubric"] if r["id"] == item.rubric_item_id)[
                "key"
            ]
            == "runtime"
        ):
            item.outcome, item.evidence_excerpt = "unknown", ""
    result = app.learning.record_observation(reviewer_id, attempt["attempt_id"], value)
    assert result["evaluator_kind"] == "human_review"
    assert not result["competence_verified"]


async def test_close_helper_and_wake_failure_roll_back_together(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan)
    attempt = app.learning.start_attempt(
        actor, activity["activity_id"], AttemptInput(idempotency_key="close-atomic")
    )

    def guard(*args):
        pass

    def after(*args):
        raise RuntimeError("wake/action receipt did not commit")

    monkeypatch.setattr(
        app,
        "practice_workflows",
        SimpleNamespace(guard_direct_attempt_in=guard, after_direct_attempt_in=after),
        raising=False,
    )
    with pytest.raises(RuntimeError):
        app.learning.close_attempt(actor, attempt["attempt_id"])
    with app.db.read() as db:
        assert db.get(LearningAttempt, attempt["attempt_id"]).state == "started"
        assert not db.get(Workspace, attempt["workspace_id"]).sealed
    with app.db.transaction() as db:
        app.learning.close_attempt_in(db, actor, attempt["attempt_id"])
    with app.db.read() as db:
        assert db.get(LearningAttempt, attempt["attempt_id"]).state == "abandoned"
        assert db.get(Workspace, attempt["workspace_id"]).sealed


async def test_historical_unverifiable_runtime_is_read_only_unknown_projection(env):
    app, actor = env["app"], env["actor"]
    activity, attempt, _, _ = await submitted_with_fixture_receipt(env)
    value = observation(activity)
    runtime_id = next(r["id"] for r in activity["specification"]["rubric"] if r["key"] == "runtime")
    for item in value.rubric_results:
        if item.rubric_item_id == runtime_id:
            item.outcome, item.evidence_excerpt = "unknown", ""
    result = app.learning.record_observation(actor, attempt["attempt_id"], value)
    # Negative historical-record fixture: reproduce the old missing-verifier gap.
    # It must not be accepted as current runtime proof or rewritten in storage.
    with app.db.transaction() as db:
        row = db.get(LearningObservationRecord, result["observation_id"])
        row.rubric_results = [
            {**r, "outcome": "demonstrated"} if r["rubric_item_id"] == runtime_id else r
            for r in row.rubric_results
        ]
        recorded = row.rubric_results
        row.assessment = "self_reported_under_declared_conditions"
    for values in (
        app.learning.observations(actor)["observations"],
        app.learning.attempt_detail(actor, attempt["attempt_id"])["observations"],
    ):
        item = next(r for r in values[0]["rubric_results"] if r["rubric_item_id"] == runtime_id)
        assert item["outcome"] == "unknown" and item["recorded_outcome"] == "demonstrated"
        assert values[0]["assessment"] == "specific_gaps_or_unknown"
        assert not values[0]["runtime_evidence_available"]
    with app.db.read() as db:
        row = db.get(LearningObservationRecord, result["observation_id"])
        assert row.rubric_results == recorded
        assert row.assessment == "self_reported_under_declared_conditions"
    detail = app.learning.attempt_detail(actor, attempt["attempt_id"])
    assert not detail["evidence"]["execution_observations"][0]["runtime_observed_not_mastery"]


async def test_observation_rejects_attempt_specification_drift(env):
    activity, attempt = await submitted(env)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        db.get(LearningAttempt, attempt["attempt_id"]).specification_hash = "f" * 64
    with pytest.raises(DomainError) as failed:
        app.learning.record_observation(actor, attempt["attempt_id"], observation(activity))
    assert failed.value.code == "ACTIVITY_HASH"
