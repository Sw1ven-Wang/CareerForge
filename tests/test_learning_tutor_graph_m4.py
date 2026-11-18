"""Synthetic M4 paid-stage and no-paid delivery recovery contracts, not native acceptance."""

from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from careerforge.contracts import TurnInput
from careerforge.db import clock
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    Conversation,
    ExternalRequest,
    LearningPlan,
    StageArtifact,
    StreamEvent,
    Turn,
)
from careerforge.models_dialogue import WorkflowTutorRequest
from careerforge.models_learning_ext import LearningProfile
from careerforge.services.session_turn import DELIVERY_STAGE, FINALIZED_STAGE, SessionTurnStages
from careerforge.workflows.runtime_adapter import GraphRuntimeAdapter


async def admitted(env, *, dispatch=True, funding=True):
    """Real accepted-plan, indexed action receipt, unpaid owner and paid admission."""
    from test_learning_graph_admission_m4 import direct

    app = env["app"]
    conv_id, value, _, result = await direct(env, funding=funding)
    request_id = result["action_receipts"][0]["request_id"]
    with app.db.read() as db:
        execution_id = db.get(WorkflowTutorRequest, request_id).execution_id
    if dispatch:
        await app.execute_workflow(execution_id)
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, request_id)
        if dispatch:
            assert request.run_id, request.failure_code
        return {
            "request_id": request.id,
            "run_id": request.run_id,
            "turn_id": request.turn_id,
            "conversation_id": conv_id,
            "plan_id": value.plan_id,
        }


