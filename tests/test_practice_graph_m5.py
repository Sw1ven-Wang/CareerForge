"""Engineering fixtures only: real domain transactions and graphs, synthetic provider."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from test_session_tutor_binding_v166 import accepted

from careerforge.contracts_conversation_actions_v3 import (
    LearningDialogueMessageV1,
    LearningSessionAdmissionV1,
    TutorRequestInputV1,
)
from careerforge.contracts_practice_dialogue_v1 import PracticeDialogueMessageV1
from careerforge.models import AgentRun, Conversation, Idempotency
from careerforge.models_dialogue import (
    WorkflowAssessmentRequest,
    WorkflowExecution,
    WorkflowPracticeRequest,
    WorkflowSession,
)
from careerforge.models_learning_ext import LearningAttempt
from careerforge.util import fingerprint


async def session(env):
    app, actor = env["app"], env["actor"]
    conv, turn, _ = await accepted(env)
    app.settings = replace(
        app.settings,
        workflow_engine="langgraph",
        session_graph_version="knowledge-session-v2",
        learning_graph_version="learning-session-practice-v1",
    )
    app.db.settings = app.settings
    value = LearningSessionAdmissionV1(
        plan_id=turn.plan_id,
        version_id=turn.version_id,
        module_id=turn.module_id,
        expected_plan_revision=turn.expected_plan_revision,
        expected_profile_revision=app.learning.profile(actor)["revision"],
        expected_focus_revision=1,
        idempotency_key="m5-admit",
        authorization_ref=env["funding"],
        tutor_request=TutorRequestInputV1(question=turn.question),
    )
    msg = LearningDialogueMessageV1(
        text="start_learning_session",
        expected_focus_revision=1,
        idempotency_key=value.idempotency_key,
        authorization_ref=value.authorization_ref,
        tutor_request=value.tutor_request,
    )
    result = app.dialogue.dispatch(
        actor, conv, msg, forced_actions=[("start_learning_session", {})], learning_input=value
    )
    assert result["action_receipts"][0]["status"] == "applied", result
    await app.drain()
    with app.db.read() as db:
        workflow = db.get(WorkflowSession, result["workflow_id"])
        execution = db.get(WorkflowExecution, workflow.current_execution_id)
        assert execution.graph_version == "learning-session-practice-v1"
        assert execution.status == "waiting", execution.last_error_code
    return conv, workflow.id


def command(env, conv, wid, intent, key, *, funding=None):
    app = env["app"]
    with app.db.read() as db:
        workflow = db.get(WorkflowSession, wid)
        conversation = db.get(Conversation, conv)
        msg = PracticeDialogueMessageV1(
            contract="practice-dialogue-v1",
            text=intent["type"],
            expected_focus_revision=conversation.revision,
            idempotency_key=key,
            workflow_id=wid,
            execution_id=workflow.current_execution_id,
            expected_workflow_revision=workflow.revision,
            authorization_ref=funding,
            intent=intent,
        )
    result = app.dialogue.dispatch(env["actor"], conv, msg)
    return result, msg


async def start(env, conv, wid, mode="assisted"):
    app = env["app"]
    with app.db.read() as db:
        workflow = db.get(WorkflowSession, wid)
        execution = db.get(WorkflowExecution, workflow.current_execution_id)
        profile = app.learning.profile(env["actor"])
        module = profile["active_module_id"]
        intent = dict(
            type="start_practice",
            activity=dict(module_id=module, kind="understanding_check"),
            assistance_mode=mode,
            expected_profile_revision=profile["revision"],
            module_hash=execution.binding_payload["accepted"]["module_hashes"][module],
        )
    result, msg = command(env, conv, wid, intent, "m5-practice")
    assert result["action_receipts"][0]["status"] == "applied", result
    await app.drain()
    p = app.practice_workflows.detail(
        env["actor"], conv, result["action_receipts"][0]["practice_id"]
    )
    assert p["practice_status"] == "waiting_answer", p
    return p, msg, result


def target(p):
    return dict(
        practice_id=p["practice_id"],
        practice_hash=p["request_hash"],
        attempt_id=p["attempt_id"],
        specification_hash=p["specification_hash"],
    )


@pytest.mark.asyncio
async def test_same_conversation_graph_submission_and_assessment(env):
    from careerforge.contracts import ModelOutput

    env["model"].overrides["evaluation"] = lambda request: ModelOutput(
        structured={
            "rubric_items": [
                {
                    "rubric_item_id": item["id"],
                    "outcome": "unknown",
                    "submission_quote": "",
                    "fact_atom_ids": [],
                    "explanation": "工程候选，未知。",
                }
                for item in request["context"]["rubric"]
            ],
            "limitations": ["synthetic engineering only"],
        },
        synthetic=True,
    )
    app = env["app"]
    conv, wid = await session(env)
    p, msg, created = await start(env, conv, wid)
    original = fingerprint(created)
    assert fingerprint(app.dialogue.dispatch(env["actor"], conv, msg)) == original
    text = "  原始答案\n边界条件、来源与反例。\r\n"
    result, submission = command(
        env,
        conv,
        wid,
        dict(type="submit_practice", **target(p), submission=dict(response=text)),
        "m5-submit",
    )
    assert result["action_receipts"][0]["status"] == "applied", result
    assert app.dialogue.dispatch(env["actor"], conv, submission) == result
    await app.drain()
    p = app.practice_workflows.detail(env["actor"], conv, p["practice_id"])
    with app.db.read() as db:
        attempt = db.get(LearningAttempt, p["attempt_id"])
        assert attempt.response == text
        assert len(list(db.scalars(select(WorkflowPracticeRequest)))) == 1
        assert (
            len(
                list(
                    db.scalars(
                        select(Idempotency).where(
                            Idempotency.operation == "workflow.practice.create"
                        )
                    )
                )
            )
            == 1
        )
    result, _ = command(
        env,
        conv,
        wid,
        dict(type="request_assessment", **target(p), submission_hash=p["submission_hash"]),
        "m5-assess",
        funding=env["funding"],
    )
    assert result["action_receipts"][0]["status"] == "applied", result
    await app.drain()
    p = app.practice_workflows.detail(env["actor"], conv, p["practice_id"])
    with app.db.read() as db:
        a = db.get(WorkflowAssessmentRequest, p["assessment"]["request_id"])
        run = db.get(AgentRun, a.run_id)
        assert a.status == "completed", (a.status, a.failure_code, run.status if run else None)
        assert (
            run.workflow == "learning_assessment" and run.graph_version == "learning-assessment-v1"
        )
        assert run.call_cap == 1
    assert p["assessment_candidate"]["competence_verified"] is False
    assert p["competence_verified"] is False
