"""Paid accepted-module stages and the narrow, no-model delivery capability.

A delivery witness is written by AnswerService in its terminal transaction. It
is not inferred from a completed Turn or a checkpoint, and cannot authorize a
model call. Only selection drift is relaxed after that committed boundary.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from sqlalchemy import select

from ..contracts import IntentDecision, TaskType
from ..contracts_answer_completion_v166 import ANSWER_COMPLETION_STAGE
from ..db import aware, clock, locked
from ..errors import DomainError, LeaseLost
from ..models import (
    AgentRun,
    AnswerBlock,
    Conversation,
    ExternalRequest,
    FundingAgreement,
    KnowledgeGap,
    ModelReceipt,
    StageArtifact,
    StreamEvent,
    Turn,
    Workspace,
)
from ..util import fingerprint
from .answer_continuity import continuation_projection, continued_focus
from .dependencies import check_closure, require_object
from .policies import frozen_policies
from .session_tutor import (
    SESSION_TUTOR_KEY,
    session_evidence_in,
    validate_session_funding_in,
    validate_session_tutor_in,
)

REQUEST_KEY = "learning_tutor_request"
STARTED_STAGE = "learning_tutor.started.v1"
DELIVERY_METRICS_STAGE = "learning_tutor.delivery_metrics.v1"
DELIVERY_STAGE = "learning_tutor.delivery.v1"
FINALIZED_STAGE = "learning_tutor.finalized.v1"
GRAPH_VERSION = "learning-tutor-v1"


def _fail():
    raise DomainError("LEARNING_TUTOR_DELIVERY", "已交付导师回答的固定凭据不一致。", 409)


def _stage(db, run, key):
    row = db.scalar(
        select(StageArtifact).where(StageArtifact.run_id == run.id, StageArtifact.stage_key == key)
    )
    if row is not None and row.input_hash != run.input_hash:
        _fail()
    return row


def has_delivery_in(db, run):
    # Presence is only a route hint. leased_finalization_in always validates it.
    return run is not None and _stage(db, run, DELIVERY_STAGE) is not None


def _rows(db, turn):
    return list(
        db.scalars(
            select(AnswerBlock).where(AnswerBlock.turn_id == turn.id).order_by(AnswerBlock.ordinal)
        )
    )


def _terminal(db, turn):
    events = list(
        db.scalars(
            select(StreamEvent).where(
                StreamEvent.turn_id == turn.id, StreamEvent.event_type == "answer.completed"
            )
        )
    )
    if len(events) != 1:
        _fail()
    return events[0]


def _identity(db, run, turn, snapshot):
    retrieval = _stage(db, run, "retrieval")
    completion = _stage(db, run, ANSWER_COMPLETION_STAGE)
    if retrieval is None or completion is None:
        _fail()
    manifest = require_object(db, run.principal_id, turn.manifest_id, "selection_manifest")
    terminal = _terminal(db, turn)
    started, metrics = _stage(db, run, STARTED_STAGE), _stage(db, run, DELIVERY_METRICS_STAGE)
    if (
        started is None
        or metrics is None
        or metrics.payload.get("started_at") != started.payload.get("started_at")
    ):
        _fail()
    from ..models_dialogue import WorkflowExecution, WorkflowTutorRequest

    request = db.get(
        WorkflowTutorRequest, run.input_manifest.get(REQUEST_KEY, {}).get("request_id")
    )
    execution = db.get(WorkflowExecution, request.execution_id) if request else None
    if request is None or execution is None:
        _fail()
    return {
        "contract": "learning-tutor-delivery-v1",
        "request": run.input_manifest.get(REQUEST_KEY),
        "learning_binding_hash": execution.goal_binding_hash,
        "accepted_hash": fingerprint(request.request.get("accepted")),
        "goal": {
            "goal_version_id": (request.request.get("accepted", {}).get("goal") or {}).get(
                "goal_version_id"
            ),
            "hash": fingerprint(request.request.get("accepted", {}).get("goal")),
        },
        "run_id": run.id,
        "turn_id": turn.id,
        "input_hash": run.input_hash,
        "principal_id": run.principal_id,
        "namespace_id": run.namespace_id,
        "conversation_id": turn.conversation_id,
        "question_hash": fingerprint(turn.question),
        "focus_revision": turn.focus_revision,
        "policy_hash": fingerprint(turn.policy_versions),
        "focus_hash": fingerprint(turn.focus),
        "funding_id": run.funding_id,
        "call_cap": run.call_cap,
        "model_profile": run.input_manifest.get("model_profile"),
        "session": turn.focus.get(SESSION_TUTOR_KEY),
        "session_hash": fingerprint(snapshot),
        "privacy_generation": snapshot["privacy_generation"],
        "module": {
            key: snapshot["module"][key]
            for key in (
                "plan_id",
                "version_id",
                "module_id",
                "module_content_hash",
                "plan_revision",
                "publication_id",
                "unit_version_id",
                "source_id",
            )
        },
        "plan_manifest_hash": snapshot["plan_manifest_hash"],
        "retrieval_manifest_id": manifest.id,
        "retrieval_manifest_hash": manifest.content_hash,
        "retrieval_hash": fingerprint(retrieval.payload),
        "blocks": [
            {"id": r.id, "ordinal": r.ordinal, "hash": r.content_hash} for r in _rows(db, turn)
        ],
        "completion_hash": fingerprint(completion.payload),
        "started_hash": fingerprint(started.payload),
        "delivery_metrics_hash": fingerprint(metrics.payload),
        "terminal_event": {
            "id": terminal.id,
            "seq": terminal.seq,
            "hash": fingerprint(terminal.payload),
        },
    }


def seal_delivery_in(db, runtime, turn, lease):
    """Called only inside AnswerService.finish's live-authority transaction."""
    if not lease:
        return
    run = db.get(AgentRun, lease[0])
    if run is None or REQUEST_KEY not in run.input_manifest:
        return
    from .runtime import leased

    active = leased(db, *lease)
    if (
        active.workflow,
        active.workflow_engine,
        active.graph_version,
        active.state_schema_version,
    ) != ("chat", "langgraph", GRAPH_VERSION, 1) or turn.status != "completed":
        _fail()
    snapshot = validate_session_tutor_in(db, active.principal_id, turn, run=active)
    started = _stage(db, active, STARTED_STAGE)
    if started is None:
        _fail()
    try:
        started_at = aware(datetime.fromisoformat(started.payload["started_at"]))
    except (KeyError, TypeError, ValueError):
        _fail()
    delivered_at = clock(db)
    elapsed = (delivered_at - started_at).total_seconds() * 1000
    # This measures elapsed first-load-to-delivery time, including recovery waits.
    # A backwards wall clock is unknown, never a fabricated zero-latency sample.
    runtime.save_stage_in(
        db,
        active,
        DELIVERY_METRICS_STAGE,
        {
            "kind": "elapsed_first_load_to_delivery_ms",
            "started_at": started_at.isoformat(),
            "delivered_at": delivered_at.isoformat(),
            "status": "measured" if elapsed >= 0 else "unavailable_clock_order",
            "latency_ms": elapsed if elapsed >= 0 else None,
        },
    )
    db.flush()
    witness = _identity(db, active, turn, snapshot)
    runtime.save_stage_in(
        db, active, DELIVERY_STAGE, {"binding": witness, "binding_hash": fingerprint(witness)}
    )


