from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import __version__
from .application import Application
from .config import Settings
from .contracts import (
    Contract,
    FeedbackInput,
    ImportSourceInput,
    KnowledgeBuildInput,
    RetrievalTaskSpec,
    TurnInput,
)
from .contracts_evolution_graph import EvolutionSessionInput
from .contracts_feedback_target_v1 import (
    AnswerContinuationFeedbackInput,
    AnswerFeedbackConversationInput,
)
from .contracts_ordinary_checker_v166 import AdoptConversationCheckerInput
from .contracts_session_progress_v166 import SessionModuleAckInput
from .contracts_session_tutor_v166 import GuidedTutorTurnInput, SessionTutorTurnInput
from .errors import DomainError
from .models import (
    AgentRun,
    Dispute,
    EvolutionCandidate,
    EvolutionEvaluation,
    Impact,
    PatchProposal,
    Principal,
    ResearchNode,
    RunEvent,
    SearchObservation,
    StageArtifact,
)
from .observability import CONTENT_TYPE_LATEST, HTTP_LATENCY, generate_latest
from .services.dependencies import check_closure, invalidate
from .services.policies import ensure_policies
from .services.privacy import assert_personal_access
from .services.security import (
    authenticate,
    create_principal,
    namespace_access,
    principal,
    private_namespace,
    set_grant,
)
from .services.taxonomy import facet_object_id, topic_object_id


class FundingBody(Contract):
    namespace_id: str
    call_cap: int = Field(ge=1, le=10000)
    allowed_roles: list[str] | None = None
    hours: int = Field(default=24, ge=1, le=168)
    data_classes: list[str] | None = None
    allowed_source_ids: list[str] | None = Field(default=None, max_length=1024)
    confirm: Literal["AUTHORIZE"]


class ConversationBody(Contract):
    auto_build: bool = False
    build_call_cap: int = Field(default=24, ge=8, le=100)


class CancelBody(Contract):
    expected_revision: int = Field(ge=1)


class InvalidateBody(Contract):
    state: Literal["suspended", "revoked"]
    reason: str = Field(min_length=1, max_length=1000)


class SelectHeadBody(Contract):
    selection_namespace_id: str
    publication_id: str
    expected_revision: int = Field(ge=0)


class DisputeBody(Contract):
    target_id: str
    reason: str = Field(min_length=1, max_length=3000)


class EvolutionBody(Contract):
    namespace_id: str
    funding_id: str
    target: Literal["teaching_skill", "routing_policy", "retrieval_recipe", "research_heuristic"]
    experience_ids: list[str] = Field(min_length=1, max_length=50)
    idempotency_key: str


class DatasetBody(Contract):
    namespace_id: str
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=40)
    cases: list[dict[str, Any]] = Field(min_length=1, max_length=1000)
    provenance: Literal["fixture", "human_gold"]
    annotators: list[str] = Field(default_factory=list)


class EvaluationBody(Contract):
    dataset_id: str


class PromoteBody(Contract):
    evaluation_id: str
    expected_revision: int = Field(ge=1)
    confirm: Literal["PROMOTE"]


class RollbackBody(Contract):
    namespace_id: str
    target: str
    expected_revision: int = Field(ge=1)
    confirm: Literal["ROLLBACK"]


class WorkspaceBody(Contract):
    files: dict[str, str] = Field(default_factory=dict)
    publication_ids: list[str] = Field(default_factory=list, max_length=20)
    assistance_mode: Literal["assisted", "independent"] = "assisted"


class FileBody(Contract):
    expected_revision: int = Field(ge=1)
    path: str
    content: str | None


class CodingBody(Contract):
    expected_revision: int = Field(ge=1)
    selected_files: list[str] = Field(default_factory=list, max_length=3)
    question: str = Field(min_length=1, max_length=10000)
    funding_id: str
    call_cap: int = Field(default=2, ge=1, le=4)
    idempotency_key: str


class ApplyBody(Contract):
    expected_revision: int = Field(ge=1)
    confirm: Literal["APPLY"]


class CommandBody(Contract):
    expected_revision: int = Field(ge=1)
    argv: list[str] = Field(min_length=1, max_length=30)


class ExecuteBody(Contract):
    approval_nonce: str
    confirm: Literal["RUN"]


class SubmissionBody(Contract):
    expected_revision: int
    explanation: str = Field(min_length=1, max_length=10000)
    transfer_notes: str = Field(min_length=1, max_length=10000)
    external_help: Literal["none_declared", "used", "unknown"] = "unknown"
    idempotency_key: str


class SubmissionReviewBody(Contract):
    checks: dict[str, bool]
    confirm: Literal["REVIEW"]


class PlanBody(Contract):
    publication_ids: list[str] = Field(min_length=1, max_length=20)
    learning_intent: Literal["build_project", "learn_project", "technical_design", "function"]
    title: str = Field(min_length=1, max_length=200)


class PlanReviseBody(Contract):
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    module_ids: list[str] = Field(min_length=1, max_length=20)


