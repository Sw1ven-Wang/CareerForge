"""Negative authority and immutable input tests, synthetic models only."""

import pytest
from sqlalchemy import select
from test_learning_graph_admission_m4 import direct

from careerforge.contracts_conversation_actions_v3 import (
    LearningDialogueMessageV1,
    TutorRequestInputV1,
)
from careerforge.errors import DomainError
from careerforge.models import AgentRun, Conversation, ModelReceipt
from careerforge.models_dialogue import (
    ConversationActionReceipt,
    WorkflowExecution,
    WorkflowSession,
    WorkflowTutorRequest,
)


@pytest.mark.asyncio
async def test_new_envelope_hash_conflicts_and_old_receipt_stays_exact(env):
    app, actor = env["app"], env["actor"]
    conv, value, msg, result = await direct(env, funding=False)
    for options in [
        TutorRequestInputV1(question="changed"),
        TutorRequestInputV1(question=value.tutor_request.question, model_call_cap=9),
    ]:
        changed = msg.model_copy(update={"tutor_request": options})
        with pytest.raises(DomainError) as exc:
            app.dialogue.dispatch(
                actor,
                conv,
                changed,
                forced_actions=[("start_learning_session", {})],
                learning_input=value,
            )
        assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    await app.drain()
    assert (
        app.dialogue.dispatch(
            actor, conv, msg, forced_actions=[("start_learning_session", {})], learning_input=value
        )
        == result
    )