def validate_delivery_in(db, run):
    """Read-only committed evidence, plus ALL non-selection live fences.

    The historical session reader is used ONLY to check immutable admission
    artifacts. Its three extra relaxations (funding, independent mode and source
    workflow control) are explicitly restored below before granting capability.
    """
    from ..models_dialogue import WorkflowSession
    from ..models_learning_ext import PlanProposalManifest
    from .feedback_target_v1 import validate_run_in as feedback_run
    from .learning_requests import validate_delivered_request_in
    from .ordinary_checker import validate_ordinary_run_in
    from .privacy import assert_personal_access

    witness = _stage(db, run, DELIVERY_STAGE)
    if witness is None or (
        run.workflow,
        run.workflow_engine,
        run.graph_version,
        run.state_schema_version,
    ) != ("chat", "langgraph", GRAPH_VERSION, 1):
        _fail()
    if fingerprint(run.input_manifest) != run.input_hash:
        _fail()
    # Completed Turns are immutable. This reader never mutates a Turn and must
    # not acquire unnecessary selection/Turn locks behind Funding/Run. ACK and
    # admission serialize their writes separately; selection may have advanced.
    turn = db.get(Turn, run.input_manifest.get("turn_id"), populate_existing=True)
    if turn is None or turn.run_id != run.id or turn.status != "completed":
        _fail()
    validate_delivered_request_in(db, run, run.input_manifest.get(REQUEST_KEY))
    snapshot = validate_session_tutor_in(
        db, run.principal_id, turn, run=run, history=True, immutable_delivery=True
    )
    if snapshot is None:
        _fail()
    assert_personal_access(db, run.principal_id, expected_generation=snapshot["privacy_generation"])
    validate_session_funding_in(db, run.principal_id, run, turn.focus[SESSION_TUTOR_KEY])
    funding = db.get(FundingAgreement, run.funding_id)
    if funding.payer_id != run.principal_id or funding.namespace_id != run.namespace_id:
        raise DomainError("FUNDING_UNAVAILABLE", "导师付款身份已改变。", 403)
    if db.scalar(
        select(Workspace.id).where(
            Workspace.principal_id == run.principal_id,
            Workspace.assistance_mode == "independent",
            Workspace.sealed.is_(False),
        )
    ):
        raise DomainError("INDEPENDENT_ATTEMPT", "独立练习期间不能完成导师结算。")
    proposal = db.scalar(
        select(PlanProposalManifest).where(
            PlanProposalManifest.plan_version_id == snapshot["request"]["version_id"]
        )
    )
    scope = proposal.payload.get("input", {}).get("dialogue_scope") if proposal else None
    if scope:
        workflow = db.get(WorkflowSession, scope.get("workflow_id"))
        if (
            workflow is None
            or workflow.conversation_id != turn.conversation_id
            or workflow.control_state != "active"
        ):
            raise DomainError("SESSION_TUTOR_SCOPE", "原目标会话已暂停或取消。", 409)
    feedback_run(db, run)
    validate_ordinary_run_in(db, run)
    frozen_policies(db, run.principal_id, run.namespace_id, turn.policy_versions)
    for cls in (ModelReceipt, ExternalRequest):
        if db.scalar(
            select(cls.id).where(cls.run_id == run.id, cls.status.in_(["attempted", "unknown"]))
        ):
            raise DomainError(
                "RECONCILIATION_REQUIRED", "尚有外部请求结果未知，不能完成结算。", 409
            )
    rows = _rows(db, turn)
    if not rows or [r.ordinal for r in rows] != list(range(len(rows))):
        _fail()
    check_closure(db, run.principal_id, [r.id for r in rows] + [turn.manifest_id])
    for row in rows:
        obj = require_object(db, run.principal_id, row.id, "answer_block")
        if (
            obj.payload != row.content
            or obj.content_hash != row.content_hash
            or fingerprint(row.content) != row.content_hash
        ):
            _fail()
    binding = _identity(db, run, turn, snapshot)
    if witness.payload != {"binding": binding, "binding_hash": fingerprint(binding)}:
        _fail()
    return turn, snapshot, rows