class PlanAcceptBody(Contract):
    version_id: str
    expected_revision: int


class ObservationBody(Contract):
    module_id: str
    response: str = Field(min_length=1, max_length=10000)
    rubric: dict[str, bool]
    help_mode: Literal["assisted", "self_declared_independent", "unknown"]


class UserBody(Contract):
    name: str = Field(min_length=1, max_length=120)


class GrantBody(Contract):
    principal_id: str
    role: Literal["reader", "writer", "admin"]
    active: bool


class RecheckBody(Contract):
    publication_id: str
    funding_id: str
    idempotency_key: str


class ResolveDisputeBody(Contract):
    publication_id: str
    outcome: Literal["uphold", "revoke", "correct"]
    review_id: str
    reason: str = Field(min_length=1, max_length=3000)
    replacement_id: str | None = None


class UpdatePreviewBody(Contract):
    old_source_id: str
    new_source_id: str


class GapBuildBody(Contract):
    contract_version: Literal["exact-question-gap-schedule-1.6.6"] = (
        "exact-question-gap-schedule-1.6.6"
    )
    funding_id: str
    call_cap: int = Field(default=24, ge=8, le=100)
    idempotency_key: str = Field(min_length=1, max_length=150)
    source_id: str | None = None
    max_projects: Literal[1] = 1
    max_units_per_project: int = Field(default=2, ge=1, le=3)
    duration_minutes: int = Field(default=30, ge=1, le=10080, strict=True)
    planning_call_seconds: int = Field(default=120, ge=1, le=3600, strict=True)
    auto_reanswer: bool = False
    confirm: Literal["BUILD"]


class GapReconcileBody(Contract):
    answer_turn_id: str | None = None
    confirm: Literal["RECONCILE"]


class GapReanswerBody(Contract):
    funding_id: str = Field(min_length=1, max_length=180)
    confirm: Literal["REANSWER"]


class RolloutBody(Contract):
    evaluation_id: str
    expected_revision: int = Field(ge=1)
    traffic_percent: int = Field(default=10, ge=1, le=50)
    minimum_per_arm: int = Field(default=10, ge=1, le=1000)
    maximum_turns: int = Field(default=200, ge=2, le=10000)
    confirm: Literal["CANARY"]