@pytest.mark.asyncio
async def test_real_paid_graph_stages_and_no_ordinary_router(env, monkeypatch):
    app = env["app"]
    started = await admitted(env)

    async def forbidden(*args, **kwargs):
        raise AssertionError("ordinary router/retrieval must not execute in accepted-module graph")

    monkeypatch.setattr(app.conversations, "process", forbidden)
    monkeypatch.setattr(app.retrieval, "retrieve", forbidden)
    await app.execute(started["run_id"])
    with app.db.read() as db:
        run, turn = db.get(AgentRun, started["run_id"]), db.get(Turn, started["turn_id"])
        assert run.status == "succeeded", app.runtime.stage(run.id, "result")
        assert run.graph_version == "learning-tutor-v1" and turn.status == "completed"
        assert app.runtime.stage(run.id, DELIVERY_STAGE)
        assert app.runtime.stage(run.id, FINALIZED_STAGE)
    async with app.graph_runtime.checkpointers.open() as saver:
        graph = app.graph_runtime.graph(saver, run, run.epoch, "irrelevant")
        snapshot = await graph.aget_state({"configurable": {"thread_id": run.id}})
        assert not snapshot.next and snapshot.values["current_stage"] == "completed"
        assert set(graph.get_graph().nodes) >= {
            "validate_turn_and_load_module",
            "retrieve_reviewed_evidence",
            "tutor_checked_stream",
            "answer_completion_guard",
            "commit_answer",
            "finalize_turn_result",
        }
    assert not app.runtime.stage(run.id, "orchestration")
    from careerforge.contracts_session_tutor_v166 import SessionTutorTurnInput

    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, started["request_id"])
        materialized = SessionTutorTurnInput.model_validate(request.materialized_input)
    with pytest.raises(DomainError) as conflict:
        app.conversations._submit(
            env["actor"],
            started["conversation_id"],
            materialized,
            session_value=materialized,
            learning_request_id="another-request",
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


async def delivered_crash(env, monkeypatch, *, started=None):
    app = env["app"]
    started = started or await admitted(env)
    run_id = started["run_id"]
    epoch = app.runtime.claim(run_id, "crash-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
    adapter = app.graph_runtime = GraphRuntimeAdapter(app)
    original = SessionTurnStages.deliver

    def crash(self):
        original(self)
        raise RuntimeError("crash after committed delivery before checkpoint")

    with monkeypatch.context() as patch:
        patch.setattr(SessionTurnStages, "deliver", crash)
        with pytest.raises(RuntimeError, match="before checkpoint"):
            await adapter.execute(run, epoch, "crash-worker")
    assert app.runtime.stage(run_id, DELIVERY_STAGE)
    assert not app.runtime.stage(run_id, FINALIZED_STAGE)
    return started, epoch


@pytest.mark.asyncio
async def test_stale_checkpoint_recovers_after_selection_drift_without_calls(env, monkeypatch):
    app = env["app"]
    started, epoch = await delivered_crash(env, monkeypatch)
    calls = list(env["model"].calls)
    with app.db.transaction() as db:
        profile = db.scalar(
            select(LearningProfile).where(LearningProfile.principal_id == env["actor"])
        )
        profile.active_plan_id = profile.active_module_id = None
        profile.revision += 1
        plan = db.get(LearningPlan, started["plan_id"])
        plan.active_version_id = None
        plan.revision += 1
        conv = db.get(Conversation, started["conversation_id"])
        conv.revision += 1
        conv.focus = {"newer_user_focus": True}
        # Atomic delivery normally already includes result. Recovery must still
        # reconstruct it from exact witnessed artifacts, not call a model.
        db.execute(
            delete(StageArtifact).where(
                StageArtifact.run_id == started["run_id"], StageArtifact.stage_key == "result"
            )
        )
        run = db.get(AgentRun, started["run_id"])
        run.lease_until = clock(db) - timedelta(seconds=1)
        events = [
            (r.id, r.seq, r.payload)
            for r in db.scalars(
                select(StreamEvent).where(StreamEvent.turn_id == started["turn_id"])
            )
        ]
    app.runtime.reap()
    await app.execute(started["run_id"])
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
        assert run.status == "succeeded", app.runtime.stage(run.id, "result")
        assert db.get(Conversation, started["conversation_id"]).focus == {"newer_user_focus": True}
        assert events == [
            (r.id, r.seq, r.payload)
            for r in db.scalars(
                select(StreamEvent).where(StreamEvent.turn_id == started["turn_id"])
            )
        ]
    assert env["model"].calls == calls
    assert app.runtime.stage(run.id, FINALIZED_STAGE)
    async with app.graph_runtime.checkpointers.open() as saver:
        snapshot = await app.graph_runtime.graph(saver, run, run.epoch, "unused").aget_state(
            {"configurable": {"thread_id": run.id}}
        )
        assert not snapshot.next and snapshot.values["current_stage"] == "completed"


@pytest.mark.asyncio
async def test_same_worker_heartbeat_finish_use_witness_and_paid_gateway_stays_closed(
    env, monkeypatch
):
    app = env["app"]
    started, epoch = await delivered_crash(env, monkeypatch)
    with app.db.transaction() as db:
        profile = db.scalar(
            select(LearningProfile).where(LearningProfile.principal_id == env["actor"])
        )
        profile.active_module_id = None
        profile.revision += 1
    with pytest.raises(DomainError) as error:
        app.runtime.validate(started["run_id"], epoch, "crash-worker")
    assert error.value.code == "FINALIZATION_ONLY"
    calls = len(env["model"].calls)
    app.runtime.heartbeat(started["run_id"], epoch, "crash-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
    await app.graph_runtime.execute(run, epoch, "crash-worker")
    assert app.runtime.finish(run.id, epoch, "crash-worker", "succeeded")
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["unknown", "witness", "block", "profile", "missing_witness", "funding", "control"],
)
async def test_delivery_capability_remains_fail_closed(env, monkeypatch, corruption):
    app = env["app"]
    started, epoch = await delivered_crash(env, monkeypatch)
    with app.db.transaction() as db:
        if corruption == "unknown":
            db.add(
                ExternalRequest(
                    run_id=started["run_id"],
                    operation="fixture",
                    request_hash="f" * 64,
                    status="unknown",
                )
            )
        elif corruption == "witness":
            stage = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == started["run_id"],
                    StageArtifact.stage_key == DELIVERY_STAGE,
                )
            )
            stage.payload = {**stage.payload, "binding_hash": "f" * 64}
        elif corruption == "block":
            from careerforge.models import AnswerBlock

            row = db.scalar(select(AnswerBlock).where(AnswerBlock.turn_id == started["turn_id"]))
            row.content = {**row.content, "text": "tampered"}
        elif corruption == "missing_witness":
            db.execute(
                delete(StageArtifact).where(
                    StageArtifact.run_id == started["run_id"],
                    StageArtifact.stage_key == DELIVERY_STAGE,
                )
            )
        elif corruption == "funding":
            from careerforge.models import FundingAgreement

            db.get(FundingAgreement, env["funding"]).status = "revoked"
        elif corruption == "control":
            from careerforge.models_dialogue import WorkflowSession

            request = db.get(WorkflowTutorRequest, started["request_id"])
            db.get(WorkflowSession, request.workflow_id).control_state = "paused"
        else:
            app.runtime.current_model_profile = lambda: "changed-profile"
    before = len(env["model"].calls)
    with pytest.raises(DomainError):
        app.runtime.validate_execution(started["run_id"], epoch, "crash-worker")
    assert len(env["model"].calls) == before
    assert not app.runtime.stage(started["run_id"], FINALIZED_STAGE)