@pytest.mark.asyncio
async def test_unknown_funding_rejected_before_new_receipt(env):
    app, actor = env["app"], env["actor"]
    conv, _, _, _ = await direct(env, funding=False)
    revision = app.conversations.detail(actor, conv)["revision"]
    with pytest.raises(DomainError) as error:
        app.dialogue.dispatch(
            actor,
            conv,
            LearningDialogueMessageV1(
                text="another",
                expected_focus_revision=revision,
                idempotency_key="foreign",
                authorization_ref="missing",
            ),
        )
    assert error.value.code == "NOT_FOUND"
    with app.db.read() as db:
        assert not db.scalar(
            select(ConversationActionReceipt).where(
                ConversationActionReceipt.conversation_id == conv,
                ConversationActionReceipt.idempotency_key == "foreign",
            )
        )
        assert (
            len(
                list(
                    db.scalars(
                        select(WorkflowTutorRequest).where(
                            WorkflowTutorRequest.conversation_id == conv
                        )
                    )
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_sealed_grant_revoked_before_dispatch_rejects_zero_run(env):
    app, actor = env["app"], env["actor"]
    _, _, _, result = await direct(env)
    app.runtime.revoke_funding(actor, env["funding"])
    await app.drain()
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, result["action_receipts"][0]["request_id"])
        assert request.status == "rejected" and request.failure_code == "FUNDING_UNAVAILABLE"
        assert request.turn_id is None and request.run_id is None
        assert request.authorization_hash


@pytest.mark.asyncio
async def test_origin_tampering_and_no_backfill(env):
    app = env["app"]
    _, _, _, result = await direct(env, funding=False)
    with app.db.transaction() as db:
        request = db.get(WorkflowTutorRequest, result["action_receipts"][0]["request_id"])
        row = db.get(WorkflowExecution, request.execution_id)
        row.execution_generation = 2
    await app.drain()
    with app.db.read() as db:
        row = db.get(WorkflowExecution, request.execution_id)
        assert row.status == "failed" and row.last_error_code == "LEARNING_ORIGIN"
        assert request.run_id is None


@pytest.mark.asyncio
async def test_unresolved_paid_outcome_blocks_followup_after_failure(env):
    app = env["app"]
    conv, _, _, result = await direct(env)
    # Execute just the unpaid owner; the child remains queued and no fixture call occurs.
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, result["action_receipts"][0]["request_id"])
    await app.execute_workflow(request.execution_id)
    with app.db.transaction() as db:
        request = db.get(WorkflowTutorRequest, request.id)
        run = db.get(AgentRun, request.run_id)
        run.status = "failed"
        db.add(
            ModelReceipt(
                run_id=run.id,
                stage_key="unknown-test",
                payer_id=env["actor"],
                profile_version=app.provider.profile_version,
                request_hash="a" * 64,
                role="tutor",
                status="unknown",
                epoch=run.epoch,
            )
        )
        workflow = db.get(WorkflowSession, request.workflow_id)
        app.learning_workflows.wake_in(db, workflow, kind="unknown-result")
    await app.execute_workflow(request.execution_id)
    with app.db.read() as db:
        row = db.get(WorkflowExecution, request.execution_id)
        assert row.wait_kind == "reconciliation"
        assert db.get(WorkflowTutorRequest, request.id).status == "dispatched"
    response = app.dialogue.dispatch(
        env["actor"],
        conv,
        LearningDialogueMessageV1(
            text="repeat",
            expected_focus_revision=app.conversations.detail(env["actor"], conv)["revision"],
            idempotency_key="unknown-repeat",
            authorization_ref=env["funding"],
        ),
    )
    assert response["action_receipts"][0]["failure_code"] == "LEARNING_REQUEST_PENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize("funded", [True, False])
async def test_origin_receipt_rejects_pending_question_and_hash_reseal(env, funded):
    from careerforge.services.learning_requests import receipt_in
    from careerforge.util import fingerprint

    app = env["app"]
    _, _, _, response = await direct(env, funding=funded)
    with app.db.transaction() as db:
        request = db.get(WorkflowTutorRequest, response["action_receipts"][0]["request_id"])
        request.request = {
            **request.request,
            "question": "forged replacement",
            "model_call_cap": 25,
        }
        request.request_hash = fingerprint(request.request)
        if request.authorization_hash:
            receipt = db.get(ConversationActionReceipt, request.authorization_receipt_id)
            request.authorization_hash = fingerprint(
                {
                    "request_id": request.id,
                    "request_hash": request.request_hash,
                    "funding_id": request.funding_id,
                    "receipt_id": receipt.id,
                    "action_index": request.authorization_action_index,
                    "receipt_request_hash": receipt.request_hash,
                }
            )
        with pytest.raises(DomainError) as error:
            receipt_in(db, request)
        assert error.value.code == "LEARNING_REQUEST_BINDING"
    calls = len(env["model"].calls)
    await app.drain()
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, request.id)
        assert request.run_id is None and request.turn_id is None
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
async def test_authorization_outcome_rejects_owned_grant_reseal(env):
    from careerforge.services.learning_requests import receipt_in
    from careerforge.util import fingerprint

    app = env["app"]
    _, _, _, response = await direct(env)
    replacement = app.runtime.create_funding(env["actor"], env["ns"], 100)
    with app.db.transaction() as db:
        request = db.get(WorkflowTutorRequest, response["action_receipts"][0]["request_id"])
        receipt = db.get(ConversationActionReceipt, request.authorization_receipt_id)
        request.funding_id = replacement
        request.authorization_hash = fingerprint(
            {
                "request_id": request.id,
                "request_hash": request.request_hash,
                "funding_id": replacement,
                "receipt_id": receipt.id,
                "action_index": request.authorization_action_index,
                "receipt_request_hash": receipt.request_hash,
            }
        )
        with pytest.raises(DomainError) as error:
            receipt_in(db, request, authorization=True)
        assert error.value.code == "LEARNING_REQUEST_BINDING"


@pytest.mark.asyncio
async def test_same_chat_module_selection_focus_fence_and_direct_busy(env):
    from careerforge.contracts_session_tutor_v166 import SessionTutorTurnInput

    app, actor = env["app"], env["actor"]
    conv, value, _, response = await direct(env)
    await app.drain()
    revision = app.conversations.detail(actor, conv)["revision"]
    with app.db.transaction() as db:
        row = db.get(Conversation, conv)
        row.focus = {
            **row.focus,
            "evidence_answer_state": {"old": True},
            "project_source_ids": ["old"],
        }
    message = LearningDialogueMessageV1(
        text="/module 1", expected_focus_revision=revision, idempotency_key="chat-select"
    )
    selected = app.dialogue.dispatch(actor, conv, message)
    assert selected["action_receipts"][0]["status"] == "applied", selected
    assert app.dialogue.dispatch(actor, conv, message) == selected
    detail = app.conversations.detail(actor, conv)
    assert detail["revision"] == revision + 1 and "evidence_answer_state" not in detail["focus"]
    assert "project_source_ids" not in detail["focus"]
    direct_turn = SessionTutorTurnInput(
        question="direct request",
        expected_revision=detail["revision"],
        funding_id=env["funding"],
        idempotency_key="direct-live",
        namespace_id=env["ns"],
        plan_id=value.plan_id,
        version_id=value.version_id,
        module_id=value.module_id,
        expected_plan_revision=value.expected_plan_revision,
    )
    app.conversations.submit_session(actor, conv, direct_turn)
    profile_before = app.learning.profile(actor)
    result = app.dialogue.dispatch(
        actor,
        conv,
        LearningDialogueMessageV1(
            text="/module 1",
            expected_focus_revision=app.conversations.detail(actor, conv)["revision"],
            idempotency_key="busy-select",
        ),
    )
    assert result["action_receipts"][0]["failure_code"] == "CONVERSATION_BUSY", result
    assert app.learning.profile(actor) == profile_before