def leased_finalization_in(db, run_id, epoch, worker):
    from .run_pause import ensure_unpaused
    from .runtime import TERMINAL, active_funding, lock_run

    run = lock_run(db, run_id)

    def fence():
        now = clock(db)
        if (
            run is None
            or run.status != "running"
            or run.epoch != epoch
            or run.worker_id != worker
            or run.lease_until is None
            or aware(run.lease_until) <= now
            or aware(run.deadline_at) <= now
        ):
            raise LeaseLost()

    fence()
    ensure_unpaused(db, run)
    funding = active_funding(db, run)
    validate_delivery_in(db, run)
    parent = db.get(AgentRun, run.parent_id) if run.parent_id else None
    if parent and (parent.status in TERMINAL or aware(parent.deadline_at) <= clock(db)):
        raise LeaseLost()
    fence()
    if funding.status != "active" or aware(funding.expires_at) <= clock(db):
        raise DomainError("FUNDING_UNAVAILABLE", "费用授权已失效。", 403)
    return run


def validate_finalized_in(db, run):
    result, finalized = _stage(db, run, "result"), _stage(db, run, FINALIZED_STAGE)
    delivery = _stage(db, run, DELIVERY_STAGE)
    if (
        result is None
        or finalized is None
        or delivery is None
        or finalized.payload
        != {
            "delivery_hash": fingerprint(delivery.payload),
            "result_hash": fingerprint(result.payload),
        }
    ):
        _fail()


