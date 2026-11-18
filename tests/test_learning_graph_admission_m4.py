"""Synthetic integration of real domain acceptance and registered learning graphs."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from test_session_tutor_binding_v166 import accepted

from careerforge.contracts_conversation_actions_v3 import (
    LearningDialogueMessageV1,
    LearningSessionAdmissionV1,
    TutorRequestAuthorizationV1,
    TutorRequestInputV1,
)
from careerforge.errors import DomainError
from careerforge.models import AgentRun, Conversation, FundingAgreement, RunEvent, Turn
from careerforge.models_dialogue import (
    ConversationActionReceipt,
    WorkflowExecution,
    WorkflowSession,
    WorkflowTutorRequest,
)
from careerforge.util import fingerprint


def enable(app):
    app.settings = replace(
        app.settings,
        workflow_engine="langgraph",
        session_graph_version="knowledge-session-v2",
        learning_graph_version="learning-session-v1",
    )
    app.db.settings = app.settings


async def direct(env, *, funding=True):
    app, actor = env["app"], env["actor"]
    conv, turn, plan = await accepted(env)
    enable(app)
    value = LearningSessionAdmissionV1(
        plan_id=turn.plan_id,
        version_id=turn.version_id,
        module_id=turn.module_id,
        expected_plan_revision=turn.expected_plan_revision,
        expected_profile_revision=app.learning.profile(actor)["revision"],
        expected_focus_revision=1,
        idempotency_key="m4-admit",
        authorization_ref=env["funding"] if funding else None,
        tutor_request=TutorRequestInputV1(question=turn.question),
    )
    message = LearningDialogueMessageV1(
        text="start_learning_session",
        expected_focus_revision=1,
        idempotency_key=value.idempotency_key,
        authorization_ref=value.authorization_ref,
        tutor_request=value.tutor_request,
    )
    result = app.dialogue.dispatch(
        actor, conv, message, forced_actions=[("start_learning_session", {})], learning_input=value
    )
    assert result["action_receipts"][0]["status"] == "applied", result
    return conv, value, message, result


@pytest.mark.asyncio
async def test_fresh_origin_actual_paid_graph_and_immutable_receipt(env):
    app, actor = env["app"], env["actor"]
    conv, value, message, result = await direct(env)
    original = fingerprint(result)
    request_id = result["action_receipts"][0]["request_id"]
    await app.drain()
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, request_id)
        execution = db.get(WorkflowExecution, request.execution_id)
        run = db.get(AgentRun, request.run_id)
        assert execution.execution_generation == 1 and execution.predecessor_execution_id is None
        assert execution.status == "waiting", execution.last_error_code
        assert run and run.graph_version == "learning-tutor-v1"
        assert run.status == "succeeded", app.runtime.stage(run.id, "result")
        assert request.status == "completed", request.failure_code
        assert db.get(Turn, request.turn_id).status == "completed"
        assert execution.wait_kind == "followup"
        assert (
            fingerprint(db.get(ConversationActionReceipt, result["receipt_id"]).result) == original
        )
    replay = app.dialogue.dispatch(
        actor, conv, message, forced_actions=[("start_learning_session", {})], learning_input=value
    )
    assert fingerprint(replay) == original


@pytest.mark.asyncio
async def test_unfunded_question_is_durable_no_turn_and_exact_authorization(env):
    app, actor = env["app"], env["actor"]
    conv, value, message, result = await direct(env, funding=False)
    request_id = result["action_receipts"][0]["request_id"]
    await app.drain()
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, request_id)
        workflow = db.get(WorkflowSession, request.workflow_id)
        assert (
            request.status == "pending_authorization" and not request.run_id and not request.turn_id
        )
        assert not db.scalar(select(Turn.id).where(Turn.conversation_id == conv))
        body = TutorRequestAuthorizationV1(
            expected_revision=workflow.revision,
            idempotency_key="m4-auth",
            request_id=request.id,
            request_hash=request.request_hash,
            authorization_ref=env["funding"],
        )
        rev = db.get(Conversation, conv).revision
    authorized = app.dialogue.dispatch(
        actor,
        conv,
        LearningDialogueMessageV1(
            text="authorize_tutor_request",
            expected_focus_revision=rev,
            idempotency_key=body.idempotency_key,
            authorization_ref=body.authorization_ref,
        ),
        forced_actions=[("authorize_tutor_request", {})],
        workflow_id=workflow.id,
        expected_workflow_revision=body.expected_revision,
        learning_input=body,
    )
    assert authorized["action_receipts"][0]["status"] == "applied", authorized
    await app.drain()
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, request_id)
        assert request.status == "completed", request.failure_code
        assert db.get(Turn, request.turn_id).question == value.tutor_request.question


@pytest.mark.asyncio
async def test_pending_blocks_all_central_submit_entrypoints(env):
    from careerforge.contracts import TurnInput
    from careerforge.contracts_session_tutor_v166 import GuidedTutorTurnInput

    app, actor = env["app"], env["actor"]
    conv, value, _, _ = await direct(env, funding=False)
    rev = app.conversations.detail(actor, conv)["revision"]
    with pytest.raises(DomainError) as error:
        app.conversations.submit(
            actor,
            conv,
            TurnInput(question="bypass", expected_revision=rev, idempotency_key="bypass"),
        )
    assert error.value.code == "LEARNING_REQUEST_PENDING"
    with pytest.raises(DomainError) as error:
        app.conversations.submit_guided(
            actor, conv, GuidedTutorTurnInput(expected_revision=rev, idempotency_key="guided")
        )
    assert error.value.code == "LEARNING_REQUEST_PENDING"


@pytest.mark.asyncio
async def test_knowledge_terminal_handoff_single_initial_and_repeat_acceptance(env):
    from test_dialogue_actions import api_client, send
    from test_session_graph_m2 import prepare

    app = env["app"]
    enable(app)
    async with api_client(env) as api:
        path, wid, kid = await prepare(env, api)
        result = (await send(api, path, "接受学习计划，讲解关键条件")).json()
        assert result["action_receipts"][0]["status"] == "applied", result
        rid = result["action_receipts"][0]["request_id"]
        assert result["action_receipts"][1]["request_id"] == rid
        await app.drain()
        with app.db.read() as db:
            workflow, knowledge = db.get(WorkflowSession, wid), db.get(WorkflowExecution, kid)
            learning = db.get(WorkflowExecution, workflow.current_execution_id)
            request = db.get(WorkflowTutorRequest, rid)
            assert knowledge.status == "completed" and knowledge.terminal_checkpoint_id
            assert learning.id != knowledge.id and learning.predecessor_execution_id == knowledge.id
            assert learning.execution_generation == 2 and learning.wait_kind == "followup", (
                learning.last_error_code
            )
            assert request.status == "completed", request.failure_code
        repeated = (await send(api, path, "接受学习计划，讲解关键条件")).json()
        assert repeated["action_receipts"][0]["status"] == "already_applied", repeated
        assert repeated["action_receipts"][0]["request_id"] == rid
        assert repeated["action_receipts"][1]["failure_code"] == "ACTION_DEPENDENCY"
        await app.drain()
        with app.db.read() as db:
            assert (
                len(
                    list(
                        db.scalars(
                            select(WorkflowTutorRequest).where(
                                WorkflowTutorRequest.workflow_id == wid
                            )
                        )
                    )
                )
                == 1
            )


@pytest.mark.asyncio
async def test_three_same_conversation_followups_and_module_selection(env):
    from careerforge.contracts_conversation_actions_v3 import ModuleSelectionV1

    app, actor = env["app"], env["actor"]
    conv, admission, _, result = await direct(env)
    wid = result["workflow_id"]
    await app.drain()
    for i in range(2):
        rev = app.conversations.detail(actor, conv)["revision"]
        follow = LearningDialogueMessageV1(
            text="该函数返回什么 value？",
            expected_focus_revision=rev,
            idempotency_key="follow-" + str(i),
            authorization_ref=env["funding"],
        )
        response = app.dialogue.dispatch(actor, conv, follow)
        assert response["action_receipts"][0]["status"] == "applied", response
        assert app.dialogue.dispatch(actor, conv, follow) == response
        await app.drain()
    with app.db.read() as db:
        requests = list(
            db.scalars(
                select(WorkflowTutorRequest)
                .where(WorkflowTutorRequest.workflow_id == wid)
                .order_by(WorkflowTutorRequest.sequence)
            )
        )
        assert [r.sequence for r in requests] == [1, 2, 3]
        assert len({r.execution_id for r in requests}) == 1
        assert all(r.status == "completed" for r in requests), [
            (
                r.status,
                r.failure_code,
                list(
                    db.scalars(
                        select(RunEvent.payload).where(
                            RunEvent.run_id == r.run_id, RunEvent.event_type == "run.finished"
                        )
                    )
                ),
            )
            for r in requests
        ]
        assert requests[2].predecessor_request_id == requests[1].id
        assert requests[2].predecessor_turn_id == requests[1].turn_id
        workflow = db.get(WorkflowSession, wid)
    revision = app.conversations.detail(actor, conv)["revision"]
    profile = app.learning.profile(actor)
    value = ModuleSelectionV1(
        expected_revision=workflow.revision,
        idempotency_key="select",
        plan_id=admission.plan_id,
        version_id=admission.version_id,
        module_id=admission.module_id,
        expected_plan_revision=admission.expected_plan_revision,
        expected_profile_revision=profile["revision"],
        expected_focus_revision=revision,
    )
    result = app.dialogue.dispatch(
        actor,
        conv,
        LearningDialogueMessageV1(
            text="select_learning_module",
            expected_focus_revision=revision,
            idempotency_key="select",
        ),
        forced_actions=[("select_learning_module", {})],
        learning_input=value,
    )
    assert result["action_receipts"][0]["status"] == "applied", result
    assert not result["action_receipts"][0]["module_completed"]
    assert app.learning.profile(actor)["revision"] == profile["revision"] + 1


@pytest.mark.asyncio
async def test_learning_pause_resume_cancel_pending_without_paid_run(env):
    from test_dialogue_actions import api_client, send

    app = env["app"]
    conv, _, _, result = await direct(env, funding=False)
    path = "/conversations/" + conv
    await app.drain()
    async with api_client(env) as api:
        paused = (await send(api, path, "/pause")).json()
        assert paused["action_receipts"][0]["status"] == "applied", paused
        await app.drain()
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, result["workflow_id"])
            assert workflow.control_state == "paused"
        resumed = (await send(api, path, "/resume")).json()
        assert resumed["action_receipts"][0]["status"] == "applied", resumed
        await app.drain()
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, result["workflow_id"])
            assert workflow.control_state == "active"
            assert (
                db.get(WorkflowExecution, workflow.current_execution_id).wait_kind
                == "authorization"
            )
        cancelled = (await send(api, path, "/cancel")).json()
        assert cancelled["action_receipts"][0]["status"] == "applied", cancelled
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, result["action_receipts"][0]["request_id"])
        assert request.status == "cancelled" and request.run_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("unusable", ["revoked", "exhausted", "role"])
async def test_owned_unusable_initial_grant_preserves_question_without_run(env, unusable):
    app, actor = env["app"], env["actor"]
    # Create actual accepted data before making only the Tutor grant unusable.
    conv, turn, _ = await accepted(env)
    enable(app)
    with app.db.transaction() as db:
        funding = db.get(FundingAgreement, env["funding"])
        if unusable == "revoked":
            funding.status = "revoked"
        elif unusable == "exhausted":
            funding.call_cap = funding.calls_consumed
        else:
            funding.allowed_roles = ["learning_planner"]
    body = LearningSessionAdmissionV1(
        plan_id=turn.plan_id,
        version_id=turn.version_id,
        module_id=turn.module_id,
        expected_plan_revision=turn.expected_plan_revision,
        expected_profile_revision=app.learning.profile(actor)["revision"],
        expected_focus_revision=1,
        idempotency_key="unusable",
        authorization_ref=env["funding"],
        tutor_request=TutorRequestInputV1(question="保留这个问题"),
    )
    msg = LearningDialogueMessageV1(
        text="start_learning_session",
        expected_focus_revision=1,
        idempotency_key=body.idempotency_key,
        authorization_ref=body.authorization_ref,
        tutor_request=body.tutor_request,
    )
    result = app.dialogue.dispatch(
        actor, conv, msg, forced_actions=[("start_learning_session", {})], learning_input=body
    )
    assert result["action_receipts"][0]["status"] == "applied", result
    await app.drain()
    with app.db.read() as db:
        row = db.get(WorkflowTutorRequest, result["action_receipts"][0]["request_id"])
        assert row.status == "pending_authorization" and not row.turn_id and not row.run_id
        assert row.request["question"] == "保留这个问题"
        assert row.request["requested_funding_id"] == env["funding"]


@pytest.mark.asyncio
async def test_acceptance_paused_before_knowledge_end_then_resume_handoffs_once(env):
    from test_dialogue_actions import api_client, send
    from test_session_graph_m2 import prepare

    app = env["app"]
    enable(app)
    async with api_client(env) as api:
        path, wid, kid = await prepare(env, api)
        accepted_result = (await send(api, path, "接受学习计划")).json()
        assert accepted_result["action_receipts"][0]["status"] == "applied"
        paused = (await send(api, path, "/pause")).json()
        assert paused["action_receipts"][0]["status"] == "applied", paused
        await app.drain()
        with app.db.read() as db:
            owner = db.get(WorkflowSession, wid)
            assert owner.control_state == "paused"
            assert owner.current_execution_id == kid
        resumed = (await send(api, path, "/resume")).json()
        assert resumed["action_receipts"][0]["status"] == "applied", resumed
        await app.drain()
        with app.db.read() as db:
            owner = db.get(WorkflowSession, wid)
            assert owner.current_execution_id != kid
            assert (
                len(
                    list(
                        db.scalars(
                            select(WorkflowExecution).where(
                                WorkflowExecution.predecessor_execution_id == kid
                            )
                        )
                    )
                )
                == 1
            )
            assert db.get(WorkflowExecution, owner.current_execution_id).wait_kind == "followup"


@pytest.mark.asyncio
async def test_same_chat_switch_and_teach_and_unknown_module_is_not_paid_followup(env):
    app, actor = env["app"], env["actor"]
    conv, _, _, _ = await direct(env)
    await app.drain()
    revision = app.conversations.detail(actor, conv)["revision"]
    calls = len(env["model"].calls)
    unknown = app.dialogue.dispatch(
        actor,
        conv,
        LearningDialogueMessageV1(
            text="/module 99",
            expected_focus_revision=revision,
            idempotency_key="unknown-module",
            authorization_ref=env["funding"],
        ),
    )
    assert unknown["action_receipts"][0]["failure_code"] == "LEARNING_MODULE_AMBIGUOUS"
    assert len(env["model"].calls) == calls
    result = app.dialogue.dispatch(
        actor,
        conv,
        LearningDialogueMessageV1(
            text="/module 1 --teach",
            expected_focus_revision=revision,
            idempotency_key="switch-teach",
            authorization_ref=env["funding"],
        ),
    )
    assert [a["status"] for a in result["action_receipts"]] == ["applied", "applied"], result
    await app.drain()
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, result["action_receipts"][1]["request_id"])
        assert request.status == "completed", request.failure_code


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["profile", "plan"])
async def test_accepted_request_selection_drift_before_real_knowledge_end_does_not_stick(
    env, changed
):
    from test_dialogue_actions import api_client, send
    from test_session_graph_m2 import prepare

    from careerforge.models import LearningPlan
    from careerforge.models_learning_ext import LearningProfile

    app = env["app"]
    enable(app)
    async with api_client(env) as api:
        path, wid, kid = await prepare(env, api)
        result = (await send(api, path, "接受学习计划")).json()
        assert result["action_receipts"][0]["status"] == "applied", result
        rid = result["action_receipts"][0]["request_id"]
        receipt_hash = fingerprint(result)
        with app.db.transaction() as db:
            request = db.get(WorkflowTutorRequest, rid)
            assert request.status == "pending_handoff" and request.execution_id is None
            original = request.request_hash
            if changed == "profile":
                profile = db.scalar(
                    select(LearningProfile).where(LearningProfile.principal_id == env["actor"])
                )
                profile.active_module_id = None
                profile.revision += 1
            else:
                plan = db.get(LearningPlan, request.request["accepted"]["plan_id"])
                plan.active_version_id = None
                plan.revision += 1
        await app.drain()
        with app.db.read() as db:
            request = db.get(WorkflowTutorRequest, rid)
            workflow = db.get(WorkflowSession, wid)
            knowledge = db.get(WorkflowExecution, kid)
            learning = db.get(WorkflowExecution, workflow.current_execution_id)
            assert knowledge.status == "completed" and knowledge.terminal_checkpoint_id
            assert learning.predecessor_execution_id == kid and learning.id != kid
            assert learning.status == "waiting" and learning.wait_kind == "selection"
            assert request.execution_id == learning.id and request.status == "rejected"
            assert request.failure_code in {
                "LEARNING_SELECTION_CHANGED",
                "LEARNING_PLAN_CHANGED",
                "SESSION_TUTOR_INACTIVE",
            }
            assert request.run_id is None and request.turn_id is None
            assert request.request_hash == original
            assert not app.learning_requests.pending_in(db, workflow.conversation_id)
            assert (
                fingerprint(db.get(ConversationActionReceipt, result["receipt_id"]).result)
                == receipt_hash
            )