@pytest.mark.asyncio
async def test_paid_completion_reconciles_old_request_after_plan_selection_drift(env, monkeypatch):
    from datetime import timedelta

    from test_learning_tutor_graph_m4 import delivered_crash

    from careerforge.db import clock
    from careerforge.models import LearningPlan

    app = env["app"]
    started, _ = await delivered_crash(env, monkeypatch)
    with app.db.transaction() as db:
        plan = db.get(LearningPlan, started["plan_id"])
        plan.revision += 1
        plan.active_version_id = None
        run = db.get(AgentRun, started["run_id"])
        run.lease_until = clock(db) - timedelta(seconds=1)
    before = len(env["model"].calls)
    await app.drain()
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, started["request_id"])
        run = db.get(AgentRun, request.run_id)
        execution = db.get(WorkflowExecution, request.execution_id)
        assert run.status == "succeeded"
        assert request.status == "completed", request.failure_code
        assert execution.status == "waiting" and execution.wait_kind == "selection", (
            execution.last_error_code
        )
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("funded", [True, False])
async def test_privacy_export_erases_pending_or_completed_learning_request(env, funded):
    from test_privacy_v16 import confirm

    app, actor = env["app"], env["actor"]
    _, _, _, result = await direct(env, funding=funded)
    await app.drain()
    request_id = result["action_receipts"][0]["request_id"]
    exported = app.privacy.export(actor)
    assert any(row["id"] == request_id for row in exported["records"]["workflow_tutor_requests"])
    await confirm(app.privacy, actor, app.privacy.propose(actor))
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, request_id)
        assert request.status == "cancelled" and request.request == {}
        assert request.materialized_input in (None, {})
        assert request.failure_code == "PRIVACY_ERASURE"
        execution = db.get(WorkflowExecution, request.execution_id)
        assert execution.status == "cancelled" and execution.binding_payload == {}


def repeat_current_admission(env, conv, original, *, key, funding=False):
    """New explicit request reads current server revisions, not an old CAS."""
    from careerforge.models import LearningPlan

    app = env["app"]
    with app.db.read() as db:
        plan = db.get(LearningPlan, original.plan_id)
        profile = app.learning.profile(env["actor"])
        value = original.model_copy(
            update={
                "version_id": plan.active_version_id,
                "expected_plan_revision": plan.revision,
                "expected_profile_revision": profile["revision"],
                "expected_focus_revision": db.get(Conversation, conv).revision,
                "idempotency_key": key,
                "authorization_ref": env["funding"] if funding else None,
            }
        )
    message = LearningDialogueMessageV1(
        text="start_learning_session",
        expected_focus_revision=value.expected_focus_revision,
        idempotency_key=key,
        authorization_ref=value.authorization_ref,
        tutor_request=value.tutor_request,
    )
    return app.dialogue.dispatch(
        env["actor"],
        conv,
        message,
        forced_actions=[("start_learning_session", {})],
        learning_input=value,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["same_version_reaccept", "new_version"])