def reviewed_evidence(app, db, actor, turn, active):
    """Shared by legacy accepted-session processing and the real graph node."""
    return session_evidence_in(app.knowledge, db, actor, turn, active)


class SessionTurnStages:
    def __init__(self, app, run, epoch, worker):
        self.app, self.run, self.lease = app, run, (run.id, epoch, worker)
        self.turn_id = run.input_manifest["turn_id"]

    def load(self):
        from .runtime import leased

        with self.app.db.transaction() as db:
            active = leased(db, *self.lease)
            turn, conv = self.app.answers._turn(db, active.principal_id, self.turn_id, self.lease)
            if turn.status == "completed":
                _fail()  # A completed M4 Turn without a witness is not resumable.
            snapshot = validate_session_tutor_in(db, active.principal_id, turn, run=active)
            if snapshot is None:
                _fail()
            focus = deepcopy(turn.focus)
            focus.pop("accepted_module_guidance", None)
            if snapshot.get("module_guidance") is True:
                focus["accepted_module_guidance"] = True
            # Same bounded, explicitly non-authoritative legacy history.
            history = []
            previous = db.scalars(
                select(Turn)
                .where(
                    Turn.conversation_id == conv.id, Turn.status == "completed", Turn.id != turn.id
                )
                .order_by(Turn.created_at.desc())
                .limit(4)
            ).all()
            for old in reversed(previous):
                fragments = []
                for block in _rows(db, old):
                    try:
                        check_closure(db, active.principal_id, [block.id])
                        fragments.append(block.content.get("text", "")[:600])
                    except DomainError:
                        fragments.append("[历史证据当前不可用]")
                history.append(
                    {
                        "question": old.question[:600],
                        "answer_excerpt": "\n".join(fragments)[:1200],
                        "historical_not_new_evidence": True,
                    }
                )
            focus["history"] = history
            policies = frozen_policies(
                db, active.principal_id, conv.namespace_id, turn.policy_versions
            )
            turn.status = "running"
            if _stage(db, active, STARTED_STAGE) is None:
                self.app.runtime.save_stage_in(
                    db, active, STARTED_STAGE, {"started_at": clock(db).isoformat()}
                )
            return {
                "actor": active.principal_id,
                "question": turn.question,
                "focus": focus,
                "policies": policies,
            }

    def retrieve(self):
        from .runtime import leased

        with self.app.db.transaction() as db:
            active = leased(db, *self.lease)
            evidence = reviewed_evidence(
                self.app, db, active.principal_id, db.get(Turn, self.turn_id), active
            )
            self.app.runtime.save_stage_in(db, active, "retrieval", evidence)
        return evidence

    async def stream(self):
        context = self.load()
        evidence = self.app.runtime.stage(self.run.id, "retrieval")
        if evidence is None:
            _fail()
        blocks = self.app.answers.committed(context["actor"], self.turn_id)
        if self.app.runtime.stage(self.run.id, ANSWER_COMPLETION_STAGE) is None:
            async for block in self.app.answers.iter_tutor(
                context["actor"],
                context["question"],
                evidence,
                context["policies"],
                context["focus"],
                self.lease,
                start_ordinal=len(blocks),
                answer_contract=context["focus"].get("answer_contract"),
            ):
                self.app.answers.commit_block(
                    context["actor"],
                    self.turn_id,
                    block,
                    evidence["retrieval_manifest_id"],
                    len(blocks),
                    self.lease,
                )
                blocks.append(block)

    async def complete(self):
        self.load()
        evidence = self.app.runtime.stage(self.run.id, "retrieval")
        if evidence is None:
            _fail()
        _, gap = await self.app.answers.complete_question(
            self.run.principal_id, self.turn_id, evidence["retrieval_manifest_id"], self.lease
        )
        if gap is not None:
            blocks = self.app.answers.committed(self.run.principal_id, self.turn_id)
            self.app.answers.commit_block(
                self.run.principal_id,
                self.turn_id,
                gap,
                evidence["retrieval_manifest_id"],
                len(blocks),
                self.lease,
            )

    def deliver(self):
        evidence = self.app.runtime.stage(self.run.id, "retrieval")
        if evidence is None:
            _fail()
        self.app.answers.finish(
            self.run.principal_id, self.turn_id, evidence["retrieval_manifest_id"], self.lease
        )

    def finalize(self):
        with self.app.db.transaction() as db:
            # Admissions already own Conversation before Funding. Acquire the
            # exact previewed conversation first, and revalidate its association
            # after the Run/witness locks; never lock another conversation later.
            preview = db.get(Turn, self.turn_id)
            if preview is None:
                _fail()
            conv = locked(db, Conversation, preview.conversation_id)
            active = self.app.runtime._leased_execution_in(db, *self.lease)
            turn, snapshot, rows = validate_delivery_in(db, active)
            if (
                conv is None
                or turn.conversation_id != conv.id
                or conv.principal_id != active.principal_id
            ):
                _fail()
            terminal = _terminal(db, turn)
            assessment = _stage(db, active, ANSWER_COMPLETION_STAGE).payload
            result = {
                "turn_id": turn.id,
                "retrieval_manifest_id": turn.manifest_id,
                **{
                    k: v for k, v in terminal.payload.items() if k not in {"turn_id", "block_count"}
                },
                "uncovered_questions": assessment["uncovered_questions"],
            }
            self.app.runtime.save_stage_in(db, active, "result", result)
            marker = {
                "delivery_hash": fingerprint(_stage(db, active, DELIVERY_STAGE).payload),
                "result_hash": fingerprint(result),
            }
            old = _stage(db, active, FINALIZED_STAGE)
            if old is not None:
                if old.payload != marker:
                    _fail()
                return
            blocks = [r.content for r in rows]
            gaps = [
                b
                for b in blocks
                if b.get("kind") == "evidence_gap"
                and b.get("gap_category", "evidence") == "evidence"
            ]
            if gaps and not db.scalar(
                select(KnowledgeGap.id).where(KnowledgeGap.turn_id == turn.id)
            ):
                sources = snapshot["task_spec"]["project_source_ids"]
                db.add(
                    KnowledgeGap(
                        namespace_id=active.namespace_id,
                        principal_id=active.principal_id,
                        turn_id=turn.id,
                        topic_keys=snapshot["task_spec"]["topic_keys"],
                        source_ids=sources,
                        reason_codes=sorted(
                            {
                                reason
                                for b in gaps
                                for reason in b.get("details", ["insufficient_evidence"])
                            }
                        ),
                        proposed_route="analysis" if sources else "research",
                    )
                )
            metrics = _stage(db, active, DELIVERY_METRICS_STAGE).payload
            if metrics["status"] == "measured":
                self.app.rollouts.observe_turn_in(
                    db, turn.id, blocks, metrics["latency_ms"], "completed"
                )
            if conv.revision == turn.focus_revision + 1:
                intent = IntentDecision(
                    interpreted_intent=TaskType.IMPLEMENTATION,
                    proposed_next_action="retrieve_and_tutor",
                    reference_resolution="resolved",
                    confidence=1.0,
                    topic_keys=snapshot["task_spec"]["topic_keys"],
                    project_source_ids=[snapshot["module"]["source_id"]],
                    rationale_codes=["explicit_accepted_session_module"],
                )
                conv.focus = continued_focus(
                    conv.focus, continuation_projection(intent, turn.id), blocks
                )
            # Recheck the same capability after potentially blocking projection reads.
            self.app.runtime._leased_execution_in(db, *self.lease)
            self.app.runtime.save_stage_in(db, active, FINALIZED_STAGE, marker)

    def reconcile(self):
        with self.app.db.transaction() as db:
            active = self.app.runtime._leased_execution_in(db, *self.lease)
            validate_finalized_in(db, active)