def create_app(service: Application | None = None) -> FastAPI:
    service = service or Application(Settings.from_env())
    app = FastAPI(
        title="CareerForge",
        version=__version__,
        description="CLI-first evidence-grounded learning; see validation report for verified scope.",
    )
    app.state.services = service

    def actor(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise DomainError("UNAUTHENTICATED", "需要Bearer访问令牌。", 401)
        with service.db.read() as db:
            return authenticate(db, authorization[7:]).id

    def owned_run(db: Session, actor_id: str, identifier: str) -> AgentRun:
        assert_personal_access(db, actor_id)
        run = db.get(AgentRun, identifier)
        if not run or run.principal_id != actor_id:
            raise DomainError("NOT_FOUND", "任务不存在。", 404)
        namespace_access(db, actor_id, run.namespace_id)
        return run

    def run_body_access(
        db: Session, run: AgentRun, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        # Only documented, server-validated reference fields confer authority.
        # Do not infer IDs by searching untrusted prose or model output strings.
        roots: set[str] = set()
        for value in (run.input_manifest, payload or {}):
            snapshot = value.get("goal_snapshot")
            if isinstance(snapshot, dict):
                from .services.research_coverage import validate_goal_snapshot

                validate_goal_snapshot(db, run.principal_id, snapshot)
                roots.add(snapshot["goal_version_id"])
            for key in (
                "source_id",
                "jd_revision_id",
                "retrieval_manifest_id",
                "selection_manifest_id",
            ):
                if isinstance(value.get(key), str):
                    roots.add(value[key])
            for key in (
                "source_ids",
                "publication_ids",
                "unit_version_ids",
                "review_ids",
                "access_dependencies",
            ):
                roots.update(item for item in value.get(key, []) if isinstance(item, str))
            for item in value.get("knowledge", []):
                if isinstance(item, dict) and isinstance(item.get("publication_id"), str):
                    roots.add(item["publication_id"])
            roots.update(
                identifier
                for key, version in value.get("taxonomy_versions", {}).items()
                for identifier in (topic_object_id(key, version), facet_object_id(key, version))
            )
            update_scope = value.get("update_scope")
            if isinstance(update_scope, dict):
                for key in ("old_source_id", "new_source_id"):
                    if isinstance(update_scope.get(key), str):
                        roots.add(update_scope[key])
                roots.update(
                    unit["publication_id"]
                    for unit in update_scope.get("units", [])
                    if isinstance(unit.get("publication_id"), str)
                )
        observations: list[dict[str, Any]] = list(
            db.scalars(
                select(SearchObservation.payload)
                .join(ResearchNode)
                .where(ResearchNode.run_id == run.id)
            )
        )
        for observation in observations:
            roots.update(
                item for item in observation.get("access_dependencies", []) if isinstance(item, str)
            )
        check_closure(db, run.principal_id, sorted(roots))
        if run.input_manifest.get("research_contract_version"):
            from .services.research_coverage import validate_research_run

            validate_research_run(db, run.principal_id, run)
            for identifier in (payload or {}).get("publication_ids", []):
                service.knowledge.publication_pack(db, run.principal_id, identifier)
        if run.workflow == "learning_plan" and payload is not None:
            return service.learning.validate_plan_result(db, run.principal_id, run.id, payload)
        if run.workflow == "jd_extract" and payload is not None:
            return service.jd.validate_result(db, run.principal_id, run, payload)
        if run.workflow == "learning_assessment" and payload is not None:
            return service.assessments.validate_result(db, run.principal_id, run, payload)
        if (
            run.workflow == "knowledge_build"
            and payload is not None
            and run.input_manifest.get("project_analysis_contract_version")
        ):
            projection = service.build.project_analysis.project_in(
                db, run.principal_id, run, payload
            )
            if any(payload.get(key) != value for key, value in projection.items()):
                raise DomainError(
                    "PROJECT_ANALYSIS_BINDING", "保存的项目结构投影与固定证据不一致。", 409
                )
            return {**payload, **projection}
        return payload

    @app.exception_handler(DomainError)
    async def business_error(request, error):
        return JSONResponse(
            status_code=error.status,
            content={"error": {"code": error.code, "message": error.message}},
        )

    @app.middleware("http")
    async def telemetry(request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        HTTP_LATENCY.labels(route, request.method, str(response.status_code)).observe(
            time.monotonic() - start
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health/live")
    def live():
        return {"status": "alive", "version": __version__}

    @app.get("/health/ready")
    def ready():
        try:
            # SELECT 1 alone falsely marks an empty/unmigrated database ready.
            # Accounts are initialized explicitly, not as a readiness side effect.
            with service.db.read() as db:
                db.execute(select(Principal.id).limit(1))
                from .db import schema_is_current

                if not schema_is_current(db.get_bind()):
                    return JSONResponse(
                        status_code=503, content={"database": "unavailable_or_unmigrated"}
                    )
                from .models_privacy_ext import PrivacyState

                if (
                    db.scalar(
                        select(Principal.id)
                        .outerjoin(PrivacyState, PrivacyState.principal_id == Principal.id)
                        .where(PrivacyState.principal_id.is_(None))
                        .limit(1)
                    )
                    is not None
                ):
                    return JSONResponse(
                        status_code=503, content={"database": "unavailable_or_unmigrated"}
                    )
        except SQLAlchemyError:
            return JSONResponse(status_code=503, content={"database": "unavailable_or_unmigrated"})
        return {
            "database": "ready",
            "mode": service.settings.mode,
            "model_configured": bool(service.settings.model_name),
            "synthetic_provider": bool(service.provider.synthetic),
            "production_acceptance_claimed": False,
        }

    @app.get("/metrics")
    def metrics(user=Depends(actor)):
        with service.db.read() as db:
            if not principal(db, user).is_admin:
                raise DomainError("FORBIDDEN", "监控端点需管理员。", 403)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/v1/me")
    def me(user=Depends(actor)):
        with service.db.read() as db:
            p = principal(db, user)
            ns = private_namespace(db, user)
            return {
                "principal_id": p.id,
                "name": p.name,
                "namespace_id": ns.id,
                "is_admin": p.is_admin,
            }

    @app.post("/api/v1/admin/users", status_code=201)
    def users(body: UserBody, user=Depends(actor)):
        with service.db.transaction() as db:
            if not principal(db, user).is_admin:
                raise DomainError("FORBIDDEN", "创建用户需要系统管理员。", 403)
            p, ns, token = create_principal(db, body.name)
            return {"principal_id": p.id, "namespace_id": ns.id, "token": token}

    @app.put("/api/v1/namespaces/{identifier}/grants")
    def grants(identifier: str, body: GrantBody, user=Depends(actor)):
        with service.db.transaction() as db:
            set_grant(db, user, identifier, body.principal_id, body.role, body.active)
        return {"updated": True}

    @app.post("/api/v1/funding", status_code=201)
    def funding(body: FundingBody, user=Depends(actor)):
        # This package has one deployment-owned credential set, so only the admin can authorize spend.
        with service.db.read() as db:
            if not principal(db, user).is_admin:
                raise DomainError(
                    "SPENDING_NOT_GRANTED", "实例凭证由管理员管理；普通用户不能自授付费额度。", 403
                )
        return {
            "funding_id": service.runtime.create_funding(
                user,
                body.namespace_id,
                body.call_cap,
                body.allowed_roles,
                body.hours,
                body.data_classes,
                body.allowed_source_ids,
            )
        }

    @app.delete("/api/v1/funding/{identifier}")
    def revoke_funding(identifier: str, user=Depends(actor)):
        service.runtime.revoke_funding(user, identifier)
        return {"revoked": True}

    @app.post("/api/v1/sources", status_code=201)
    def import_source(body: ImportSourceInput, user=Depends(actor)):
        return {"source_id": service.sources.import_source(user, body)}

    @app.get("/api/v1/sources/{identifier}")
    def source_detail(identifier: str, user=Depends(actor)):
        return service.sources.describe(user, identifier)

    @app.post("/api/v1/knowledge/builds", status_code=202)
    def builds(body: KnowledgeBuildInput, user=Depends(actor)):
        return {"run_id": service.build.start(user, body)}

    @app.get("/api/v1/knowledge/builds/{identifier}/project-analysis")
    def project_analysis(identifier: str, user=Depends(actor)):
        with service.db.read() as db:
            run = owned_run(db, user, identifier)
            result = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run.id, StageArtifact.stage_key == "result"
                )
            )
            if result is None:
                raise DomainError("PROJECT_ANALYSIS_PENDING", "构建尚无结果，未追加模型调用。", 409)
            if result.input_hash != run.input_hash:
                raise DomainError("PROJECT_ANALYSIS_BINDING", "构建结果与固定输入不一致。", 409)
            payload = run_body_access(db, run, result.payload)
            return service.build.project_analysis.project_in(db, user, run, payload)

    @app.get("/api/v1/runs")
    def runs(user=Depends(actor)):
        with service.db.read() as db:
            assert_personal_access(db, user)
            return [
                {
                    "id": r.id,
                    "workflow": r.workflow,
                    "status": r.status,
                    "revision": r.revision,
                    "parent_id": r.parent_id,
                }
                for r in db.scalars(
                    select(AgentRun)
                    .where(AgentRun.principal_id == user)
                    .order_by(AgentRun.created_at.desc())
                    .limit(100)
                )
            ]

    @app.get("/api/v1/runs/{identifier}")
    def run_detail(identifier: str, user=Depends(actor)):
        with service.db.read() as db:
            r = owned_run(db, user, identifier)
            result = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == r.id, StageArtifact.stage_key == "result"
                )
            )
            projected_result = run_body_access(db, r, result.payload if result else None)
            interrupted_coverage = (
                service.build.interrupted_coverage_in(db, r) if projected_result is None else None
            )
            return {
                "run_id": r.id,
                "workflow": r.workflow,
                "status": r.status,
                "revision": r.revision,
                "epoch": r.epoch,
                "parent_id": r.parent_id,
                "call_cap": r.call_cap,
                "calls_consumed": r.calls_consumed,
                "held_calls": r.held_calls,
                "result": projected_result,
                "deadline_at": r.deadline_at,
                "duration_minutes": r.input_manifest.get("duration_minutes"),
                "execution_plan": r.input_manifest.get("execution_plan"),
                "checkpoint": service.build.checkpoint_in(db, user, r),
                **(
                    {"coordinator_coverage_decision": interrupted_coverage}
                    if interrupted_coverage
                    else {}
                ),
            }

    @app.get("/api/v1/runs/{identifier}/graph")
    async def graph_checkpoint(identifier: str, user=Depends(actor)):
        with service.db.read() as db:
            run = owned_run(db, user, identifier)
            run_body_access(db, run, None)
        if run.workflow_engine == "legacy":
            return {"engine": "legacy", "checkpoint_exists": False}
        from .workflows.runtime_adapter import GraphRuntimeAdapter

        adapter = getattr(service, "graph_runtime", None)
        if adapter is None:
            adapter = service.graph_runtime = GraphRuntimeAdapter(service)
        try:
            return {"engine": "langgraph", **await adapter.summary(run)}
        except DomainError:
            raise
        except Exception:
            raise DomainError(
                "GRAPH_CHECKPOINT_UNAVAILABLE", "Checkpoint 暂不可用。", 503
            ) from None

    @app.get("/api/v1/runs/{identifier}/events")
    def run_events(identifier: str, after: int = 0, user=Depends(actor)):
        if after < 0:
            raise DomainError("CURSOR", "游标无效。", 422)
        with service.db.read() as db:
            r = owned_run(db, user, identifier)
            run_body_access(db, r)
            return [
                {"seq": e.seq, "type": e.event_type, "payload": e.payload}
                for e in db.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == identifier, RunEvent.seq > after)
                    .order_by(RunEvent.seq)
                    .limit(500)
                )
            ]

    @app.get("/api/v1/runs/{identifier}/research-tree")
    def tree(identifier: str, user=Depends(actor)):
        with service.db.read() as db:
            r = owned_run(db, user, identifier)
            run_body_access(db, r)
            return [
                {
                    "node_id": n.id,
                    "parent_id": n.parent_id,
                    "depth": n.depth,
                    "question": n.question,
                    "status": n.status,
                    "payload": n.payload,
                }
                for n in db.scalars(
                    select(ResearchNode)
                    .where(ResearchNode.run_id == identifier)
                    .order_by(ResearchNode.depth)
                )
            ]

    @app.post("/api/v1/runs/{identifier}/cancel")
    def cancel(identifier: str, body: CancelBody, user=Depends(actor)):
        service.runtime.cancel(user, identifier, body.expected_revision)
        with service.db.read() as db:
            r = owned_run(db, user, identifier)
            turn_id = r.input_manifest.get("turn_id") if r.workflow == "chat" else None
        if isinstance(turn_id, str) and turn_id:
            service.conversations.mark_failed(turn_id, "USER_CANCELLED")
        return {"status": "cancelled"}

    @app.post("/api/v1/conversations", status_code=201)
    def conversation(body: ConversationBody, user=Depends(actor)):
        return service.conversations.detail(
            user, service.conversations.create(user, body.auto_build, body.build_call_cap)
        )

    @app.post("/api/v2/conversations/answer-feedback", status_code=201)
    def answer_feedback_conversation(body: AnswerFeedbackConversationInput, user=Depends(actor)):
        return service.conversations.detail(
            user, service.conversations.create_answer_feedback(user, body)
        )

    @app.get("/api/v1/conversations/{identifier}")
    def conversation_detail(identifier: str, user=Depends(actor)):
        return service.conversations.detail(user, identifier)

    @app.post("/api/v2/conversations/{identifier}/checker-contract-adoptions")
    def adopt_checker_contract(
        identifier: str, body: AdoptConversationCheckerInput, user=Depends(actor)
    ):
        return service.conversations.adopt_checker_contract(user, identifier, body)

    @app.post("/api/v1/conversations/{identifier}/turns", status_code=202)
    async def turns(identifier: str, body: TurnInput, user=Depends(actor)):
        result = service.conversations.submit(user, identifier, body)
        if result["run_id"] is None:
            try:
                await service.conversations.process(result["turn_id"])
            except DomainError as exc:
                service.conversations.mark_failed(result["turn_id"], exc.code)
                raise
        return result

    @app.post("/api/v1/conversations/{identifier}/session-tutor-turns", status_code=202)
    async def session_tutor_turns(
        identifier: str, body: SessionTutorTurnInput, user=Depends(actor)
    ):
        result = service.conversations.submit_session(user, identifier, body)
        if result["run_id"] is None:
            try:
                await service.conversations.process(result["turn_id"])
            except DomainError as exc:
                service.conversations.mark_failed(result["turn_id"], exc.code)
                raise
        return result

    @app.post("/api/v1/conversations/{identifier}/guided-turns", status_code=202)
    async def guided_turns(identifier: str, body: GuidedTutorTurnInput, user=Depends(actor)):
        result = service.conversations.submit_guided(user, identifier, body)
        if result["run_id"] is None and result.get("status") != "completed":
            try:
                await service.conversations.process(result["turn_id"])
            except DomainError as exc:
                service.conversations.mark_failed(result["turn_id"], exc.code)
                raise
        return result

    @app.get("/api/v1/learning/teaching-sessions/{identifier}")
    def teaching_session(identifier: str, user=Depends(actor)):
        return service.conversations.teaching_session(user, identifier)

    @app.post("/api/v1/learning/session-module-acks", status_code=201)
    def acknowledge_session_module(body: SessionModuleAckInput, user=Depends(actor)):
        return service.conversations.acknowledge_module(user, body)

    @app.get("/api/v1/turns/{identifier}/session-tutor-binding")
    def session_tutor_binding(identifier: str, user=Depends(actor)):
        return service.conversations.session_binding(user, identifier)

    @app.get("/api/v1/turns/{identifier}")
    def turn_detail(identifier: str, user=Depends(actor)):
        return service.answers.events(user, identifier)

    @app.get("/api/v1/turns/{identifier}/events")
    async def turn_stream(
        identifier: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None),
        user=Depends(actor),
    ):
        if last_event_id and (not last_event_id.isascii() or not last_event_id.isdecimal()):
            raise DomainError("CURSOR", "事件游标无效。", 422)
        try:
            cursor = max(after, int(last_event_id or 0))
        except ValueError as exc:
            raise DomainError("CURSOR", "事件游标无效。", 422) from exc
        service.answers.events(user, identifier, cursor)

        async def stream():
            nonlocal cursor
            while True:
                if await request.is_disconnected():
                    return
                packet = service.answers.events(user, identifier, cursor)
                for e in packet["events"]:
                    cursor = e["seq"]
                    yield f"id: {cursor}\nevent: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                if packet["status"] in {"completed", "interrupted", "blocked"}:
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/knowledge/query")
    async def query(body: RetrievalTaskSpec, user=Depends(actor)):
        return await service.retrieval.retrieve(user, body)

    @app.get("/api/v1/knowledge/topics/{key}/projects")
    def topic_projects(key: str, cursor: str | None = None, limit: int = 20, user=Depends(actor)):
        return service.retrieval.directory(user, key, cursor, limit)

    @app.get("/api/v1/knowledge/publications/{identifier}")
    def publication(identifier: str, user=Depends(actor)):
        with service.db.read() as db:
            return service.knowledge.publication_pack(db, user, identifier)

    @app.post("/api/v1/knowledge/select")
    def select_knowledge(body: SelectHeadBody, user=Depends(actor)):
        return {
            "selection_head_id": service.knowledge.select_head(
                user, body.selection_namespace_id, body.publication_id, body.expected_revision
            )
        }

    @app.post("/api/v1/knowledge/objects/{identifier}/invalidate")
    def invalidate_object(identifier: str, body: InvalidateBody, user=Depends(actor)):
        with service.db.transaction() as db:
            row = invalidate(db, user, identifier, body.state, body.reason)
            return {"object_id": row.id, "state": row.state, "revision": row.revision}

    @app.get("/api/v1/knowledge/impacts")
    def impacts(user=Depends(actor)):
        with service.db.read() as db:
            ns = private_namespace(db, user)
            return [
                {
                    "source_id": i.source_id,
                    "consumer_id": i.consumer_id,
                    "generation": i.generation,
                    "enforcement": i.enforcement,
                }
                for i in db.scalars(select(Impact).where(Impact.namespace_id == ns.id).limit(200))
            ]

    @app.post("/api/v1/knowledge/disputes", status_code=201)
    def disputes(body: DisputeBody, user=Depends(actor)):
        return service.evolution.dispute(user, body.target_id, body.reason)

    @app.get("/api/v1/knowledge/disputes")
    def dispute_list(user=Depends(actor)):
        with service.db.read() as db:
            assert_personal_access(db, user)
            return [
                {
                    "dispute_id": d.id,
                    "target_id": d.target_id,
                    "status": d.status,
                    "reason": d.reason,
                }
                for d in db.scalars(
                    select(Dispute)
                    .where(Dispute.principal_id == user)
                    .order_by(Dispute.created_at.desc())
                    .limit(100)
                )
            ]

    @app.post("/api/v1/knowledge/disputes/{identifier}/recheck", status_code=202)
    def recheck_dispute(identifier: str, body: RecheckBody, user=Depends(actor)):
        return service.lifecycle.recheck(
            user, identifier, body.publication_id, body.funding_id, body.idempotency_key
        )

    @app.post("/api/v1/knowledge/disputes/{identifier}/resolve")
    def resolve_dispute(identifier: str, body: ResolveDisputeBody, user=Depends(actor)):
        return service.lifecycle.resolve(
            user,
            identifier,
            body.publication_id,
            body.outcome,
            body.review_id,
            body.reason,
            body.replacement_id,
        )

    @app.post("/api/v1/knowledge/update-previews")
    def update_preview(body: UpdatePreviewBody, user=Depends(actor)):
        return service.lifecycle.preview_update(user, body.old_source_id, body.new_source_id)

    @app.get("/api/v1/knowledge/gaps")
    def gap_list(user=Depends(actor)):
        from .models import KnowledgeGap

        with service.db.read() as db:
            assert_personal_access(db, user)
            return [
                {
                    "gap_id": g.id,
                    "turn_id": g.turn_id,
                    "topic_keys": g.topic_keys,
                    "source_ids": g.source_ids,
                    "reason_codes": g.reason_codes,
                    "status": g.status,
                    "build_run_id": g.build_run_id,
                }
                for g in db.scalars(
                    select(KnowledgeGap)
                    .where(KnowledgeGap.principal_id == user)
                    .order_by(KnowledgeGap.created_at.desc())
                    .limit(100)
                )
            ]

    @app.post("/api/v1/knowledge/gaps/{identifier}/schedule", status_code=202)
    def schedule_gap(identifier: str, body: GapBuildBody, user=Depends(actor)):
        return service.lifecycle.schedule_gap(
            user,
            identifier,
            body.funding_id,
            body.call_cap,
            body.idempotency_key,
            contract_version=body.contract_version,
            source_id=body.source_id,
            max_projects=body.max_projects,
            max_units_per_project=body.max_units_per_project,
            duration_minutes=body.duration_minutes,
            planning_call_seconds=body.planning_call_seconds,
            auto_reanswer=body.auto_reanswer,
        )

    @app.post("/api/v1/knowledge/gaps/{identifier}/reconcile")
    def reconcile_gap(identifier: str, body: GapReconcileBody, user=Depends(actor)):
        return service.lifecycle.reconcile_gap(user, identifier, body.answer_turn_id)

    @app.post("/api/v1/knowledge/gaps/{identifier}/reanswer", status_code=202)
    def reanswer_gap(identifier: str, body: GapReanswerBody, user=Depends(actor)):
        return service.lifecycle.reanswer_gap(user, identifier, body.funding_id)

    @app.delete("/api/v1/feedback/{identifier}/consent")
    def revoke_feedback(identifier: str, user=Depends(actor)):
        return service.evolution.revoke_feedback(user, identifier)

    @app.post("/api/v1/evolution/candidates/{identifier}/canary", status_code=201)
    def start_canary(identifier: str, body: RolloutBody, user=Depends(actor)):
        return service.rollouts.start(
            user,
            identifier,
            body.evaluation_id,
            body.expected_revision,
            body.traffic_percent,
            body.minimum_per_arm,
            body.maximum_turns,
        )

    @app.get("/api/v1/evolution/canaries/{identifier}")
    def canary_detail(identifier: str, user=Depends(actor)):
        return service.rollouts.detail(user, identifier)

    @app.post("/api/v1/evolution/canaries/{identifier}/stop")
    def stop_canary(identifier: str, user=Depends(actor)):
        return service.rollouts.stop(user, identifier)

    @app.post("/api/v1/feedback", status_code=201)
    def feedback(body: FeedbackInput, user=Depends(actor)):
        return service.evolution.feedback(user, body)

    @app.post("/api/v2/feedback/answer-continuation", status_code=201)
    def answer_continuation_feedback(body: AnswerContinuationFeedbackInput, user=Depends(actor)):
        return service.evolution.feedback_for_answer(user, body)

    @app.get("/api/v1/evolution/candidates")
    def candidate_list(user=Depends(actor)):
        with service.db.read() as db:
            ns = private_namespace(db, user)
            return [
                {
                    "candidate_id": c.id,
                    "target": c.target,
                    "base_policy_id": c.base_policy_id,
                    "status": c.status,
                    "content": c.content,
                }
                for c in db.scalars(
                    select(EvolutionCandidate)
                    .where(EvolutionCandidate.namespace_id == ns.id)
                    .order_by(EvolutionCandidate.created_at.desc())
                    .limit(100)
                )
            ]

    @app.get("/api/v1/evolution/history")
    def evolution_history(user=Depends(actor)):
        with service.db.read() as db:
            ns = private_namespace(db, user)
            return [
                {
                    "evaluation_id": e.id,
                    "candidate_id": e.candidate_id,
                    "decision": e.decision,
                    "report": e.report,
                }
                for e in db.scalars(
                    select(EvolutionEvaluation)
                    .join(EvolutionCandidate)
                    .where(EvolutionCandidate.namespace_id == ns.id)
                    .order_by(EvolutionEvaluation.created_at.desc())
                    .limit(100)
                )
            ]

    @app.post("/api/v1/conversations/{conversation_id}/evolution", status_code=202)
    def evolution_command(conversation_id: str, body: EvolutionSessionInput, user=Depends(actor)):
        return service.evolution_workflows.submit(user, conversation_id, body)

    @app.get("/api/v1/conversations/{conversation_id}/evolution/{workflow_id}")
    def evolution_workflow_status(conversation_id: str, workflow_id: str, user=Depends(actor)):
        return service.evolution_workflows.detail(user, conversation_id, workflow_id)

    @app.get("/api/v1/evolution/runs/{run_id}/history")
    def evolution_run_history(run_id: str, user=Depends(actor)):
        from .services.evolution_stages import historical_summary

        return historical_summary(service, user, run_id)

    @app.post("/api/v1/evolution/candidates", status_code=202)
    def candidate(body: EvolutionBody, user=Depends(actor)):
        return {
            "run_id": service.evolution.start_candidate(
                user,
                body.namespace_id,
                body.funding_id,
                body.experience_ids,
                body.target,
                body.idempotency_key,
            )
        }

    @app.post("/api/v1/evolution/datasets", status_code=201)
    def datasets(body: DatasetBody, user=Depends(actor)):
        return {
            "dataset_id": service.evolution.register_dataset(
                user,
                body.namespace_id,
                body.name,
                body.version,
                body.cases,
                body.provenance,
                body.annotators,
            )
        }

    @app.post("/api/v1/evolution/candidates/{identifier}/evaluate")
    async def evaluate(identifier: str, body: EvaluationBody, user=Depends(actor)):
        return await service.evolution.evaluate(user, identifier, body.dataset_id)

    @app.post("/api/v1/evolution/candidates/{identifier}/promote")
    def promote(identifier: str, body: PromoteBody, user=Depends(actor)):
        return service.evolution.promote(
            user, identifier, body.evaluation_id, body.expected_revision
        )

    @app.post("/api/v1/evolution/rollback")
    def rollback(body: RollbackBody, user=Depends(actor)):
        return service.evolution.rollback(
            user, body.namespace_id, body.target, body.expected_revision
        )

    @app.get("/api/v1/policies")
    def policies(user=Depends(actor)):
        with service.db.transaction() as db:
            ns = private_namespace(db, user)
            return ensure_policies(db, user, ns.id)

    @app.post("/api/v1/plans", status_code=201)
    def plan(body: PlanBody, user=Depends(actor)):
        return service.learning.propose_plan(
            user, body.publication_ids, body.learning_intent, body.title
        )

    @app.get("/api/v1/plans/{identifier}")
    def plan_detail(identifier: str, user=Depends(actor)):
        return service.learning.detail(user, identifier)

    @app.post("/api/v1/plans/{identifier}/revision-proposals", status_code=201)
    def revise_plan(identifier: str, body: PlanReviseBody, user=Depends(actor)):
        return service.learning.revise_plan(
            user, identifier, body.expected_revision, body.reason, body.module_ids
        )

    @app.post("/api/v1/plans/{identifier}/accept")
    def accept_plan(identifier: str, body: PlanAcceptBody, user=Depends(actor)):
        return service.learning.accept_plan(
            user, identifier, body.version_id, body.expected_revision
        )

    @app.post("/api/v1/learning/observations", status_code=201)
    def observation(body: ObservationBody, user=Depends(actor)):
        return service.learning.observe(
            user, body.module_id, body.response, body.rubric, body.help_mode
        )

    @app.post("/api/v1/workspaces", status_code=201)
    def workspace(body: WorkspaceBody, user=Depends(actor)):
        return service.coding.create_workspace(
            user, body.files, body.publication_ids, body.assistance_mode
        )

    @app.get("/api/v1/workspaces/{identifier}")
    def workspace_detail(identifier: str, user=Depends(actor)):
        return service.coding.detail(user, identifier)

    @app.put("/api/v1/workspaces/{identifier}/file")
    def save_file(identifier: str, body: FileBody, user=Depends(actor)):
        return service.coding.save(
            user, identifier, body.expected_revision, body.path, body.content
        )

    @app.post("/api/v1/workspaces/{identifier}/coding", status_code=202)
    def coding(identifier: str, body: CodingBody, user=Depends(actor)):
        return {
            "run_id": service.coding.start(
                user,
                identifier,
                body.expected_revision,
                body.selected_files,
                body.question,
                body.funding_id,
                body.call_cap,
                body.idempotency_key,
            )
        }

    @app.get("/api/v1/workspaces/{identifier}/patches/{patch_id}")
    def patch_detail(identifier: str, patch_id: str, user=Depends(actor)):
        with service.db.read() as db:
            service.coding._workspace(db, user, identifier)
            patch = db.get(PatchProposal, patch_id)
            if not patch or patch.workspace_id != identifier:
                raise DomainError("NOT_FOUND", "补丁不存在。", 404)
            return {
                "proposal_id": patch.id,
                "expected_revision": patch.expected_revision,
                "edits": patch.edits,
                "applied_revision": patch.applied_revision,
            }

    @app.post("/api/v1/workspaces/{identifier}/patches/{patch_id}/apply")
    def apply_patch(identifier: str, patch_id: str, body: ApplyBody, user=Depends(actor)):
        return service.coding.apply(
            user, identifier, patch_id, body.expected_revision, body.confirm
        )

    @app.post("/api/v1/workspaces/{identifier}/commands", status_code=201)
    def command(identifier: str, body: CommandBody, user=Depends(actor)):
        return service.coding.propose_command(user, identifier, body.expected_revision, body.argv)

    @app.post("/api/v1/commands/{identifier}/execute", status_code=202)
    def execute_command(identifier: str, body: ExecuteBody, user=Depends(actor)):
        return service.coding.confirm_command(user, identifier, body.approval_nonce, body.confirm)

    @app.get("/api/v1/commands/{identifier}")
    def command_result(identifier: str, user=Depends(actor)):
        return service.coding.command_result(user, identifier)

    @app.post("/api/v1/workspaces/{identifier}/submissions", status_code=201)
    def submit(identifier: str, body: SubmissionBody, user=Depends(actor)):
        return service.coding.submit(
            user,
            identifier,
            body.expected_revision,
            body.explanation,
            body.transfer_notes,
            body.external_help,
            body.idempotency_key,
        )

    @app.post("/api/v1/submissions/{identifier}/review")
    def review_submission(identifier: str, body: SubmissionReviewBody, user=Depends(actor)):
        return service.coding.review_submission(user, identifier, body.checks, body.confirm)

    @app.post("/api/v1/workspaces/{identifier}/close")
    def close(identifier: str, user=Depends(actor)):
        return service.coding.close_attempt(user, identifier)

    from .routes.assessment import register_routes as register_assessment
    from .routes.compat import register_routes as register_compat
    from .routes.governance import register_routes as register_governance
    from .routes.jd import register_routes as register_jd
    from .routes.learning_ext import register_routes as register_learning
    from .routes.privacy import register_routes as register_privacy
    from .routes.retrieval_ext import register_routes as register_retrieval
    from .routes.runtime_ext import register_routes as register_runtime

    register_compat(app, service, actor)
    register_assessment(app, service, actor)
    register_governance(app, service.governance, actor)
    register_jd(app, service, actor)
    register_learning(app, service, actor)
    register_privacy(app, service, actor)
    register_retrieval(app, service, actor)
    register_runtime(app, service, actor)
    from .routes.dialogue import register_dialogue_routes

    register_dialogue_routes(app, service, actor)
    return app