@pytest.mark.parametrize("entry", ["http", "same_chat"])
async def test_module_selection_checks_actual_locked_plan_not_only_old_learning_binding(
    env, change, entry
):
    from test_dialogue_actions import api_client

    from careerforge.contracts_conversation_actions_v3 import ModuleSelectionV1
    from careerforge.models import PlanVersion

    app, actor = env["app"], env["actor"]
    conv, admitted, _, result = await direct(env)
    await app.drain()
    version_id, revision = admitted.version_id, admitted.expected_plan_revision
    if change == "new_version":
        with app.db.read() as db:
            ids = db.get(PlanVersion, version_id).module_ids
        revised = app.learning.revise_plan(
            actor, admitted.plan_id, revision, "explicit new version", ids
        )
        version_id, revision = revised["version_id"], revised["expected_revision"]
    app.learning.accept_plan(actor, admitted.plan_id, version_id, revision)
    before = app.learning.profile(actor)
    detail = app.conversations.detail(actor, conv)
    with app.db.read() as db:
        workflow = db.get(WorkflowSession, result["workflow_id"])
        before_workflow_revision = workflow.revision
    if entry == "http":
        value = ModuleSelectionV1(
            expected_revision=before_workflow_revision,
            idempotency_key="stale-module-http",
            plan_id=admitted.plan_id,
            version_id=admitted.version_id,
            module_id=admitted.module_id,
            expected_plan_revision=admitted.expected_plan_revision,
            expected_profile_revision=before["revision"],
            expected_focus_revision=detail["revision"],
        )
        async with api_client(env) as api:
            response = await api.post(
                f"/workflows/{workflow.id}/module-selection", json=value.model_dump(mode="json")
            )
            assert response.status_code == 202, response.text
            outcome = response.json()
    else:
        outcome = app.dialogue.dispatch(
            actor,
            conv,
            LearningDialogueMessageV1(
                text="/module 1",
                expected_focus_revision=detail["revision"],
                idempotency_key="stale-module-chat",
            ),
        )
    assert outcome["action_receipts"][0]["failure_code"] == "LEARNING_PLAN_CHANGED", outcome
    assert app.learning.profile(actor) == before
    assert app.conversations.detail(actor, conv)["focus"] == detail["focus"]
    with app.db.read() as db:
        assert db.get(WorkflowSession, workflow.id).revision == before_workflow_revision


@pytest.mark.asyncio
@pytest.mark.parametrize("new_version", [False, True])
async def test_explicit_reaccepted_snapshot_gets_fresh_owner_not_stale_canonical_request(
    env, new_version
):
    from careerforge.models import PlanVersion
    from careerforge.util import fingerprint

    app, actor = env["app"], env["actor"]
    conv, value, _, first = await direct(env)
    await app.drain()
    version_id, revision = value.version_id, value.expected_plan_revision
    if new_version:
        with app.db.read() as db:
            ids = db.get(PlanVersion, version_id).module_ids
        revised = app.learning.revise_plan(
            actor, value.plan_id, revision, "fresh accepted task", ids
        )
        version_id, revision = revised["version_id"], revised["expected_revision"]
    accepted = app.learning.accept_plan(actor, value.plan_id, version_id, revision)
    before = fingerprint(first)
    second = repeat_current_admission(env, conv, value, key="fresh-current-snapshot")
    assert second["action_receipts"][0]["status"] == "applied", second
    assert second["workflow_id"] != first["workflow_id"]
    with app.db.read() as db:
        original = db.get(WorkflowTutorRequest, first["action_receipts"][0]["request_id"])
        fresh = db.get(WorkflowTutorRequest, second["action_receipts"][0]["request_id"])
        assert original.request["accepted"]["plan_revision"] == value.expected_plan_revision
        assert fresh.request["accepted"]["plan_revision"] == accepted["revision"]
        assert fresh.request["accepted"]["version_id"] == version_id
        assert fresh.status == "pending_authorization" and fresh.run_id is None
        assert fingerprint(db.get(ConversationActionReceipt, first["receipt_id"]).result) == before
        assert db.get(WorkflowExecution, fresh.execution_id).predecessor_execution_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", ["cancelled", "failed"])
