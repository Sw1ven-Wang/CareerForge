"""Explicit Evolution command/session owner, never inferred training or release consent."""

from datetime import timedelta

from sqlalchemy import select

from ..contracts_learning_ext import ReplayInput
from ..db import aware, clock, locked
from ..errors import DomainError
from ..models import (
    AgentRun,
    EvolutionCandidate,
    EvolutionEvaluation,
    FundingAgreement,
    Idempotency,
    Principal,
)
from ..models_dialogue import (
    WorkflowEvolutionRequest,
    WorkflowExecution,
    WorkflowRunLink,
    WorkflowSession,
)
from ..util import fingerprint, uid
from ..workflows.registry import validate_session_binding
from .evolution_stages import CONTRACT, fail, graph_identity
from .policies import ensure_policies
from .runtime import TERMINAL
from .security import namespace_admin
from .session_execution import SessionExecutions

SESSION_VERSION = "evolution-session-v1"
COMMAND = "evolution.session.command.v1"


def request_in(db, request):
    if (
        request is None
        or request.request_hash != fingerprint(request.request)
        or request.request.get("contract") != "evolution-workflow-request-v1"
        or request.kind != request.request.get("intent", {}).get("type")
    ):
        fail("演化请求缺失或内容改变。")
    receipt = db.get(Idempotency, request.command_receipt_id)
    if (
        receipt is None
        or receipt.principal_id != request.principal_id
        or receipt.operation != COMMAND
        or receipt.result.get("request_id") != request.id
        or receipt.result.get("request_hash") != request.request_hash
        or receipt.result.get("workflow_id") != request.workflow_id
        or receipt.request_hash != request.request.get("origin_request_hash")
        or receipt.request_hash != fingerprint(request.request.get("origin_identity"))
        or request.request.get("origin_identity", {}).get("actor") != request.principal_id
        or request.request.get("origin_identity", {}).get("conversation_id")
        != request.conversation_id
        or request.request.get("workflow_id") != request.workflow_id
        or request.request.get("execution_id") != request.execution_id
        or request.request.get("intent")
        != request.request.get("origin_identity", {}).get("input", {}).get("intent")
    ):
        fail("演化命令原始回执不匹配。")
    if request.funding_id:
        authorization = db.get(Idempotency, request.authorization_receipt_id)
        witness = request.authorization_witness
        if (
            not authorization
            or authorization.principal_id != request.principal_id
            or authorization.operation != COMMAND
            or not isinstance(witness, dict)
            or witness.get("request_hash") != authorization.request_hash
            or fingerprint(witness.get("identity")) != authorization.request_hash
            or witness.get("identity", {}).get("actor") != request.principal_id
            or witness.get("identity", {}).get("conversation_id") != request.conversation_id
            or witness.get("request_id") != request.id
            or witness.get("funding_id") != request.funding_id
            or authorization.result.get("authorized_request_id") != request.id
            or authorization.result.get("authorized_funding_id") != request.funding_id
        ):
            fail("演化费用授权来源不匹配。")
        authorized_input = witness["identity"]["input"]
        authorized_intent = authorized_input["intent"]
        if authorization.id == request.command_receipt_id:
            if (
                authorized_input != request.request["origin_identity"]["input"]
                or (
                    authorized_intent.get("funding_id")
                    if request.kind == "candidate"
                    else authorized_intent.get("replay", {}).get("funding_id")
                )
                != request.funding_id
            ):
                fail()
        elif authorized_input.get("workflow_id") != request.workflow_id or authorized_intent != {
            "type": "authorize_candidate",
            "request_id": request.id,
            "request_hash": request.request_hash,
            "funding_id": request.funding_id,
        }:
            fail()
    return request


