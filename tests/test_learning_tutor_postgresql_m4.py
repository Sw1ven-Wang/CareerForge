"""Real PG row-lock barriers for witnessed delivery; synthetic models only.

Opt-in disposable domain DB via pg_runtime. These tests do not stand in for the
separate persistent-saver/Celery gate or native user acceptance.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

import pytest
from sqlalchemy import event
from test_learning_graph_postgresql_m4 import domain_env
from test_learning_tutor_graph_m4 import delivered_crash
from test_runtime_postgresql import pg_runtime  # noqa: F401

from careerforge.contracts import TurnInput
from careerforge.contracts_answer_completion_v166 import ANSWER_COMPLETION_STAGE
from careerforge.contracts_session_progress_v166 import SessionModuleAckInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, Conversation, FundingAgreement
from careerforge.services import runtime, session_turn
from careerforge.services.session_turn import SessionTurnStages

pytestmark = pytest.mark.production


def pending_ack(env, started):
    app, actor = env["app"], env["actor"]
    binding = app.conversations.session_binding(actor, started["turn_id"])
    module = binding["module"]
    assessment = app.runtime.stage(started["run_id"], ANSWER_COMPLETION_STAGE)
    # The actual ACK must stop on the still-running Run BEFORE evaluating the
    # delivery hash. No success/terminal Run or ACK is manufactured for this race.
    return SessionModuleAckInput(
        learning_session_id=binding["learning_session_id"],
        turn_id=started["turn_id"],
        namespace_id=env["ns"],
        plan_id=module["plan_id"],
        version_id=module["version_id"],
        module_id=module["module_id"],
        expected_plan_revision=module["plan_revision"],
        expected_profile_revision=app.learning.profile(actor)["revision"],
        binding_snapshot_hash=binding["snapshot_hash"],
        completion_assessment_hash=assessment["assessment_hash"],
        delivery_hash="f" * 64,
        confirm="ACKNOWLEDGE_MODULE_AND_ADVANCE",
        accept_incomplete_material=True,
        idempotency_key="pg-pending-ack",
    )


async def test_pg_real_ack_and_finalizer_have_no_mutable_selection_lock_cycle(
    pg_runtime,  # noqa: F811
    monkeypatch,
):
    env = domain_env(pg_runtime)
    app = env["app"]
    started, epoch = await delivered_crash(env, monkeypatch)
    value = pending_ack(env, started)
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
    funding_held, ack_funding_attempt = Event(), Event()
    original_run_lock = runtime.locked
    observed_locks = []

    def run_lock(db, cls, identifier):
        row = original_run_lock(db, cls, identifier)
        if (
            current_thread().name == "m4-finalizer"
            and cls is FundingAgreement
            and not funding_held.is_set()
        ):
            funding_held.set()
            assert ack_funding_attempt.wait(4), "ACK did not reach its funding fence"
        return row

    def sql_observer(connection, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        locking = "for update" in sql or "for share" in sql
        if current_thread().name == "m4-ack" and locking and "funding_agreements" in sql:
            ack_funding_attempt.set()
        if current_thread().name == "m4-finalizer" and locking:
            observed_locks.append(sql)

    monkeypatch.setattr(runtime, "locked", run_lock)
    event.listen(app.db.engine, "before_cursor_execute", sql_observer)
    calls = len(env["model"].calls)

    def finalize():
        current_thread().name = "m4-finalizer"
        SessionTurnStages(app, run, epoch, "crash-worker").finalize()

    def acknowledge():
        current_thread().name = "m4-ack"
        assert funding_held.wait(4)
        with pytest.raises(DomainError) as result:
            app.conversations.acknowledge_module(env["actor"], value)
        assert result.value.code == "SESSION_DELIVERY_PENDING"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(finalize), pool.submit(acknowledge)]
            for future in futures:
                future.result(timeout=15)
    finally:
        event.remove(app.db.engine, "before_cursor_execute", sql_observer)
    assert observed_locks
    assert not any(
        table in sql
        for sql in observed_locks
        for table in ("learning_plans", "learning_profiles", "conversation_turns")
    ), "Committed-only finalization must never lock mutable selection/Turn rows"
    assert len(env["model"].calls) == calls
    assert app.runtime.finish(run.id, epoch, "crash-worker", "succeeded")


async def test_pg_finalizer_locks_conversation_before_funding_against_real_admission(
    pg_runtime,  # noqa: F811
    monkeypatch,
):
    env = domain_env(pg_runtime)
    app = env["app"]
    started, epoch = await delivered_crash(env, monkeypatch)
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
        revision = db.get(Conversation, started["conversation_id"]).revision
    admission_holds_conversation, finalizer_at_conversation = Event(), Event()
    original_owned, original_final_lock, original_run_lock = (
        app.conversations._owned,
        session_turn.locked,
        runtime.locked,
    )
    lock_order = []

    def owned(db, actor, identifier, lock=False):
        row = original_owned(db, actor, identifier, lock=lock)
        if current_thread().name == "m4-admission" and lock:
            admission_holds_conversation.set()
            assert finalizer_at_conversation.wait(4)
        return row

    def final_lock(db, cls, identifier):
        if current_thread().name == "m4-finalizer" and cls is Conversation:
            lock_order.append("conversation")
            finalizer_at_conversation.set()
        return original_final_lock(db, cls, identifier)

    def run_lock(db, cls, identifier):
        if current_thread().name == "m4-finalizer" and cls is FundingAgreement:
            lock_order.append("funding")
        return original_run_lock(db, cls, identifier)

    monkeypatch.setattr(app.conversations, "_owned", owned)
    monkeypatch.setattr(session_turn, "locked", final_lock)
    monkeypatch.setattr(runtime, "locked", run_lock)

    def admit():
        current_thread().name = "m4-admission"
        with pytest.raises(DomainError) as result:
            app.conversations.submit(
                env["actor"],
                started["conversation_id"],
                TurnInput(
                    question="cannot overtake",
                    expected_revision=revision,
                    funding_id=env["funding"],
                    idempotency_key="pg-concurrent-admission",
                ),
            )
        assert result.value.code == "LEARNING_REQUEST_PENDING"

    def finalize():
        current_thread().name = "m4-finalizer"
        assert admission_holds_conversation.wait(4)
        SessionTurnStages(app, run, epoch, "crash-worker").finalize()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(admit), pool.submit(finalize)]
        for future in futures:
            future.result(timeout=15)
    assert lock_order[0] == "conversation" and "funding" in lock_order
    assert app.runtime.finish(run.id, epoch, "crash-worker", "succeeded")


def crossconversation_admission(env, started):
    from careerforge.contracts_conversation_actions_v3 import (
        LearningDialogueMessageV1,
        LearningSessionAdmissionV1,
        TutorRequestInputV1,
    )

    app, actor = env["app"], env["actor"]
    binding = app.conversations.session_binding(actor, started["turn_id"])
    module = binding["module"]
    conv = app.conversations.create(actor)
    value = LearningSessionAdmissionV1(
        plan_id=module["plan_id"],
        version_id=module["version_id"],
        module_id=module["module_id"],
        expected_plan_revision=module["plan_revision"],
        expected_profile_revision=app.learning.profile(actor)["revision"],
        expected_focus_revision=1,
        idempotency_key="pg-cross-conversation-admission",
        authorization_ref=env["funding"],
        tutor_request=TutorRequestInputV1(question="该函数返回什么 value？"),
    )
    message = LearningDialogueMessageV1(
        text="start_learning_session",
        expected_focus_revision=1,
        idempotency_key=value.idempotency_key,
        authorization_ref=value.authorization_ref,
        tutor_request=value.tutor_request,
    )
    return conv, value, message


def dispatch_crossconversation(env, prepared):
    conv, value, message = prepared
    result = env["app"].dialogue.dispatch(
        env["actor"],
        conv,
        message,
        forced_actions=[("start_learning_session", {})],
        learning_input=value,
    )
    assert result["action_receipts"][0]["status"] == "applied", result
    return result


@pytest.mark.parametrize("operation", ["admission", "pause"])
async def test_pg_live_paid_plan_locks_vs_real_same_grant_admission_or_control(
    pg_runtime,  # noqa: F811
    monkeypatch,
    operation,
):
    from test_learning_tutor_graph_m4 import admitted

    from careerforge.contracts_conversation_actions_v3 import LearningDialogueMessageV1
    from careerforge.models_dialogue import WorkflowTutorRequest

    env = domain_env(pg_runtime)
    app = env["app"]
    started = await admitted(env)
    epoch = app.runtime.claim(started["run_id"], "pg-live-worker")
    prepared = crossconversation_admission(env, started) if operation == "admission" else None
    message = LearningDialogueMessageV1(
        text="/pause",
        expected_focus_revision=app.conversations.detail(env["actor"], started["conversation_id"])[
            "revision"
        ],
        idempotency_key="pg-live-pause",
        authorization_ref=env["funding"],
    )
    funding_held, contender_attempts_funding = Event(), Event()
    original_lock = runtime.locked
    contender_lock_sql = []

    def run_lock(db, cls, identifier):
        row = original_lock(db, cls, identifier)
        if (
            current_thread().name == "m4-live-paid"
            and cls is FundingAgreement
            and not funding_held.is_set()
        ):
            funding_held.set()
            assert contender_attempts_funding.wait(4), (
                "Contender did not reach exact funding before live Plan/Profile checks"
            )
        return row

    def observer(connection, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        if current_thread().name == "m4-contender" and ("for update" in sql or "for share" in sql):
            contender_lock_sql.append(sql)
            if "funding_agreements" in sql:
                contender_attempts_funding.set()

    monkeypatch.setattr(runtime, "locked", run_lock)
    event.listen(app.db.engine, "before_cursor_execute", observer)
    calls = len(env["model"].calls)

    def validate_paid():
        current_thread().name = "m4-live-paid"
        app.runtime.validate(started["run_id"], epoch, "pg-live-worker")

    def contend():
        current_thread().name = "m4-contender"
        assert funding_held.wait(4)
        if operation == "admission":
            return dispatch_crossconversation(env, prepared)
        result = app.dialogue.dispatch(env["actor"], started["conversation_id"], message)
        assert result["action_receipts"][0]["status"] == "applied", result
        assert result["action_receipts"][0]["control_state"] == "pause_requested"
        return result

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            paid, contender = pool.submit(validate_paid), pool.submit(contend)
            paid.result(timeout=15)
            response = contender.result(timeout=15)
    finally:
        event.remove(app.db.engine, "before_cursor_execute", observer)
    assert len(env["model"].calls) == calls
    funding_index = next(
        i for i, sql in enumerate(contender_lock_sql) if "funding_agreements" in sql
    )
    for i, sql in enumerate(contender_lock_sql):
        if "learning_plans" in sql or "learning_profiles" in sql:
            assert funding_index < i
    with app.db.read() as db:
        old = db.get(AgentRun, started["run_id"])
        assert old.calls_consumed == 0
        if operation == "admission":
            request = db.get(WorkflowTutorRequest, response["action_receipts"][0]["request_id"])
            assert request.conversation_id != started["conversation_id"]
            assert request.request["accepted"]["plan_id"] == started["plan_id"]
            assert request.funding_id == old.funding_id == env["funding"]
            assert request.status == "ready" and request.run_id is None
        else:
            assert old.pause_requested


async def test_pg_crossconversation_same_plan_grant_admission_vs_real_ack_rejects_stale_request(
    pg_runtime,  # noqa: F811
    monkeypatch,
):
    from test_learning_tutor_graph_m4 import admitted
    from test_session_module_progress_v166 import ack_input

    from careerforge.models_dialogue import WorkflowTutorRequest
    from careerforge.services import learning_execution

    env = domain_env(pg_runtime)
    app, actor = env["app"], env["actor"]
    started = await admitted(env)
    await app.execute(started["run_id"])
    await app.drain()
    binding = app.conversations.session_binding(actor, started["turn_id"])
    detail = app.conversations.teaching_session(actor, binding["learning_session_id"])
    value = ack_input(env, detail, key="pg-cross-conversation-ack")
    prepared = crossconversation_admission(env, started)
    funding_held, ack_attempts_funding = Event(), Event()
    original_lock = learning_execution.locked

    def admission_lock(db, cls, identifier):
        row = original_lock(db, cls, identifier)
        if (
            current_thread().name == "m4-cross-admission"
            and cls is FundingAgreement
            and not funding_held.is_set()
        ):
            funding_held.set()
            assert ack_attempts_funding.wait(4), (
                "ACK did not lock its original funding before Plan/Profile"
            )
        return row

    def observer(connection, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        if (
            current_thread().name == "m4-cross-ack"
            and "funding_agreements" in sql
            and ("for update" in sql or "for share" in sql)
        ):
            ack_attempts_funding.set()

    monkeypatch.setattr(learning_execution, "locked", admission_lock)
    event.listen(app.db.engine, "before_cursor_execute", observer)
    calls = len(env["model"].calls)

    def admit():
        current_thread().name = "m4-cross-admission"
        return dispatch_crossconversation(env, prepared)

    def acknowledge():
        current_thread().name = "m4-cross-ack"
        assert funding_held.wait(4)
        return app.conversations.acknowledge_module(actor, value)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission, acknowledgement = pool.submit(admit), pool.submit(acknowledge)
            response = admission.result(timeout=15)
            ack = acknowledgement.result(timeout=15)
    finally:
        event.remove(app.db.engine, "before_cursor_execute", observer)
    assert ack["module_completed"] is True and ack["competence_verified"] is False
    request_id = response["action_receipts"][0]["request_id"]
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, request_id)
        assert request.status == "ready" and request.run_id is None
        request_hash, module_id = request.request_hash, request.request["module_id"]
        assert (
            request.funding_id == env["funding"]
            and request.request["accepted"]["plan_id"] == started["plan_id"]
        )
    await app.drain()
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, request_id)
        assert request.status == "rejected" and request.failure_code == "SESSION_TUTOR_INACTIVE"
        assert request.run_id is None and request.turn_id is None
        assert (request.request_hash, request.request["module_id"]) == (request_hash, module_id)
    assert len(env["model"].calls) == calls