async def test_explicit_fresh_admission_after_safely_closed_direct_owner(env, closed):
    app, actor = env["app"], env["actor"]
    conv, value, _, first = await direct(env)
    await app.drain()
    if closed == "cancelled":
        result = app.dialogue.dispatch(
            actor,
            conv,
            LearningDialogueMessageV1(
                text="/cancel",
                expected_focus_revision=app.conversations.detail(actor, conv)["revision"],
                idempotency_key="close-old-learning",
            ),
        )
        assert result["action_receipts"][0]["status"] == "applied", result
    else:
        with app.db.transaction() as db:
            request = db.get(WorkflowTutorRequest, first["action_receipts"][0]["request_id"])
            assert request.status == "completed"
            execution = db.get(WorkflowExecution, request.execution_id)
            execution.status, execution.last_error_code = (
                "failed",
                "SYNTHETIC_FAILURE_AFTER_RECONCILIATION",
            )
    second = repeat_current_admission(env, conv, value, key="explicit-restart")
    assert second["action_receipts"][0]["status"] == "applied", second
    assert second["workflow_id"] != first["workflow_id"]


@pytest.mark.asyncio
async def test_cancelled_request_cannot_hide_unknown_paid_outcome_from_fresh_admission(env):
    app, actor = env["app"], env["actor"]
    conv, value, _, first = await direct(env)
    with app.db.read() as db:
        initial = db.get(WorkflowTutorRequest, first["action_receipts"][0]["request_id"])
    await app.execute_workflow(initial.execution_id)
    with app.db.transaction() as db:
        request = db.get(WorkflowTutorRequest, initial.id)
        run = db.get(AgentRun, request.run_id)
        run.status = "failed"
        db.add(
            ModelReceipt(
                run_id=run.id,
                stage_key="unknown-before-cancel",
                payer_id=actor,
                profile_version=app.provider.profile_version,
                request_hash="b" * 64,
                role="tutor",
                status="unknown",
                epoch=run.epoch,
            )
        )
    cancelled = app.dialogue.dispatch(
        actor,
        conv,
        LearningDialogueMessageV1(
            text="/cancel",
            expected_focus_revision=app.conversations.detail(actor, conv)["revision"],
            idempotency_key="cancel-unknown-old-task",
        ),
    )
    assert cancelled["action_receipts"][0]["status"] == "applied", cancelled
    fresh = repeat_current_admission(env, conv, value, key="cannot-restart-unknown")
    assert fresh["action_receipts"][0]["failure_code"] == "LEARNING_ORIGIN_PENDING", fresh
    with app.db.read() as db:
        assert len(list(db.scalars(select(WorkflowTutorRequest)))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["direct", "knowledge_handoff"])
async def test_resealed_learning_snapshot_cannot_borrow_initial_acceptance_receipt(env, origin):
    from copy import deepcopy

    from test_dialogue_actions import api_client, send
    from test_learning_graph_admission_m4 import enable
    from test_session_graph_m2 import prepare

    from careerforge.services.learning_binding import validate_persisted_owner_in
    from careerforge.util import fingerprint

    app, actor = env["app"], env["actor"]
    if origin == "direct":
        _, _, _, result = await direct(env)
    else:
        enable(app)
        async with api_client(env) as api:
            path, _, _ = await prepare(env, api)
            result = (await send(api, path, "接受学习计划")).json()
            assert result["action_receipts"][0]["status"] == "applied", result
    await app.drain()
    initial_id = result["action_receipts"][0]["request_id"]
    with app.db.read() as db:
        initial = db.get(WorkflowTutorRequest, initial_id)
        original = deepcopy(initial.request["accepted"])
        assert initial.status == "completed"
    # Real reacceptance changes current revision, but does not rewrite the old
    # request or permit its old L to borrow that later user decision.
    app.learning.accept_plan(
        actor, original["plan_id"], original["version_id"], original["plan_revision"]
    )
    calls = len(env["model"].calls)
    with app.db.transaction() as db:
        initial = db.get(WorkflowTutorRequest, initial_id)
        execution = db.get(WorkflowExecution, initial.execution_id)
        workflow = db.get(WorkflowSession, initial.workflow_id)
        forged = deepcopy(execution.binding_payload)
        forged["accepted"]["plan_revision"] += 1
        execution.binding_payload, execution.goal_binding_hash = forged, fingerprint(forged)
        assert initial.request["accepted"] == original
        with pytest.raises(DomainError) as validation:
            app.learning_workflows.validate_in(db, workflow, execution)
        assert validation.value.code == "LEARNING_ORIGIN"
        with pytest.raises(DomainError) as paid_common:
            validate_persisted_owner_in(db, workflow, execution)
        assert paid_common.value.code == "LEARNING_ORIGIN"
    assert len(env["model"].calls) == calls