def validate_run_binding_in(app, db, actor, ns, manifest, *, run=None, admission=None):
    marker = manifest.get("evolution_graph")
    if not isinstance(marker, dict) or marker.get("contract") != CONTRACT:
        fail()
    request = request_in(db, db.get(WorkflowEvolutionRequest, marker.get("request_id")))
    workflow, execution = (
        db.get(WorkflowSession, request.workflow_id),
        db.get(WorkflowExecution, request.execution_id),
    )
    if not workflow or not execution:
        fail()
    validate_session_refs_in(db, workflow, execution)
    if (
        (request.principal_id, request.namespace_id, request.conversation_id, request.workflow_id)
        != (execution.principal_id, execution.namespace_id, execution.conversation_id, execution.workflow_id)
        or workflow.current_execution_id != execution.id
        or execution.workflow_type != "evolution_session"
        or execution.graph_version != SESSION_VERSION
        or execution.goal_binding_hash != fingerprint(execution.binding_payload)
        or execution.binding_payload.get("identity") != graph_identity()
    ):
        fail()
    expected = {
        "contract": CONTRACT,
        "request_id": request.id,
        "request_hash": request.request_hash,
        "execution_id": request.execution_id,
        "workflow_id": request.workflow_id,
        "binding_hash": execution.goal_binding_hash,
        "identity": graph_identity(),
        "privacy_generations": marker.get("privacy_generations"),
    }
    if (
        marker != expected
        or (request.principal_id, request.namespace_id) != (actor, ns)
        or request.kind not in {"candidate", "replay"}
        or not request.funding_id
    ):
        fail()
    if request.admission_manifest_hash != fingerprint(manifest):
        fail("演化任务与服务器原子冻结的完整 manifest 不同。")
    from .evolution_privacy import validate_manifest_generations_in

    validate_manifest_generations_in(db, actor, ns, manifest)
    intent = request.request["intent"]
    if request.kind == "candidate":
        if (
            manifest.get("target") != intent["target"]
            or manifest.get("experience_ids") != intent["experience_ids"]
            or manifest.get("base_policy_id") != request.request["base_policy_id"]
            or manifest.get("base_hash") != request.request["base_hash"]
        ):
            fail()
        expected = {
            "workflow": "evolution",
            "funding": request.funding_id,
            "cap": 1,
            "minutes": 30,
            "key": "evolution-candidate:" + request.id,
        }
    else:
        original = intent["replay"]
        if (
            manifest.get("candidate_id") != intent["candidate_id"]
            or manifest.get("dataset_id") != original["dataset_id"]
            or any(
                manifest.get(k) != original[k]
                for k in (
                    "per_arm_call_cap",
                    "per_arm_request_cap",
                    "max_research_nodes",
                    "max_research_depth",
                )
            )
        ):
            fail()
        total = len(manifest["cases"]) * 2 * original["per_arm_call_cap"]
        if manifest.get("total_call_cap") != total or total > 100:
            fail()
        expected = {
            "workflow": "evolution_replay",
            "funding": request.funding_id,
            "cap": total,
            "minutes": 120,
            "key": "evolution-replay:" + fingerprint("evolution-replay-command:" + request.id),
        }
    if admission is not None and (
        admission != expected
        or request.run_id is not None
        or request.status != "queued"
        or execution.status != "running"
        or workflow.control_state != "active"
    ):
        fail("同一演化命令禁止以新键或范围创建额外付费运行。")
    if run is not None:
        link = db.scalar(select(WorkflowRunLink).where(WorkflowRunLink.run_id == run.id))
        if (
            request.run_id != run.id
            or run.parent_id
            or run.funding_id != request.funding_id
            or not link
            or link.workflow_id != workflow.id
            or link.milestone != "evolution_" + request.kind
        ):
            fail()
    return request


def validate_session_refs_in(db, workflow, execution):
    if (
        not execution
        or not workflow
        or execution.workflow_type != "evolution_session"
        or execution.graph_version != SESSION_VERSION
        or workflow.session_graph_version != SESSION_VERSION
        or workflow.orchestration_engine != "langgraph"
        or workflow.current_execution_id != execution.id
        or execution.execution_generation != 1
        or (
            execution.workflow_id,
            execution.conversation_id,
            execution.principal_id,
            execution.namespace_id,
        )
        != (workflow.id, workflow.conversation_id, workflow.principal_id, workflow.namespace_id)
    ):
        fail()
    validate_session_binding(execution)
    binding = execution.binding_payload
    if binding != {
        "contract": "evolution-session-binding-v1",
        "execution_id": execution.id,
        "workflow_id": workflow.id,
        "conversation_id": workflow.conversation_id,
        "principal_id": workflow.principal_id,
        "namespace_id": workflow.namespace_id,
        "parent_workflow_id": workflow.parent_workflow_id,
        "origin_receipt_id": binding.get("origin_receipt_id"),
        "identity": graph_identity(),
    } or execution.goal_binding_hash != fingerprint(binding):
        fail()
    origin = db.get(Idempotency, binding["origin_receipt_id"])
    if (
        not origin
        or origin.principal_id != workflow.principal_id
        or origin.result.get("workflow_id") != workflow.id
    ):
        fail()