@pytest.mark.asyncio
async def test_central_pending_request_blocks_ordinary_admission_without_turn(env):
    app = env["app"]
    started = await admitted(env, dispatch=False, funding=False)
    with pytest.raises(DomainError) as error:
        app.conversations.submit(
            env["actor"],
            started["conversation_id"],
            TurnInput(question="cannot overtake", expected_revision=2, idempotency_key="other"),
        )
    assert error.value.code == "LEARNING_REQUEST_PENDING"
    with app.db.read() as db:
        assert not list(
            db.scalars(select(Turn).where(Turn.conversation_id == started["conversation_id"]))
        )


@pytest.mark.asyncio
async def test_pause_heartbeat_retains_inflight_lease_but_paid_boundary_stops(env):
    from careerforge.errors import PauseBoundary
    from careerforge.models_dialogue import WorkflowSession

    app = env["app"]
    started = await admitted(env)
    epoch = app.runtime.claim(started["run_id"], "inflight-worker")
    with app.db.transaction() as db:
        request = db.get(WorkflowTutorRequest, started["request_id"])
        db.get(WorkflowSession, request.workflow_id).control_state = "pause_requested"
        db.get(AgentRun, started["run_id"]).pause_requested = True
    app.runtime.heartbeat(started["run_id"], epoch, "inflight-worker")
    with pytest.raises(PauseBoundary):
        app.runtime.validate(started["run_id"], epoch, "inflight-worker")
    with app.db.transaction() as db:
        request = db.get(WorkflowTutorRequest, started["request_id"])
        db.get(WorkflowSession, request.workflow_id).control_state = "cancel_requested"
    with pytest.raises(DomainError):
        app.runtime.heartbeat(started["run_id"], epoch, "inflight-worker")


@pytest.mark.asyncio
async def test_settlement_rechecks_time_after_finalized_marker_read(env, monkeypatch):
    from careerforge.services import session_turn

    app = env["app"]
    started, epoch = await delivered_crash(env, monkeypatch)
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
    await app.graph_runtime.execute(run, epoch, "crash-worker")
    original = session_turn.validate_finalized_in

    def delayed_marker(db, active):
        original(db, active)
        active.lease_until = clock(db) - timedelta(seconds=1)

    monkeypatch.setattr(session_turn, "validate_finalized_in", delayed_marker)
    assert not app.runtime.finish(run.id, epoch, "crash-worker", "succeeded")
    with app.db.read() as db:
        assert db.get(AgentRun, run.id).status == "running"


@pytest.mark.asyncio
async def test_recovery_uses_recorded_delivery_latency_once_not_synthetic_zero(env, monkeypatch):
    from careerforge.services.session_turn import DELIVERY_METRICS_STAGE

    app = env["app"]
    started, epoch = await delivered_crash(env, monkeypatch)
    metrics = app.runtime.stage(started["run_id"], DELIVERY_METRICS_STAGE)
    assert metrics["kind"] == "elapsed_first_load_to_delivery_ms"
    assert metrics["status"] == "measured" and metrics["latency_ms"] > 0
    observed = []
    original = app.rollouts.observe_turn_in

    def observe(db, turn_id, blocks, latency_ms, status):
        observed.append((turn_id, latency_ms, status))
        return original(db, turn_id, blocks, latency_ms, status)

    monkeypatch.setattr(app.rollouts, "observe_turn_in", observe)
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
    await app.graph_runtime.execute(run, epoch, "crash-worker")
    await app.graph_runtime.execute(run, epoch, "crash-worker")
    assert observed == [(started["turn_id"], metrics["latency_ms"], "completed")]
    assert app.runtime.stage(run.id, DELIVERY_METRICS_STAGE) == metrics


