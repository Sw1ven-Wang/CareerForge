"""Adversarial engineering contracts, never native quality evidence."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_practice_graph_m5 import command, session, start, target

from careerforge.contracts import TurnInput
from careerforge.contracts_assessment_v166 import AssessmentInput
from careerforge.contracts_conversation_actions_v1 import DialogueMessage
from careerforge.contracts_learning_ext import AttemptSubmission
from careerforge.db import clock
from careerforge.errors import DomainError
from careerforge.models import Conversation, Experience, Feedback, Workspace
from careerforge.models_dialogue import (
    ConversationActionReceipt,
    WorkflowAssessmentRequest,
    WorkflowExecution,
    WorkflowPracticeRequest,
    WorkflowSession,
)
from careerforge.models_learning_ext import LearningAttempt


async def submitted(env):
    conv, wid = await session(env)
    p, _, _ = await start(env, conv, wid)
    result, _ = command(
        env,
        conv,
        wid,
        dict(type="submit_practice", **target(p), submission=dict(response="原始答案")),
        "submit",
    )
    assert result["action_receipts"][0]["status"] == "applied", result
    await env["app"].drain()
    return conv, wid, env["app"].practice_workflows.detail(env["actor"], conv, p["practice_id"])


def control(env, conv, wid, kind, key):
    app = env["app"]
    with app.db.read() as db:
        revision = db.get(Conversation, conv).revision
    return app.dialogue.dispatch(
        env["actor"],
        conv,
        DialogueMessage(text=kind, expected_focus_revision=revision, idempotency_key=key),
        forced_actions=[(kind, {})],
        workflow_id=wid,
    )


async def test_followup_not_submission_and_direct_assessment_denied(env):
    app = env["app"]
    conv, wid = await session(env)
    p, _, _ = await start(env, conv, wid)
    rev = app.conversations.detail(env["actor"], conv)["revision"]
    with pytest.raises(DomainError, match="普通追问"):
        app.conversations.submit(
            env["actor"],
            conv,
            TurnInput(
                question="这是否是我的答案？", expected_revision=rev, idempotency_key="ordinary"
            ),
        )
    with app.db.read() as db:
        assert db.get(LearningAttempt, p["attempt_id"]).state == "started"
    with pytest.raises(DomainError) as error:
        app.assessments.start(
            env["actor"],
            AssessmentInput(
                namespace_id=env["ns"],
                funding_id=env["funding"],
                attempt_id=p["attempt_id"],
                idempotency_key="bypass",
            ),
        )
    assert error.value.code == "PRACTICE_ASSESSMENT_REQUIRED"


async def test_failed_submission_new_key_recovers_exact_answer(env):
    app = env["app"]
    conv, wid = await session(env)
    p, _, _ = await start(env, conv, wid)
    bad, badmsg = command(
        env,
        conv,
        wid,
        dict(type="submit_practice", **target(p), submission=dict(response=" \n ")),
        "bad",
    )
    assert bad["action_receipts"][0]["failure_code"] == "PRACTICE_ANSWER_REQUIRED"
    assert app.dialogue.dispatch(env["actor"], conv, badmsg) == bad
    good, _ = command(
        env,
        conv,
        wid,
        dict(type="submit_practice", **target(p), submission=dict(response="\n真实本人原文  ")),
        "good",
    )
    assert good["action_receipts"][0]["status"] == "applied"
    with app.db.read() as db:
        assert db.get(LearningAttempt, p["attempt_id"]).response == "\n真实本人原文  "


async def test_pending_assessment_no_run_exact_authorization(env):
    app = env["app"]
    conv, wid, p = await submitted(env)
    result, _ = command(
        env,
        conv,
        wid,
        dict(type="request_assessment", **target(p), submission_hash=p["submission_hash"]),
        "request",
    )
    req = result["action_receipts"][0]
    await app.drain()
    with app.db.read() as db:
        request = db.get(WorkflowAssessmentRequest, req["request_id"])
        assert request.status == "pending_authorization" and request.run_id is None
    again, _ = command(
        env,
        conv,
        wid,
        dict(type="request_assessment", **target(p), submission_hash=p["submission_hash"]),
        "again",
    )
    assert again["action_receipts"][0]["failure_code"] == "ASSESSMENT_PENDING"
    auth, _ = command(
        env,
        conv,
        wid,
        dict(
            type="authorize_assessment_request",
            assessment_request_id=req["request_id"],
            request_hash=req["request_hash"],
        ),
        "auth",
        funding=env["funding"],
    )
    assert auth["action_receipts"][0]["status"] == "applied", auth
    with app.db.read() as db:
        assert db.get(WorkflowAssessmentRequest, req["request_id"]).status == "ready"


async def test_pending_expiry_reaper_is_durable_no_call(env, monkeypatch):
    app = env["app"]
    conv, wid, p = await submitted(env)
    result, _ = command(
        env,
        conv,
        wid,
        dict(type="request_assessment", **target(p), submission_hash=p["submission_hash"]),
        "request",
    )
    await app.drain()
    with app.db.read() as db:
        request = db.get(WorkflowAssessmentRequest, result["action_receipts"][0]["request_id"])
        future = clock(db) + timedelta(hours=25)
    monkeypatch.setattr("careerforge.services.workflow_practice.clock", lambda db: future)
    app.session_workflows.reap()
    await app.drain()
    with app.db.read() as db:
        request = db.get(WorkflowAssessmentRequest, request.id)
        assert request.status == "expired" and request.run_id is None
        assert request.active_practice_id is None


async def test_feedback_default_no_consent_exactly_once(env):
    app = env["app"]
    conv, wid, p = await submitted(env)
    with app.db.read() as db:
        practice = db.get(WorkflowPracticeRequest, p["practice_id"])
        turn = practice.request["source_tutor"]["turn_id"]
        before = db.scalar(select(func.count()).select_from(Experience))
    result, msg = command(
        env,
        conv,
        wid,
        dict(
            type="record_tutor_feedback",
            **target(p),
            feedback=dict(turn_id=turn, signal="explicit_feedback", content="希望具体解释边界"),
        ),
        "feedback",
    )
    assert result["action_receipts"][0]["status"] == "applied", result
    assert app.dialogue.dispatch(env["actor"], conv, msg) == result
    with app.db.read() as db:
        assert (
            db.scalar(select(func.count()).select_from(Feedback).where(Feedback.turn_id == turn))
            == 1
        )
        assert db.scalar(select(func.count()).select_from(Experience)) == before


async def test_pause_rejects_direct_submit_resume_original_wait(env):
    app = env["app"]
    conv, wid = await session(env)
    p, _, _ = await start(env, conv, wid)
    paused = control(env, conv, wid, "request_pause", "pause")
    assert paused["action_receipts"][0]["status"] == "applied", paused
    await app.drain()
    with pytest.raises(DomainError) as error:
        app.learning.submit_attempt(
            env["actor"], p["attempt_id"], AttemptSubmission(response="cannot sneak")
        )
    assert error.value.code == "WORKFLOW_NOT_ACTIVE"
    resumed = control(env, conv, wid, "resume_workflow", "resume")
    assert resumed["action_receipts"][0]["status"] == "applied", resumed
    await app.drain()
    with app.db.read() as db:
        workflow = db.get(WorkflowSession, wid)
        execution = db.get(WorkflowExecution, workflow.current_execution_id)
        assert execution.wait_kind == "practice_answer"
        assert db.get(LearningAttempt, p["attempt_id"]).state == "started"


async def test_cancel_seals_only_owned_independent_workspace(env):
    app = env["app"]
    conv, wid = await session(env)
    p, _, _ = await start(env, conv, wid, mode="independent")
    result = control(env, conv, wid, "cancel_workflow", "cancel")
    assert result["action_receipts"][0]["status"] == "applied", result
    with app.db.read() as db:
        practice = db.get(WorkflowPracticeRequest, p["practice_id"])
        attempt = db.get(LearningAttempt, p["attempt_id"])
        assert practice.status == "cancelled" and practice.active_conversation_id is None
        assert attempt.state == "abandoned" and db.get(Workspace, attempt.workspace_id).sealed


async def test_tampered_creation_action_hash_rejected(env):
    app = env["app"]
    conv, wid = await session(env)
    p, _, created = await start(env, conv, wid)
    with app.db.transaction() as db:
        receipt = db.get(ConversationActionReceipt, created["receipt_id"])
        receipt.action_plan_hash = "0" * 64
    with pytest.raises(DomainError) as error:
        app.practice_workflows.detail(env["actor"], conv, p["practice_id"])
    assert error.value.code == "PRACTICE_BINDING"


async def test_unknown_funding_does_not_create_new_action_or_request(env):
    app = env["app"]
    conv, wid, p = await submitted(env)
    with app.db.read() as db:
        count = db.scalar(select(func.count()).select_from(ConversationActionReceipt))
    with pytest.raises(DomainError) as error:
        command(
            env,
            conv,
            wid,
            dict(type="request_assessment", **target(p), submission_hash=p["submission_hash"]),
            "bad-grant",
            funding="missing",
        )
    assert error.value.code == "NOT_FOUND"
    with app.db.read() as db:
        assert db.scalar(select(func.count()).select_from(ConversationActionReceipt)) == count
        assert db.scalar(select(func.count()).select_from(WorkflowAssessmentRequest)) == 0


async def test_explicit_same_chat_ack_progress_no_mastery(env):
    from test_session_module_progress_v166 import ack_input

    app = env["app"]
    conv, wid, p = await submitted(env)
    with app.db.read() as db:
        practice = db.get(WorkflowPracticeRequest, p["practice_id"])
        turn_id = practice.request["source_tutor"]["turn_id"]
    binding = app.conversations.session_binding(env["actor"], turn_id)
    detail = app.conversations.teaching_session(env["actor"], binding["learning_session_id"])
    ack = ack_input(env, detail).model_copy(update={"attempt_id": p["attempt_id"]})
    before = app.learning.profile(env["actor"])["revision"]
    with pytest.raises(DomainError) as failed:
        app.conversations.acknowledge_module(env["actor"], ack)
    assert failed.value.code == "PRACTICE_ACK_REQUIRED"
    result, msg = command(
        env,
        conv,
        wid,
        dict(type="acknowledge_module", **target(p), acknowledgment=ack.model_dump(mode="json")),
        "ack",
    )
    assert result["action_receipts"][0]["status"] == "applied", result
    assert result["action_receipts"][0]["competence_verified"] is False
    assert app.learning.profile(env["actor"])["revision"] == before + 1
    assert app.dialogue.dispatch(env["actor"], conv, msg) == result
    await app.drain()
    with app.db.read() as db:
        assert db.get(WorkflowPracticeRequest, p["practice_id"]).status == "completed"


@pytest.mark.parametrize("revocation", ["role", "data", "source"])
async def test_candidate_commit_rechecks_exact_grant_after_model_receipt(env, revocation):
    from careerforge.contracts import ModelOutput
    from careerforge.models import AgentRun, FundingAgreement

    env["model"].overrides["evaluation"] = lambda request: ModelOutput(
        structured={
            "rubric_items": [
                {
                    "rubric_item_id": item["id"],
                    "outcome": "unknown",
                    "submission_quote": "",
                    "fact_atom_ids": [],
                    "explanation": "synthetic unknown",
                }
                for item in request["context"]["rubric"]
            ],
            "limitations": ["engineering fixture"],
        },
        synthetic=True,
    )
    app = env["app"]
    conv, wid, p = await submitted(env)
    result, _ = command(
        env,
        conv,
        wid,
        dict(type="request_assessment", **target(p), submission_hash=p["submission_hash"]),
        "grant-check",
        funding=env["funding"],
    )
    with app.db.read() as db:
        execution_id = db.get(WorkflowSession, wid).current_execution_id
    await app.execute_workflow(execution_id)
    with app.db.read() as db:
        request = db.get(WorkflowAssessmentRequest, result["action_receipts"][0]["request_id"])
        identifier = request.run_id
    epoch = app.runtime.claim(identifier, "authority-worker")
    assert epoch is not None
    with app.db.read() as db:
        run = db.get(AgentRun, identifier)
    await app.assessments.evaluate_candidate(run, epoch, "authority-worker")
    with app.db.transaction() as db:
        funding = db.get(FundingAgreement, env["funding"])
        if revocation == "role":
            funding.allowed_roles = [role for role in funding.allowed_roles if role != "evaluation"]
        elif revocation == "data":
            funding.allowed_data_classes = []
        else:
            funding.allowed_source_ids = ["not-the-frozen-source"]
    with pytest.raises(DomainError) as error:
        app.assessments.persist_candidate(run, epoch, "authority-worker")
    assert error.value.code in {"EGRESS_OR_ROLE_DENIED", "FUNDING_SOURCE_SCOPE"}
    assert app.runtime.stage(identifier, "result") is None
    assert len([call for call in env["model"].calls if call["role"] == "evaluation"]) == 1