class EvolutionExecutions(SessionExecutions):
    def validate_in(self, db, workflow, execution, *, access=True):
        validate_session_refs_in(db, workflow, execution)
        if access:
            self.app.workflows.conversation(db, workflow.principal_id, workflow.conversation_id)
            namespace_admin(db, workflow.principal_id, workflow.namespace_id)

    def last_in(self, db, workflow):
        return db.scalar(
            select(WorkflowEvolutionRequest)
            .where(WorkflowEvolutionRequest.workflow_id == workflow.id)
            .order_by(WorkflowEvolutionRequest.sequence.desc())
            .limit(1)
        )

    def submit(self, actor, conversation_id, value):
        identity = {
            "actor": actor,
            "conversation_id": conversation_id,
            "input": value.model_dump(mode="json"),
        }
        with self.db.transaction() as db:
            locked(db, Principal, actor)
            conv = self.app.workflows.conversation(db, actor, conversation_id, lock=True)
            namespace_admin(db, actor, conv.namespace_id)
            old = db.scalar(
                select(Idempotency).where(
                    Idempotency.principal_id == actor,
                    Idempotency.operation == COMMAND,
                    Idempotency.key == value.idempotency_key,
                )
            )
            if old:
                if old.request_hash != fingerprint(identity):
                    raise DomainError("IDEMPOTENCY_CONFLICT", "同键演化命令内容不同。", 409)
                workflow = self.app.workflows.owned_in(db, conv, old.result["workflow_id"])
                if not workflow:
                    fail()
                return self.admission_result_in(db, conv, value, old)
            if value.workflow_id is None and not self.app.settings.evolution_graph_enabled:
                raise DomainError("EVOLUTION_ADMISSION_DISABLED", "新演化图准入尚未启用。", 409)
            intent = value.intent
            receipt = Idempotency(
                principal_id=actor,
                operation=COMMAND,
                key=value.idempotency_key,
                request_hash=fingerprint(identity),
                result={},
            )
            db.add(receipt)
            db.flush()
            if value.workflow_id:
                workflow = self.app.workflows.owned_in(db, conv, value.workflow_id)
                if not workflow or workflow.revision != value.expected_workflow_revision:
                    raise DomainError("REVISION_CONFLICT", "演化会话版本已变化。", 409)
                execution = self.current(db, workflow)
                self.validate_in(db, workflow, execution)
                if (
                    execution.status in {"completed", "failed", "cancelled"}
                    or workflow.control_state != "active"
                ):
                    raise DomainError("TERMINAL_WORKFLOW", "演化会话已结束或暂停。", 409)
            else:
                parent = (
                    self.app.workflows.owned_in(db, conv, value.parent_workflow_id)
                    if value.parent_workflow_id
                    else None
                )
                if value.parent_workflow_id and parent is None:
                    fail("原父会话不存在或不属于本对话。")
                workflow = WorkflowSession(
                    conversation_id=conv.id,
                    principal_id=actor,
                    namespace_id=conv.namespace_id,
                    parent_workflow_id=parent.id if parent else None,
                    orchestration_engine="langgraph",
                    session_graph_version=SESSION_VERSION,
                    learning_graph_version=None,
                    milestone="evolution",
                    target={"entry": "explicit_evolution"},
                )
                db.add(workflow)
                db.flush()
                identifier = uid()
                binding = {
                    "contract": "evolution-session-binding-v1",
                    "execution_id": identifier,
                    "workflow_id": workflow.id,
                    "conversation_id": conv.id,
                    "principal_id": actor,
                    "namespace_id": conv.namespace_id,
                    "parent_workflow_id": workflow.parent_workflow_id,
                    "origin_receipt_id": receipt.id,
                    "identity": graph_identity(),
                }
                execution = WorkflowExecution(
                    id=identifier,
                    workflow_id=workflow.id,
                    conversation_id=conv.id,
                    principal_id=actor,
                    namespace_id=conv.namespace_id,
                    workflow_type="evolution_session",
                    graph_version=SESSION_VERSION,
                    binding_payload=binding,
                    goal_binding_hash=fingerprint(binding),
                )
                db.add(execution)
                db.flush()
                workflow.current_execution_id = execution.id
            previous = self.last_in(db, workflow)
            if intent.type == "authorize_candidate":
                request = request_in(db, db.get(WorkflowEvolutionRequest, intent.request_id))
                if (
                    request.workflow_id != workflow.id
                    or request.kind != "candidate"
                    or request.request_hash != intent.request_hash
                    or request.status != "authorization"
                    or request.funding_id
                    or request.run_id
                ):
                    fail()
                self.authorize_in(db, request, intent.funding_id, receipt, identity)
                result = {
                    "workflow_id": workflow.id,
                    "execution_id": execution.id,
                    "request_id": request.id,
                    "request_hash": request.request_hash,
                    "authorized_request_id": request.id,
                    "authorized_funding_id": request.funding_id,
                }
            else:
                if previous and previous.status in {"authorization", "queued", "running"}:
                    raise DomainError("EVOLUTION_PENDING", "请先完成或取消原演化请求。", 409)
                payload = {
                    "contract": "evolution-workflow-request-v1",
                    "intent": intent.model_dump(mode="json"),
                    "authorization_deadline": (clock(db) + timedelta(hours=24)).isoformat(),
                    "origin_request_hash": receipt.request_hash,
                    "origin_identity": identity,
                    "workflow_id": workflow.id,
                    "execution_id": execution.id,
                }
                if intent.type == "candidate":
                    self.app.evolution._episodes(
                        db, actor, conv.namespace_id, intent.experience_ids
                    )
                    base = ensure_policies(db, actor, conv.namespace_id)[intent.target]
                    payload["base_policy_id"], payload["base_hash"] = (
                        base["policy_id"],
                        base["hash"],
                    )
                if intent.type in {"replay", "promote"}:
                    candidate = db.get(EvolutionCandidate, intent.candidate_id)
                    if not candidate or candidate.namespace_id != conv.namespace_id:
                        fail()
                    # Same session causal chain, not a mutable current candidate.
                    prior_candidate = db.scalar(
                        select(WorkflowEvolutionRequest)
                        .where(
                            WorkflowEvolutionRequest.workflow_id == workflow.id,
                            WorkflowEvolutionRequest.kind == "candidate",
                            WorkflowEvolutionRequest.status == "completed",
                        )
                        .order_by(WorkflowEvolutionRequest.sequence.desc())
                        .limit(1)
                    )
                    if (
                        not prior_candidate
                        or prior_candidate.result.get("candidate_id") != candidate.id
                    ):
                        fail("回放或晋升必须引用本演化会话的原候选。")
                    if intent.type == "promote":
                        prior_replay = db.scalar(
                            select(WorkflowEvolutionRequest)
                            .where(
                                WorkflowEvolutionRequest.workflow_id == workflow.id,
                                WorkflowEvolutionRequest.kind == "replay",
                                WorkflowEvolutionRequest.status == "completed",
                            )
                            .order_by(WorkflowEvolutionRequest.sequence.desc())
                            .limit(1)
                        )
                        if (
                            not prior_replay
                            or prior_replay.result.get("evaluation_id") != intent.evaluation_id
                        ):
                            fail("晋升必须明确引用本会话独立回放结果。")
                request = WorkflowEvolutionRequest(
                    principal_id=actor,
                    namespace_id=conv.namespace_id,
                    conversation_id=conv.id,
                    workflow_id=workflow.id,
                    execution_id=execution.id,
                    command_receipt_id=receipt.id,
                    sequence=(previous.sequence + 1 if previous else 1),
                    kind=intent.type,
                    request=payload,
                    request_hash=fingerprint(payload),
                    status="authorization"
                    if intent.type == "candidate" and not intent.funding_id
                    else "queued",
                    result={},
                )
                db.add(request)
                db.flush()
                funding = (
                    intent.funding_id
                    if intent.type == "candidate"
                    else intent.replay.funding_id
                    if intent.type == "replay"
                    else None
                )
                if funding:
                    self.authorize_in(db, request, funding, receipt, identity)
                result = {
                    "workflow_id": workflow.id,
                    "execution_id": execution.id,
                    "request_id": request.id,
                    "request_hash": request.request_hash,
                    "status": request.status,
                    **(
                        {
                            "authorized_request_id": request.id,
                            "authorized_funding_id": request.funding_id,
                        }
                        if request.funding_id
                        else {}
                    ),
                }
            workflow.revision += 1
            result["workflow_revision"] = workflow.revision
            receipt.result = result
            db.flush()
            self.wake_in(db, workflow, execution, "explicit_evolution_command")
            self.app.workflows.emit_in(
                db,
                conv,
                "workflow.evolution_command",
                "evolution-command:" + receipt.id,
                workflow,
                request_id=request.id,
                operation=request.kind,
            )
            return result

    def admission_result_in(self, db, conv, value, receipt):
        """Replay the exact original admission references, never a generic dump."""
        if not isinstance(receipt.result, dict):
            fail()
        request = request_in(db, db.get(WorkflowEvolutionRequest, receipt.result.get("request_id")))
        if (request.principal_id, request.conversation_id) != (conv.principal_id, conv.id):
            fail()
        if value.workflow_id and request.workflow_id != value.workflow_id:
            fail()
        expected = {
            "workflow_id": request.workflow_id,
            "execution_id": request.execution_id,
            "request_id": request.id,
            "request_hash": request.request_hash,
            "workflow_revision": value.expected_workflow_revision + 1 if value.workflow_id else 2,
        }
        intent = value.intent
        if intent.type == "authorize_candidate":
            if request.authorization_receipt_id != receipt.id or intent.request_id != request.id:
                fail()
            funding = intent.funding_id
        else:
            if request.command_receipt_id != receipt.id:
                fail()
            funding = (intent.funding_id if intent.type == "candidate" else
                       intent.replay.funding_id if intent.type == "replay" else None)
            expected["status"] = "authorization" if intent.type == "candidate" and not funding else "queued"
        if funding:
            expected.update(authorized_request_id=request.id, authorized_funding_id=funding)
        if receipt.result != expected:
            fail("原准入回执不是精确的无正文引用结果。")
        return expected

    def authorize_in(self, db, request, funding_id, receipt, identity):
        funding = locked(db, FundingAgreement, funding_id)
        if (
            not funding
            or funding.payer_id != request.principal_id
            or funding.namespace_id != request.namespace_id
            or funding.status != "active"
            or aware(funding.expires_at) <= clock(db)
        ):
            raise DomainError("FUNDING_UNAVAILABLE", "演化费用授权不可用。", 403)
        from datetime import datetime

        if clock(db) >= datetime.fromisoformat(request.request["authorization_deadline"]):
            raise DomainError("DEADLINE", "原演化请求授权时限已过。", 409)
        request.funding_id, request.authorization_receipt_id, request.status = (
            funding.id,
            receipt.id,
            "queued",
        )
        request.authorization_witness = {
            "request_hash": receipt.request_hash,
            "identity": identity,
            "request_id": request.id,
            "funding_id": funding.id,
        }

    def route_in(self, db, workflow, execution):
        request = self.last_in(db, workflow)
        if not request:
            fail()
        request_in(db, request)
        if request.status == "authorization":
            return "wait_authorization"
        if request.status == "queued":
            return "dispatch" if request.kind in {"candidate", "replay"} else "effect"
        if request.status == "running":
            run = db.get(AgentRun, request.run_id)
            if not run:
                fail()
            return "reconcile" if run.status in TERMINAL else "wait_run"
        if request.status in {"failed", "rejected", "cancelled"} or request.kind in {
            "promote",
            "rollback",
            "reject",
        }:
            return "completed"
        if request.kind == "replay":
            evaluation = db.get(EvolutionEvaluation, request.result.get("evaluation_id"))
            if not evaluation:
                fail()
            return "wait_promotion" if evaluation.decision == "passed" else "completed"
        return "wait_replay"

    def operate_in(self, db, conv, workflow, execution, operation):
        request = request_in(db, self.last_in(db, workflow))
        if self.route_in(db, workflow, execution) != operation:
            return
        intent = request.request["intent"]
        actor = request.principal_id
        if operation == "dispatch":
            from datetime import datetime

            if clock(db) >= datetime.fromisoformat(request.request["authorization_deadline"]):
                raise DomainError("DEADLINE", "演化原始授权期限已过。", 409)
            marker = {
                "contract": CONTRACT,
                "request_id": request.id,
                "request_hash": request.request_hash,
                "execution_id": execution.id,
                "workflow_id": workflow.id,
                "binding_hash": execution.goal_binding_hash,
                "identity": graph_identity(),
            }
            if request.kind == "candidate":
                run_id = self.app.evolution.start_candidate_in(
                    db,
                    actor,
                    workflow.namespace_id,
                    request.funding_id,
                    intent["experience_ids"],
                    intent["target"],
                    "evolution-candidate:" + request.id,
                    graph_binding=marker,
                    frozen_base=(request.request["base_policy_id"], request.request["base_hash"]),
                )
            else:
                value = ReplayInput.model_validate(
                    {
                        **intent["replay"],
                        "idempotency_key": "evolution-replay-command:" + request.id,
                    }
                )
                run_id = self.app.evolution.start_replay_in(
                    db, actor, intent["candidate_id"], value, graph_binding=marker
                )["run_id"]
            request.run_id, request.status = run_id, "running"
            self.app.workflows.link_in(
                db, workflow, db.get(AgentRun, run_id), "evolution_" + request.kind
            )
        elif operation == "reconcile":
            run = db.get(AgentRun, request.run_id)
            if run.status != "succeeded":
                request.status, request.failure_code = "failed", run.status
                return
            from .evolution_stages import reconcile

            result = reconcile(self.app, run, terminal=True)
            request.result = {
                key: result[key]
                for key in ("candidate_id", "evaluation_id", "published")
                if key in result
            }
            request.status = "completed"
        elif operation == "effect":
            from .evolution_commands import EvolutionCommands

            commands = EvolutionCommands(self.app)
            scope = {
                "workflow_id": workflow.id,
                "execution_id": execution.id,
                "request_id": request.id,
                "request_hash": request.request_hash,
            }
            if request.kind == "promote":
                request.result = commands.promote_in(
                    db,
                    actor,
                    intent["candidate_id"],
                    intent["evaluation_id"],
                    intent["expected_revision"],
                    request.id,
                    scope=scope,
                )
            elif request.kind == "rollback":
                request.result = commands.rollback_in(
                    db,
                    actor,
                    workflow.namespace_id,
                    intent["target"],
                    intent["expected_revision"],
                    request.id,
                    intent["from_policy_id"],
                    intent["to_policy_id"],
                    scope=scope,
                )
            elif request.kind == "reject":
                request.result = {"rejected": True}
            else:
                fail()
            request.status = "completed"
        self.app.workflows.emit_in(
            db,
            conv,
            "workflow.evolution_stage",
            f"evolution:{request.id}:{operation}",
            workflow,
            request_id=request.id,
            stage=operation,
        )

    def acceptance_in(self, db, workflow, execution):
        return self.route_in(db, workflow, execution) == "completed"

    def reconcile_withdrawal(self, feedback_id):
        from ..models import Experience, Feedback

        with self.db.read() as db:
            feedback = db.get(Feedback, feedback_id)
            if not feedback or feedback.consent_revision != 0:
                return
            actor = feedback.principal_id
            experience_ids = set(
                db.scalars(select(Experience.id).where(Experience.feedback_id == feedback_id))
            )
            affected = []
            for request in db.scalars(
                select(WorkflowEvolutionRequest).where(
                    WorkflowEvolutionRequest.principal_id == actor
                )
            ):
                intent = request.request.get("intent", {})
                candidate = (
                    db.get(EvolutionCandidate, intent.get("candidate_id"))
                    if intent.get("candidate_id")
                    else None
                )
                if experience_ids.intersection(intent.get("experience_ids", [])) or (
                    candidate and candidate.status == "consent_revoked"
                ):
                    affected.append((request.workflow_id, request.conversation_id))
        for workflow_id, conversation_id in sorted(set(affected)):
            with self.db.transaction() as db:
                locked(db, Principal, actor)
                self.app.workflows.conversation(db, actor, conversation_id, lock=True)
                workflow = locked(db, WorkflowSession, workflow_id)
                execution = locked(db, WorkflowExecution, workflow.current_execution_id)
                for request in db.scalars(
                    select(WorkflowEvolutionRequest).where(
                        WorkflowEvolutionRequest.workflow_id == workflow.id
                    )
                ):
                    if request.status in {"authorization", "queued", "running"}:
                        request.status, request.failure_code = "cancelled", "EXPERIENCE_CONSENT"
                if execution.status not in {"completed", "cancelled", "failed"}:
                    execution.epoch += 1
                    execution.status, execution.last_error_code = "failed", "EXPERIENCE_CONSENT"
                    execution.worker_id, execution.lease_until, execution.wait_kind = (
                        None,
                        None,
                        None,
                    )
                    execution.handled_generation = execution.wake_generation
                    execution.revision += 1

    def cancel_in(self, db, workflow):
        for request in db.scalars(
            select(WorkflowEvolutionRequest).where(
                WorkflowEvolutionRequest.workflow_id == workflow.id,
                WorkflowEvolutionRequest.status.in_({"authorization", "queued", "running"}),
            )
        ):
            request.status, request.failure_code = "cancelled", "WORKFLOW_CANCELLED"

    def detail(self, actor, conversation_id, workflow_id):
        with self.db.read() as db:
            conv = self.app.workflows.conversation(db, actor, conversation_id)
            workflow = self.app.workflows.owned_in(db, conv, workflow_id)
            if not workflow:
                fail()
            execution = self.current(db, workflow)
            self.validate_in(db, workflow, execution)
            request = request_in(db, self.last_in(db, workflow))
            if not isinstance(request.result, dict):
                fail()
            result = dict(request.result)
            if request.status != "completed" and result:
                fail("未完成命令不能投影任意结果。")
            if request.status == "completed" and request.kind == "reject" and result != {"rejected": True}:
                fail()
            if request.status == "completed" and request.kind in {"promote", "rollback"}:
                from .evolution_commands import EvolutionCommands

                commands = EvolutionCommands(self.app)
                intent = request.request["intent"]
                scope = {
                    "workflow_id": workflow.id,
                    "execution_id": execution.id,
                    "request_id": request.id,
                    "request_hash": request.request_hash,
                }
                payload = (
                    {k: intent[k] for k in ("candidate_id", "evaluation_id", "expected_revision")}
                    if request.kind == "promote"
                    else {
                        "namespace_id": workflow.namespace_id,
                        "target": intent["target"],
                        "expected_revision": intent["expected_revision"],
                        "expected_from_policy_id": intent["from_policy_id"],
                        "expected_to_policy_id": intent["to_policy_id"],
                    }
                )
                receipt = db.get(Idempotency, result.get("command_receipt_id"))
                if (
                    not receipt
                    or receipt.operation != "evolution." + request.kind + ".v1"
                    or receipt.key != request.id
                ):
                    fail()
                command = commands._request(actor, receipt.operation, request.id, payload, scope)
                result = commands._read(db, actor, receipt, command)
            if request.status == "completed" and request.run_id:
                from .evolution_stages import historical_summary

                history = historical_summary(self.app, actor, request.run_id)
                key = "candidate_id" if request.kind == "candidate" else "evaluation_id"
                expected = {key: history[key], "published": False}
                if result != expected:
                    fail("会话结果与原事务效果引用不一致。")
                result = {**expected, "current_evidence_available": history["current_evidence_available"]}
            return {
                "workflow_id": workflow.id,
                "workflow_revision": workflow.revision,
                "execution_id": execution.id,
                "status": execution.status,
                "wait_kind": execution.wait_kind,
                "request_id": request.id,
                "request_hash": request.request_hash,
                "request_status": request.status,
                "run_id": request.run_id,
                "result": result,
                "failure_code": request.failure_code or execution.last_error_code,
            }