@pytest.mark.asyncio
async def test_finalizer_locks_conversation_first_and_never_mutable_selection(env, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    app = env["app"]
    started, epoch = await delivered_crash(env, monkeypatch)
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
    locked_tables = []

    def observe(orm_state):
        stmt = orm_state.statement
        if getattr(stmt, "_for_update_arg", None) is not None:
            locked_tables.extend(
                table.name for table in stmt.get_final_froms() if hasattr(table, "name")
            )

    event.listen(Session, "do_orm_execute", observe)
    try:
        SessionTurnStages(app, run, epoch, "crash-worker").finalize()
    finally:
        event.remove(Session, "do_orm_execute", observe)
    assert locked_tables[0] == "conversations"
    assert "funding_agreements" in locked_tables and "agent_runs" in locked_tables
    assert not {"learning_plans", "learning_profiles", "conversation_turns"} & set(locked_tables)


async def recover_and_reconcile_request(env, started, epoch):
    """Complete only witnessed projections, then let the real unpaid L reconcile."""
    from careerforge.models_dialogue import WorkflowExecution

    app = env["app"]
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
    await app.graph_runtime.execute(run, epoch, "crash-worker")
    assert app.runtime.finish(run.id, epoch, "crash-worker", "succeeded")
    await app.drain()
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, started["request_id"])
        execution = db.get(WorkflowExecution, request.execution_id)
        assert request.status == "completed" and request.failure_code is None
        assert (execution.status, execution.wait_kind) == ("waiting", "followup")
        assert db.get(Turn, request.turn_id).status == "completed"


def schedule_evidence_gap_is_explicit_bounded_and_not_question_success(
    env, started, gap_id, result
):
    from careerforge.models import FundingAgreement, KnowledgeGap
    from careerforge.models_dialogue import WorkflowExecution
    from careerforge.services.security import create_principal
    from careerforge.util import fingerprint

    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        other, private, _ = create_principal(db, "m4-gap-foreign-payer")
        other_id, other_ns = other.id, private.id
    foreign = app.runtime.create_funding(other_id, other_ns, 30)
    insufficient = app.runtime.create_funding(actor, env["ns"], 14)
    limits = {
        "max_projects": 1,
        "max_units_per_project": 1,
        "duration_minutes": 240,
        "planning_call_seconds": 300,
    }
    with app.db.read() as db:
        run_ids = set(db.scalars(select(AgentRun.id)))
        gap = db.get(KnowledgeGap, gap_id)
        assert gap.status == "open" and gap.build_run_id is None
        source_id = gap.source_ids[0]
        original_question = db.get(Turn, started["turn_id"]).question
    calls = len(env["model"].calls)
    for funding, code in ((foreign, "FUNDING_UNAVAILABLE"), (insufficient, "BUDGET_EXHAUSTED")):
        with pytest.raises(DomainError) as rejected:
            app.lifecycle.schedule_gap(
                actor,
                gap_id,
                funding,
                15,
                "m4-gap-denied:" + funding,
                source_id=source_id,
                **limits,
            )
        assert rejected.value.code == code
        with app.db.read() as db:
            assert set(db.scalars(select(AgentRun.id))) == run_ids
            assert db.get(KnowledgeGap, gap_id).build_run_id is None
            assert db.get(FundingAgreement, funding).calls_held == 0
    with app.db.read() as db:
        held_before = db.get(FundingAgreement, env["funding"]).calls_held
    scheduled = app.lifecycle.schedule_gap(
        actor, gap_id, env["funding"], 15, "m4-explicit-gap", source_id=source_id, **limits
    )
    replay = app.lifecycle.schedule_gap(
        actor, gap_id, env["funding"], 15, "m4-explicit-gap", source_id=source_id, **limits
    )
    assert replay["run_id"] == scheduled["run_id"] and replay["replayed"]
    assert scheduled["automatic_publication_approval"] is False
    with app.db.read() as db:
        build = db.get(AgentRun, scheduled["run_id"])
        contract = build.input_manifest["gap_schedule"]
        assert build.call_cap == 15 and build.calls_consumed == 0
        assert contract["origin"]["question"] == original_question
        assert contract["origin"]["turn_id"] == started["turn_id"]
        assert contract["selected_source_id"] == source_id
        assert all(contract["request"][key] == value for key, value in limits.items())
        assert not contract["request"].get("auto_reanswer")
        assert set(db.scalars(select(AgentRun.id))) == run_ids | {build.id}
        assert db.get(FundingAgreement, env["funding"]).calls_held == held_before + 15
        assert db.get(KnowledgeGap, gap_id).status == "scheduled"
        request = db.get(WorkflowTutorRequest, started["request_id"])
        assert request.status == "completed"
        assert db.get(WorkflowExecution, request.execution_id).wait_kind == "followup"
    reconciliation = app.lifecycle.reconcile_gap(actor, gap_id)
    assert not reconciliation["resolved"] and reconciliation["reason_codes"] == [
        "build_not_terminal"
    ]
    assert len(env["model"].calls) == calls
    assert fingerprint(app.runtime.stage(started["run_id"], "result")) == fingerprint(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["checker_rejected", "whole_question_partial"])
async def test_gap_delivery_recovery_preserves_question_verdict_and_unpaid_wait(
    env, monkeypatch, branch
):
    from test_answer_completion_v166 import completion_model

    from careerforge.contracts import ModelOutput
    from careerforge.contracts_answer_completion_v166 import ANSWER_COMPLETION_STAGE
    from careerforge.models import AnswerBlock, KnowledgeGap, ModelReceipt
    from careerforge.util import fingerprint

    app = env["app"]
    started = await admitted(env)
    observed = completion_model(env, "partial")
    checker_requests = []
    unsupported_text = "The result is stored in registry[source][name]."
    if branch == "checker_rejected":

        def rejected_paraphrase(request):
            unit = request["context"]["evidence_pack"]["knowledge_units"][0]
            fact = unit["facts"][0]
            return ModelOutput(
                structured={
                    "blocks": [
                        {
                            "kind": "repository_fact",
                            "text": unsupported_text,
                            "fact_atom_ids": [fact["fact_atom_id"]],
                            "publication_ids": [unit["publication_id"]],
                            "conditions": fact["conditions"],
                            "transformation": "paraphrase",
                        }
                    ],
                    "complete": True,
                },
                synthetic=True,
            )

        def reject_supported_scope_but_unsupported_draft(request):
            # Exercise the real independent checker gateway/receipt, not the
            # local high-risk or malformed-block fallback. This is a scripted
            # protocol verdict, not an assertion of native semantic quality.
            context = request["context"]
            assert context["proposed_block"]["text"] == unsupported_text
            assert context["proposed_block"]["text"] != context["facts"][0]["text"]
            checker_requests.append(request)
            return ModelOutput(
                structured={
                    "evidence_relevant": True,
                    "supported": False,
                    "conditions_preserved": True,
                    "quantifiers_preserved": True,
                    "no_new_project_facts": False,
                    "reasons": ["synthetic_m4_independent_checker_reject"],
                },
                synthetic=True,
            )

        env["model"].overrides["tutor"] = rejected_paraphrase
        env["model"].overrides["answer_checker"] = reject_supported_scope_but_unsupported_draft
    started, epoch = await delivered_crash(env, monkeypatch, started=started)
    assessment = app.runtime.stage(started["run_id"], ANSWER_COMPLETION_STAGE)
    result = app.runtime.stage(started["run_id"], "result")
    assert assessment["task_satisfied"] is False and assessment["status"] == "partial"
    assert result["task_satisfied"] is False and result["module_completed"] is False
    assert result["competence_verified"] is False
    with app.db.read() as db:
        turn = db.get(Turn, started["turn_id"])
        assert assessment["uncovered_questions"] == [turn.question]
        assert result["uncovered_questions"] == [turn.question]
        blocks = list(
            db.scalars(
                select(AnswerBlock)
                .where(AnswerBlock.turn_id == turn.id)
                .order_by(AnswerBlock.ordinal)
            )
        )
        assert any(
            b.kind == "evidence_gap" and b.content.get("gap_category") == "evidence" for b in blocks
        )
        if branch == "checker_rejected":
            assert assessment["checked"] == "tutor_fallback_only" and not observed
            assert any(
                b.content.get("checked") == "exact_fallback"
                and b.content.get("blocked_reason") == "ANSWER_UNSUPPORTED"
                for b in blocks
            )
            assert all(unsupported_text != b.content.get("text") for b in blocks)
            assert len(checker_requests) == 1
            checker_contract = checker_requests[0]["context"]["reviewed_evidence"]["contract"]
            receipt = db.scalar(
                select(ModelReceipt).where(
                    ModelReceipt.run_id == started["run_id"],
                    ModelReceipt.stage_key.endswith(".v2.check:0:" + checker_contract),
                )
            )
            assert receipt and receipt.role == "answer_checker" and receipt.status == "completed"
            assert receipt.response["structured"]["supported"] is False
            assert receipt.response["structured"]["reasons"] == [
                "synthetic_m4_independent_checker_reject"
            ]
        else:
            assert assessment["checked"] == "independent_model" and len(observed) == 1
            assert assessment["model_assessment"]["status"] == "partial"
        gap = db.scalar(select(KnowledgeGap).where(KnowledgeGap.turn_id == turn.id))
        assert gap and gap.status == "open" and gap.build_run_id is None
        gap_id = gap.id
        original_events = [
            (e.id, fingerprint(e.payload))
            for e in db.scalars(
                select(StreamEvent).where(StreamEvent.turn_id == turn.id).order_by(StreamEvent.seq)
            )
        ]
        run = db.get(AgentRun, started["run_id"])
        consumed, cap = run.calls_consumed, run.call_cap
        assert consumed <= cap
        assert db.get(WorkflowTutorRequest, started["request_id"]).status == "dispatched"
        run_ids = set(db.scalars(select(AgentRun.id)))
    calls = len(env["model"].calls)
    await recover_and_reconcile_request(env, started, epoch)
    assert len(env["model"].calls) == calls
    assert app.runtime.stage(started["run_id"], ANSWER_COMPLETION_STAGE) == assessment
    assert app.runtime.stage(started["run_id"], "result") == result
    with app.db.read() as db:
        assert set(db.scalars(select(AgentRun.id))) == run_ids
        assert db.get(AgentRun, started["run_id"]).calls_consumed == consumed
        assert [
            (e.id, fingerprint(e.payload))
            for e in db.scalars(
                select(StreamEvent)
                .where(StreamEvent.turn_id == started["turn_id"])
                .order_by(StreamEvent.seq)
            )
        ] == original_events
        assert db.get(KnowledgeGap, gap_id).status == "open"
    if branch == "whole_question_partial":
        schedule_evidence_gap_is_explicit_bounded_and_not_question_success(
            env, started, gap_id, result
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,category", [("MODEL_BUDGET", "budget"), ("EGRESS_OR_ROLE_DENIED", "authorization")]
)
async def test_operational_completion_gap_is_unverified_not_schedulable_evidence(
    env, monkeypatch, code, category
):
    from careerforge.contracts_answer_completion_v166 import ANSWER_COMPLETION_STAGE
    from careerforge.models import AnswerBlock, KnowledgeGap, ModelReceipt

    app = env["app"]
    started = await admitted(env)
    original = app.gateway.call

    async def unavailable(*args, **kwargs):
        if args[3] == ANSWER_COMPLETION_STAGE:
            raise DomainError(code, "Synthetic completion-stage operational gate")
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.gateway, "call", unavailable)
    started, epoch = await delivered_crash(env, monkeypatch, started=started)
    assessment = app.runtime.stage(started["run_id"], ANSWER_COMPLETION_STAGE)
    result = app.runtime.stage(started["run_id"], "result")
    assert assessment["status"] == "unverified" and assessment["task_satisfied"] is None
    assert assessment["gap_category"] == category and code in assessment["reason_codes"]
    assert result["task_satisfied"] is None and result["answer_coverage_status"] == "unverified"
    with app.db.read() as db:
        blocks = list(
            db.scalars(
                select(AnswerBlock)
                .where(AnswerBlock.turn_id == started["turn_id"])
                .order_by(AnswerBlock.ordinal)
            )
        )
        assert blocks[-1].content["gap_category"] == category
        assert not db.scalar(
            select(KnowledgeGap.id).where(KnowledgeGap.turn_id == started["turn_id"])
        )
        receipts = list(
            db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"]))
        )
        assert len(receipts) == 2 and all(r.status == "completed" for r in receipts)
        run = db.get(AgentRun, started["run_id"])
        assert run.calls_consumed == 2 <= run.call_cap
        run_ids = set(db.scalars(select(AgentRun.id)))
    calls = len(env["model"].calls)
    await recover_and_reconcile_request(env, started, epoch)
    assert len(env["model"].calls) == calls
    assert app.runtime.stage(started["run_id"], ANSWER_COMPLETION_STAGE) == assessment
    assert app.runtime.stage(started["run_id"], "result") == result
    with app.db.read() as db:
        assert set(db.scalars(select(AgentRun.id))) == run_ids
        assert not db.scalar(
            select(KnowledgeGap.id).where(KnowledgeGap.turn_id == started["turn_id"])
        )
