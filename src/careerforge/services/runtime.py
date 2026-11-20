"""Single authoritative durable runtime: short transactions, outbox, leases and metering.

Mirrors CareerForge's existing transaction/epoch design; there is no Celery result store.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..db import Database, aware, clock, locked
from ..errors import DomainError, LeaseLost
from ..lease_heartbeat import LeaseHeartbeat, LeaseHeartbeats, check_heartbeat_stop
from ..models import (
    AgentRun,
    FundingAgreement,
    Idempotency,
    ModelReceipt,
    Namespace,
    Outbox,
    Principal,
    RunAllocation,
    RunEvent,
    StageArtifact,
    StreamEvent,
    Turn,
)
from ..util import fingerprint
from .run_pause import acknowledge_in, ensure_unpaused, park, pause_requested, stop_expired_worker
from .security import namespace_access, namespace_admin, principal

TERMINAL = {"succeeded", "partially_succeeded", "failed", "budget_stopped", "cancelled"}
WORKFLOWS = {
    "desk_intent",
    "desk_dialogue",
    "curation_validation",
    "project_discovery",
    "learning_assessment",
    "jd_extract",
    "knowledge_build",
    "analysis",
    "review",
    "research",
    "chat",
    "coding",
    "evolution",
    "retrieval_index",
    "learning_plan",
    "evolution_replay",
}
ROLES = {
    "extract",
    "planner",
    "reflector",
    "analysis",
    "reviewer",
    "coverage",
    "coordinator",
    "tutor",
    "answer_checker",
    "coding",
    "reflection",
    "embedding",
    "reranker",
    "evaluation",
    "query_rewriter",
    "learning_planner",
}
DATA_CLASSES = {"public_source", "private_source", "private_jd", "learning_state"}


def event(db: Session, run: AgentRun, kind: str, payload: dict[str, Any]) -> None:
    run.event_seq += 1
    db.add(RunEvent(run_id=run.id, seq=run.event_seq, event_type=kind, payload=payload))


def interrupt_turn(db: Session, turn_id: str, code: str, run_id: str | None = None) -> bool:
    """Persist one terminal stream event under the same turn lock as answer commit."""
    turn = locked(db, Turn, turn_id)
    if (
        turn is None
        or turn.status not in {"queued", "running"}
        or (run_id is not None and turn.run_id != run_id)
    ):
        return False
    turn.status = "interrupted"
    seq = (
        int(db.scalar(select(func.max(StreamEvent.seq)).where(StreamEvent.turn_id == turn.id)) or 0)
        + 1
    )
    db.add(
        StreamEvent(
            turn_id=turn.id, seq=seq, event_type="answer.interrupted", payload={"code": code}
        )
    )
    db.flush()
    return True


def lock_run(db: Session, run_id: str) -> AgentRun | None:
    preliminary = db.get(AgentRun, run_id)
    if preliminary is None:
        return None
    # Every runtime mutation follows funding -> root -> child, including revoke.
    # Taking the run first deadlocks against a funding revocation on PostgreSQL.
    # Funding/parent IDs are immutable parts of a run's creation contract.
    locked(db, FundingAgreement, preliminary.funding_id)
    if preliminary.parent_id:
        locked(db, AgentRun, preliminary.parent_id)
    return locked(db, AgentRun, run_id)


def is_public_system_run(db: Session, run: AgentRun) -> bool:
    """Conservative payer/data-scope classification, never subscriber consent.

    A system authorization may continue work on public inputs when its payer's
    personal data is being erased. The shared-build validator separately verifies
    its immutable source contract before any execution or business write.
    """
    funding = db.get(FundingAgreement, run.funding_id)
    namespace = db.get(Namespace, run.namespace_id)
    payer = db.get(Principal, run.principal_id)
    return bool(
        funding is not None
        and namespace is not None
        and payer is not None
        and payer.active
        and payer.is_admin
        and namespace.kind == "shared_public"
        and funding.kind == "system"
        and funding.namespace_id == namespace.id
        and funding.payer_id == run.principal_id
        and set(funding.allowed_data_classes) == {"public_source"}
    )


def declared_manifest_dependencies(manifest: dict[str, Any]) -> list[str]:
    """Bounded, named server contract fields, not arbitrary IDs mined from text."""
    from .taxonomy import facet_object_id, topic_object_id

    roots: set[str] = set()
    source_id = manifest.get("source_id")
    if source_id is not None:
        if not isinstance(source_id, str) or not source_id:
            raise DomainError("RUN_CONTRACT", "冻结来源身份无效。", 422)
        roots.add(source_id)
    source_ids = manifest.get("source_ids", [])
    if (
        not isinstance(source_ids, list)
        or len(source_ids) > 1024
        or any(not isinstance(identifier, str) or not identifier for identifier in source_ids)
    ):
        raise DomainError("RUN_CONTRACT", "冻结来源身份列表无效。", 422)
    roots.update(source_ids)
    jd_id = manifest.get("jd_revision_id")
    if jd_id is not None:
        if not isinstance(jd_id, str) or not jd_id:
            raise DomainError("RUN_CONTRACT", "冻结岗位版本身份无效。", 422)
        roots.add(jd_id)
    goal_id = manifest.get("goal_version_id")
    if goal_id is None and isinstance(manifest.get("goal_snapshot"), dict):
        goal_id = manifest["goal_snapshot"].get("goal_version_id")
    if goal_id is not None:
        if not isinstance(goal_id, str) or not goal_id:
            raise DomainError("RUN_CONTRACT", "冻结目标版本身份无效。", 422)
        roots.add(goal_id)
    versions = manifest.get("taxonomy_versions", {})
    if not isinstance(versions, dict) or len(versions) > 512:
        raise DomainError("RUN_CONTRACT", "冻结分类版本合同无效。", 422)
    for key, version in versions.items():
        if not isinstance(key, str) or type(version) is not int or version < 1:
            raise DomainError("RUN_CONTRACT", "冻结分类版本合同无效。", 422)
        roots.update([topic_object_id(key, version), facet_object_id(key, version)])
    if len(roots) > 1024:
        raise DomainError("RUN_CONTRACT", "冻结合同显式依赖超过单次1024个上限。", 422)
    return sorted(roots)


def active_funding(db: Session, run: AgentRun) -> FundingAgreement:
    funding = locked(db, FundingAgreement, run.funding_id)
    if funding is None or funding.status != "active" or aware(funding.expires_at) <= clock(db):
        raise DomainError("FUNDING_UNAVAILABLE", "费用授权已失效，停止后续调用。")
    from .privacy import assert_personal_access
    from .shared_builds import validate_shared_execution

    if not is_public_system_run(db, run):
        assert_personal_access(db, run.principal_id)
    namespace_access(db, run.principal_id, run.namespace_id, True)
    validate_shared_execution(db, run)
    return funding


def leased(db: Session, run_id: str, epoch: int, worker: str, allow_pause=False) -> AgentRun:
    run = lock_run(db, run_id)
    if (
        run is None
        or run.status != "running"
        or run.epoch != epoch
        or run.worker_id != worker
        or run.lease_until is None
        or aware(run.lease_until) <= clock(db)
        or aware(run.deadline_at) <= clock(db)
    ):
        raise LeaseLost()
    if not allow_pause:
        ensure_unpaused(db, run)
    from .session_turn import REQUEST_KEY, has_delivery_in

    if has_delivery_in(db, run):
        raise DomainError("FINALIZATION_ONLY", "回答已交付，只允许无付费调用的结算路径。", 409)
    from ..models_dialogue import WorkflowTutorRequest

    learning_request = db.scalar(
        select(WorkflowTutorRequest.id).where(WorkflowTutorRequest.run_id == run.id)
    )
    if (
        REQUEST_KEY in run.input_manifest
        or learning_request is not None
        or run.graph_version == "learning-tutor-v1"
    ):
        from .learning_requests import validate_delivered_request_in

        validate_delivered_request_in(
            db, run, run.input_manifest.get(REQUEST_KEY), allow_pause=allow_pause
        )
    from .workflow_practice import validate_run_in as validate_practice_run_in

    validate_practice_run_in(db, run, allow_pause=allow_pause)
    funding = active_funding(db, run)
    if run.input_manifest.get("evolution_graph") or run.graph_version in {
        "evolution-candidate-v1",
        "evolution-replay-v1",
    }:
        from .evolution_privacy import validate_run_generations_in

        validate_run_generations_in(db, run)
    from .feedback_target_v1 import validate_run_in as validate_feedback_run_in

    validate_feedback_run_in(db, run)
    if run.workflow == "chat":
        from .ordinary_checker import validate_ordinary_run_in
        from .session_tutor import validate_session_run_in

        if allow_pause and run.graph_version == "learning-tutor-v1":
            validate_session_run_in(db, run, allow_learning_pause=True)
        else:
            validate_session_run_in(db, run)
        validate_ordinary_run_in(db, run)
    if run.input_manifest.get("execution_contract_version"):
        from .build_execution import validate_execution_manifest

        validate_execution_manifest(run.input_manifest, run.call_cap)
    # A child cannot opt out of its parent's frozen goal scope by deleting a
    # marker. Legacy unmarked jobs still return immediately in the validator.
    if (
        run.input_manifest.get("research_contract_version")
        or run.input_manifest.get("goal_build_scope") is not None
        or run.workflow in {"knowledge_build", "analysis", "review", "research"}
    ):
        from .research_coverage import validate_research_run

        validate_research_run(db, run.principal_id, run)
    if run.workflow == "jd_extract":
        from .jd import JDService

        JDService.validate_run_in(db, run.principal_id, run)
    parent = None
    if run.parent_id:
        parent = db.get(AgentRun, run.parent_id)
        if parent is None or parent.status in TERMINAL or aware(parent.deadline_at) <= clock(db):
            raise LeaseLost()
        if run.workflow == "analysis" and (
            "coordinator_analysis_contract" in parent.input_manifest
            or "coordinator_analysis_contract" in run.input_manifest
        ):
            from .analysis_coordination import require_bound_brief
            from .sources import SourceService

            source_id = parent.input_manifest.get("source_id")
            if not isinstance(source_id, str):
                raise DomainError("ANALYSIS_BRIEF_SCOPE", "分析简报缺少固定来源。", 409)
            require_bound_brief(db, run, SourceService.get(db, run.principal_id, source_id))
    # Authorization/contract/dependency reads above can synchronously block.
    # Fence once more at their tail using the SAME locked records and a fresh
    # authoritative clock, without repeating those reads or extending a lease.
    now = clock(db)
    if (
        run.status != "running"
        or run.epoch != epoch
        or run.worker_id != worker
        or run.lease_until is None
        or aware(run.lease_until) <= now
        or aware(run.deadline_at) <= now
        or (parent is not None and (parent.status in TERMINAL or aware(parent.deadline_at) <= now))
    ):
        raise LeaseLost()
    if funding.status != "active" or aware(funding.expires_at) <= now:
        raise DomainError("FUNDING_UNAVAILABLE", "费用授权已失效，停止后续调用。")
    return run


def settle(db: Session, run: AgentRun) -> None:
    if run.budget_settled:
        return
    if run.parent_id:
        parent = locked(db, AgentRun, run.parent_id)
        allocation = db.scalar(
            select(RunAllocation).where(RunAllocation.child_id == run.id).with_for_update()
        )
        if allocation and not allocation.settled:
            parent.held_calls -= allocation.allocated - run.calls_consumed
            allocation.settled = True
        db.flush()
        remaining = db.scalars(select(AgentRun).where(AgentRun.parent_id == parent.id)).all()
        if parent.status == "waiting_children" and all(
            child.status in TERMINAL for child in remaining
        ):
            parent.status = "queued"
            parent.revision += 1
            event(db, parent, "task.children_ready", {})
            db.add(Outbox(kind="run", aggregate_id=parent.id, payload={}))
    else:
        funding = locked(db, FundingAgreement, run.funding_id)
        funding.calls_held -= run.call_cap - run.calls_consumed
        if funding.calls_held < 0:
            raise DomainError("BUDGET_INVARIANT", "额度结算不一致。", 500)
    run.budget_settled = True


def finish(db: Session, run: AgentRun, status: str, code: str | None = None) -> None:
    if status not in TERMINAL:
        raise DomainError("STATE", "无效的终止状态。", 500)
    if run.status in TERMINAL:
        return
    if not run.parent_id:
        children = db.scalars(
            select(AgentRun)
            .where(AgentRun.parent_id == run.id)
            .order_by(AgentRun.id)
            .with_for_update()
        ).all()
        for child in children:
            if child.status not in TERMINAL:
                child.epoch += 1
                finish(db, child, "cancelled", "PARENT_FINISHED")
    # The process may have disappeared after dispatch. Preserve the consumed
    # call and treat its outcome as unknown when settling an interrupted run.
    db.execute(
        update(ModelReceipt)
        .where(ModelReceipt.run_id == run.id, ModelReceipt.status == "attempted")
        .values(status="unknown")
    )
    run.status = status
    run.revision += 1
    run.worker_id = None
    run.lease_until = None
    event(db, run, "run.finished", {"status": status, "code": code})
    if run.workflow == "chat" and status in {"failed", "budget_stopped", "cancelled"}:
        turn_id = run.input_manifest.get("turn_id")
        if isinstance(turn_id, str):
            # Claims, reaping, budget failures and funding revocation all use
            # this transaction, not just the HTTP cancellation endpoint.
            interrupt_turn(db, turn_id, code or "RUN_" + status.upper(), run.id)
    settle(db, run)
    if run.parent_id is None:
        from ..models_dialogue import WorkflowRunLink

        linked = db.scalar(select(WorkflowRunLink.id).where(WorkflowRunLink.run_id == run.id))
        if linked:
            db.add(Outbox(kind="workflow.milestone", aggregate_id=run.id, payload={}))
    if run.pause_requested:
        db.add(
            Outbox(
                kind="workflow.pause_checkpoint", aggregate_id=run.parent_id or run.id, payload={}
            )
        )
    gap_schedule = run.input_manifest.get("gap_schedule") or {}
    if run.workflow == "knowledge_build" and gap_schedule.get("request", {}).get("auto_reanswer"):
        db.add(Outbox(kind="gap.auto", aggregate_id=run.id, payload={}))
    binding = run.input_manifest.get("gap_reanswer")
    if run.workflow == "chat" and binding:
        build = db.get(AgentRun, binding["build_run_id"])
        if build is not None:
            schedule = build.input_manifest.get("gap_schedule") or {}
            if schedule.get("request", {}).get("auto_reanswer"):
                db.add(Outbox(kind="gap.answer", aggregate_id=build.id, payload={}))


class Runtime:
    heartbeat_interval = 10.0
    current_model_profile: Callable[[], str] | None = None

    def __init__(self, database: Database):
        self.db = database
        self._heartbeats = LeaseHeartbeats()

    def create_funding(
        self,
        actor: str,
        namespace_id: str,
        call_cap: int,
        roles: list[str] | None = None,
        hours: int = 24,
        data_classes: list[str] | None = None,
        allowed_source_ids: list[str] | None = None,
    ) -> str:
        if not 1 <= call_cap <= 10000 or not 1 <= hours <= 168:
            raise DomainError("BUDGET", "额度或期限越界。", 422)
        with self.db.transaction() as s:
            ns = namespace_access(s, actor, namespace_id, True)
            if ns.kind == "shared_public":
                namespace_admin(s, actor, namespace_id)
                if not principal(s, actor).is_admin:
                    raise DomainError("SYSTEM_FUNDING", "公共构建只允许系统资助身份。", 403)
            # None requests defaults; [] is an explicit deny-all authorization.
            allowed = sorted(ROLES) if roles is None else list(roles)
            if not set(allowed) <= ROLES:
                raise DomainError("ROLE", "授权包含未知模型角色。", 422)
            classes = sorted(DATA_CLASSES) if data_classes is None else list(data_classes)
            if not set(classes) <= DATA_CLASSES:
                raise DomainError("DATA_CLASS", "授权包含未知数据外发类别。", 422)
            if ns.kind == "shared_public":
                classes = [item for item in classes if item == "public_source"]
            from .dependencies import check_closure
            from .sources import SourceService

            sources = [] if allowed_source_ids is None else list(allowed_source_ids)
            if len(sources) > 1024 or any(
                not isinstance(item, str) or not item for item in sources
            ):
                raise DomainError("FUNDING_SOURCE_SCOPE", "费用来源身份列表无效或过长。", 422)
            sources = sorted(set(sources))
            for identifier in sources:
                SourceService.get(s, actor, identifier)
            closure = check_closure(s, actor, sources)
            if ns.kind == "shared_public" and not closure.public_only:
                raise DomainError("PRIVATE_TO_PUBLIC", "系统公共授权不能包含私人来源。", 403)
            row = FundingAgreement(
                payer_id=actor,
                namespace_id=namespace_id,
                kind="system" if ns.kind == "shared_public" else "private",
                call_cap=call_cap,
                allowed_roles=allowed,
                allowed_source_ids=sources,
                allowed_data_classes=classes,
                expires_at=clock(s) + timedelta(hours=hours),
            )
            s.add(row)
            s.flush()
            return row.id

    def revoke_funding(self, actor: str, funding_id: str) -> None:
        with self.db.transaction() as s:
            row = locked(s, FundingAgreement, funding_id)
            if not row or row.payer_id != actor:
                raise DomainError("NOT_FOUND", "授权不存在。", 404)
            row.status = "revoked"
            # Issued requests retain their receipts; no follow-on business actions allowed.
            runs = s.scalars(
                select(AgentRun)
                .where(
                    AgentRun.funding_id == funding_id,
                    AgentRun.parent_id.is_(None),
                    AgentRun.status.notin_(TERMINAL),
                )
                .order_by(AgentRun.id)
            ).all()
            for run in runs:
                locked(s, AgentRun, run.id)
                run.epoch += 1
                finish(s, run, "cancelled", "FUNDING_REVOKED")

    def cancel_owned_in(self, db: Session, actor: str, reason: str = "PRIVACY_ERASURE") -> int:
        """Trusted erasure control plane; deliberately bypasses the personal gate.

        The erasure fence must already have committed. Lock all funding rows in
        deterministic order before roots, preserving runtime settlement ordering.
        Only actor-owned runs or work in their personal namespaces is changed;
        pure public-system roots are excluded. Children settle with their root.
        """
        candidates = db.execute(
            select(AgentRun.id, AgentRun.funding_id)
            .where(
                or_(
                    AgentRun.principal_id == actor,
                    AgentRun.namespace_id.in_(
                        select(Namespace.id).where(
                            Namespace.owner_id == actor, Namespace.kind != "shared_public"
                        )
                    ),
                ),
                AgentRun.parent_id.is_(None),
                AgentRun.status.notin_(TERMINAL),
            )
            .order_by(AgentRun.funding_id, AgentRun.id)
        ).all()
        for funding_id in sorted({row.funding_id for row in candidates}):
            locked(db, FundingAgreement, funding_id)
        cancelled = 0
        for candidate in candidates:
            run = locked(db, AgentRun, candidate.id)
            if run is not None and run.status not in TERMINAL and not is_public_system_run(db, run):
                run.epoch += 1
                finish(db, run, "cancelled", reason)
                cancelled += 1
        return cancelled

    def create(
        self,
        actor: str,
        namespace_id: str,
        funding_id: str,
        workflow: str,
        manifest: dict[str, Any],
        call_cap: int,
        key: str,
        minutes: int = 30,
    ) -> str:
        with self.db.transaction() as s:
            return self.create_in(
                s, actor, namespace_id, funding_id, workflow, manifest, call_cap, key, minutes
            ).id

    def create_in(
        self,
        s: Session,
        actor: str,
        ns_id: str,
        funding_id: str,
        workflow: str,
        manifest: dict[str, Any],
        call_cap: int,
        key: str,
        minutes: int = 30,
    ) -> AgentRun:
        namespace_access(s, actor, ns_id, True)
        if (
            workflow not in WORKFLOWS
            or not key
            or len(key) > 150
            or type(call_cap) is not int
            or not 0 <= call_cap <= 100
            or type(minutes) is not int
            or not 1 <= minutes <= 7 * 24 * 60
        ):
            raise DomainError("RUN_CONTRACT", "任务合同无效。", 422)
        identity = {
            "ns": ns_id,
            "funding": funding_id,
            "workflow": workflow,
            "input": manifest,
            "cap": call_cap,
            "minutes": minutes,
        }
        # Serialize the idempotency key, not just one funding agreement: two
        # requests may reuse one key with different funding IDs. SQLite's
        # BEGIN IMMEDIATE already serializes writers. An advisory transaction
        # lock avoids locking unrelated principal/namespace rows on PostgreSQL.
        if s.get_bind().dialect.name == "postgresql":
            lock_id = (
                int(fingerprint({"actor": actor, "operation": "run.create", "key": key})[:16], 16)
                - 2**63
            )
            s.execute(select(func.pg_advisory_xact_lock(lock_id)))
        old = s.scalar(
            select(Idempotency).where(
                Idempotency.principal_id == actor,
                Idempotency.operation == "run.create",
                Idempotency.key == key,
            )
        )
        if old:
            if old.request_hash != fingerprint(identity):
                raise DomainError("IDEMPOTENCY_CONFLICT", "相同请求键对应不同内容。")
            existing = s.get(AgentRun, old.result["run_id"])
            if existing is None:
                raise DomainError(
                    "IDEMPOTENCY_RESULT_MISSING", "已记录的任务不存在，禁止重新计费创建。", 500
                )
            return existing
        funding = locked(s, FundingAgreement, funding_id)
        if (
            not funding
            or funding.payer_id != actor
            or funding.namespace_id != ns_id
            or funding.status != "active"
            or aware(funding.expires_at) <= clock(s)
        ):
            raise DomainError("FUNDING_UNAVAILABLE", "费用授权不适用于当前任务。", 403)
        if funding.allowed_source_ids:
            from .dependencies import assert_source_scope, check_closure

            roots = declared_manifest_dependencies(manifest)
            # The authoritative locked funding row may be narrower than an earlier
            # service preparation snapshot. No reservation precedes this recheck.
            check_closure(s, actor, roots)
            assert_source_scope(s, roots, funding.allowed_source_ids)
        if funding.calls_consumed + funding.calls_held + call_cap > funding.call_cap:
            raise DomainError("BUDGET_EXHAUSTED", "没有足够额度预留给完整任务。")
        now = clock(s)
        deadline = min(aware(funding.expires_at), now + timedelta(minutes=minutes))
        if (
            manifest.get("execution_contract_version")
            or manifest.get("build_wall_time_contract") is not None
        ):
            from .build_execution import UNLIMITED_BUILD_EXECUTION, validate_execution_manifest

            plan = validate_execution_manifest(manifest, call_cap)
            if workflow != "knowledge_build" or plan is None or plan["duration_minutes"] != minutes:
                raise DomainError("BUILD_EXECUTION_CONTRACT", "执行期限与冻结构建合同不一致。", 422)
            unlimited = plan["version"] == UNLIMITED_BUILD_EXECUTION
            if unlimited:
                # This is still an authorization deadline, never an infinite lease.
                # Do not modify/renew funding or any existing run.
                deadline = aware(funding.expires_at)
            if (deadline - now).total_seconds() < plan["estimated_seconds"]:
                raise DomainError(
                    "FUNDING_TIME_BUDGET" if unlimited else "BUILD_TIME_BUDGET",
                    "费用授权的剩余有效期无法容纳冻结的时间规划。请新建更长有效期的授权，"
                    "或调整 duration_minutes、planning_call_seconds、max_units_per_project；未预留调用。",
                    422,
                )
        learning_graph = False
        if "learning_tutor_request" in manifest:
            from .learning_requests import validate_run_admission_in

            validate_run_admission_in(s, actor, ns_id, manifest)
            if workflow != "chat":
                raise DomainError("LEARNING_TUTOR_BINDING", "导师请求只能绑定 chat 任务。", 409)
            learning_graph = True
        assessment_graph = "learning_assessment_request" in manifest
        if assessment_graph:
            from .workflow_practice import validate_run_admission_in

            validate_run_admission_in(s, actor, ns_id, manifest)
            if workflow != "learning_assessment" or call_cap != 1:
                raise DomainError("PRACTICE_BINDING", "评分必须独立运行且调用上限为一。", 409)
        evolution_graph = "evolution_graph" in manifest
        if evolution_graph:
            from .evolution_workflow import validate_run_binding_in

            request = validate_run_binding_in(
                self,
                s,
                actor,
                ns_id,
                manifest,
                admission={
                    "workflow": workflow,
                    "funding": funding_id,
                    "cap": call_cap,
                    "minutes": minutes,
                    "key": key,
                },
            )
            # Runtime owns the app-independent ledger; the validator resolves
            # the frozen request/session directly, never a caller graph choice.
            if (workflow, request.kind) not in {
                ("evolution", "candidate"),
                ("evolution_replay", "replay"),
            } or (workflow == "evolution" and call_cap != 1):
                raise DomainError("EVOLUTION_GRAPH_BINDING", "演化请求类型或调用额度不匹配。", 409)
        funding.calls_held += call_cap
        # Pin only newly created, supported root runs. Existing runs never consult the flag.
        from ..workflows.registry import DEFAULT_GRAPH_VERSIONS

        use_graph = (
            learning_graph
            or assessment_graph
            or evolution_graph
            or (
                self.db.settings.workflow_engine == "langgraph"
                and workflow in DEFAULT_GRAPH_VERSIONS
            )
        )
        row = AgentRun(
            workflow_engine="langgraph" if use_graph else "legacy",
            graph_version=(
                ("evolution-candidate-v1" if workflow == "evolution" else "evolution-replay-v1")
                if evolution_graph
                else "learning-assessment-v1"
                if assessment_graph
                else "learning-tutor-v1"
                if learning_graph
                else DEFAULT_GRAPH_VERSIONS[workflow]
            )
            if use_graph
            else None,
            state_schema_version=1 if use_graph else None,
            principal_id=actor,
            namespace_id=ns_id,
            funding_id=funding_id,
            workflow=workflow,
            input_manifest=manifest,
            input_hash=fingerprint(manifest),
            call_cap=call_cap,
            deadline_at=deadline,
        )
        s.add(row)
        s.flush()
        event(s, row, "run.created", {"workflow": workflow, "call_cap": call_cap})
        s.add(Outbox(kind="run", aggregate_id=row.id, payload={}))
        s.add(
            Idempotency(
                principal_id=actor,
                operation="run.create",
                key=key,
                request_hash=fingerprint(identity),
                result={"run_id": row.id},
            )
        )
        return row

    def claim(self, run_id: str, worker: str) -> int | None:
        with self.db.transaction() as s:
            run = lock_run(s, run_id)
            if not run or run.status != "queued":
                return None
            if pause_requested(s, run):
                park(s, run)
                return None
            if aware(run.deadline_at) <= clock(s):
                finish(s, run, "failed", "DEADLINE")
                return None
            try:
                active_funding(s, run)
            except DomainError as exc:
                finish(s, run, "failed", exc.code)
                return None
            parent = s.get(AgentRun, run.parent_id) if run.parent_id else None
            if parent and (parent.status in TERMINAL or aware(parent.deadline_at) <= clock(s)):
                finish(s, run, "cancelled", "PARENT_FINISHED")
                return None
            run.epoch += 1
            run.revision += 1
            run.status = "running"
            run.worker_id = worker
            run.lease_until = clock(s) + timedelta(seconds=60)
            event(s, run, "run.started", {"epoch": run.epoch})
            return run.epoch

    def maintain_lease(
        self, run_id: str, epoch: int, worker: str
    ) -> AbstractContextManager[LeaseHeartbeat]:
        return self._heartbeats.maintain(
            (run_id, epoch, worker),
            lambda: self.heartbeat(run_id, epoch, worker),
            self.heartbeat_interval,
        )

    def heartbeat(self, run_id: str, epoch: int, worker: str) -> None:
        check_heartbeat_stop()
        with self.db.transaction() as s:
            # An in-flight provider can settle on the same lease after a pause request.
            run = self._leased_execution_in(s, run_id, epoch, worker, allow_pause=True)
            check_heartbeat_stop()
            # leased() includes authorization/source checks that may themselves
            # block. Recheck time after that work, under the same ownership locks;
            # never turn an already expired lease/deadline into a new lease.
            parent = s.get(AgentRun, run.parent_id) if run.parent_id else None
            funding = s.get(FundingAgreement, run.funding_id)
            now = clock(s)
            check_heartbeat_stop()
            if (
                run.lease_until is None
                or aware(run.lease_until) <= now
                or aware(run.deadline_at) <= now
                or (parent is not None and aware(parent.deadline_at) <= now)
            ):
                raise LeaseLost()
            if funding is None or aware(funding.expires_at) <= now:
                raise DomainError("FUNDING_UNAVAILABLE", "费用授权已失效，停止后续调用。")
            run.lease_until = now + timedelta(seconds=60)

    def acknowledge_pause(self, run_id: str, epoch: int, worker: str) -> bool:
        with self.db.transaction() as db:
            run = lock_run(db, run_id)
            if (
                run is None
                or run.epoch != epoch
                or run.worker_id != worker
                or run.lease_until is None
                or aware(run.lease_until) <= clock(db)
            ):
                return False
            return acknowledge_in(db, run)

    def validate(self, run_id: str, epoch: int, worker: str) -> None:
        with self.db.transaction() as s:
            leased(s, run_id, epoch, worker)

    def _leased_execution_in(self, db, run_id, epoch, worker, *, allow_pause=False):
        from .session_turn import has_delivery_in, leased_finalization_in

        run = lock_run(db, run_id)
        if has_delivery_in(db, run):
            profile = getattr(self, "current_model_profile", None)
            if profile is not None and run.input_manifest.get("model_profile") != profile():
                raise DomainError(
                    "MODEL_PROFILE_CHANGED", "运行模型配置已改变，必须重新授权新任务。"
                )
            active = leased_finalization_in(db, run_id, epoch, worker)
            if profile is not None and active.input_manifest.get("model_profile") != profile():
                raise DomainError(
                    "MODEL_PROFILE_CHANGED", "运行模型配置已改变，必须重新授权新任务。"
                )
            return active
        if run is not None and run.graph_version == "learning-tutor-v1":
            turn = db.get(Turn, run.input_manifest.get("turn_id"))
            if turn is not None and turn.status == "completed":
                raise DomainError(
                    "LEARNING_TUTOR_DELIVERY", "已交付导师回答缺少原子交付凭据，禁止重建。", 409
                )
        return leased(db, run_id, epoch, worker, allow_pause)

    def validate_execution(self, run_id, epoch, worker):
        """Worker-only dispatch/heartbeat capability; never used by the gateway."""
        with self.db.transaction() as db:
            self._leased_execution_in(db, run_id, epoch, worker)

    def save_stage(
        self, run_id: str, epoch: int, worker: str, key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        with self.db.transaction() as s:
            run = leased(s, run_id, epoch, worker)
            return self.save_stage_in(s, run, key, payload)

    def save_stage_in(
        self, s: Session, run: AgentRun, key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        old = s.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id, StageArtifact.stage_key == key
            )
        )
        if old:
            if old.input_hash != run.input_hash or fingerprint(old.payload) != fingerprint(payload):
                raise DomainError("STAGE_CONFLICT", "已保存阶段与新内容不一致。")
            return old.payload
        s.add(
            StageArtifact(run_id=run.id, stage_key=key, input_hash=run.input_hash, payload=payload)
        )
        event(s, run, "stage.completed", {"key": key})
        s.flush()
        return payload

    def stage(self, run_id: str, key: str) -> dict[str, Any] | None:
        with self.db.read() as s:
            row = s.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run_id, StageArtifact.stage_key == key
                )
            )
            return row.payload if row else None

    def finish(
        self, run_id: str, epoch: int, worker: str, status: str, code: str | None = None
    ) -> bool:
        """Return whether this worker still owned and committed the transition."""
        with self.db.transaction() as s:
            run = lock_run(s, run_id)
            if (
                not run
                or run.epoch != epoch
                or run.worker_id != worker
                or run.status != "running"
                or run.lease_until is None
                or aware(run.lease_until) <= clock(s)
                or aware(run.deadline_at) <= clock(s)
            ):
                return False
            if acknowledge_in(s, run):
                return False
            # Failures may still settle a now-unavailable authorization, but an
            # expired/revoked identity must never turn a late result into success.
            if status in {"succeeded", "partially_succeeded"}:
                try:
                    if run.graph_version == "learning-tutor-v1":
                        from .session_turn import validate_finalized_in

                        validate_finalized_in(s, run)
                    # Marker reads may block too. The final ownership/deadline
                    # fence must follow them, not precede them.
                    self._leased_execution_in(s, run_id, epoch, worker)
                except LeaseLost:
                    return False
                except DomainError as exc:
                    status, code = "failed", exc.code
            finish(s, run, status, code)
            return True

    def cancel(self, actor: str, run_id: str, expected_revision: int) -> None:
        with self.db.transaction() as s:
            self.cancel_in(s, actor, run_id, expected_revision)

    @staticmethod
    def cancel_in(s, actor, run_id, expected_revision):
        """Same cancellation/settlement authority inside a caller-owned transaction."""
        run = lock_run(s, run_id)
        if not run:
            raise DomainError("NOT_FOUND", "任务不存在。", 404)
        ns = namespace_access(s, actor, run.namespace_id)
        if ns.kind == "shared_public":
            namespace_admin(s, actor, ns.id)
        elif actor != run.principal_id:
            raise DomainError("NOT_FOUND", "任务不存在。", 404)
        if run.status == "cancelled":
            return
        if run.revision != expected_revision:
            raise DomainError("REVISION_CONFLICT", "任务版本已变化。")
        if run.status in TERMINAL:
            raise DomainError("TERMINAL_RUN", "任务已经结束。")
        run.epoch += 1
        finish(s, run, "cancelled", "USER_CANCELLED")

    def create_child(
        self,
        run_id: str,
        epoch: int,
        worker: str,
        task_key: str,
        workflow: str,
        manifest: dict[str, Any],
        call_cap: int,
    ) -> str:
        from .build_execution import EVIDENCE_RETRY_CONTRACT, validate_execution_manifest

        with self.db.transaction() as s:
            parent = leased(s, run_id, epoch, worker)
            if (
                parent.parent_id
                or workflow not in {"analysis", "review", "research"}
                or not task_key
                or len(task_key) > 100
                or type(call_cap) is not int
            ):
                raise DomainError("DELEGATION_SCOPE", "只允许单层注册角色委派。")
            if parent.input_manifest.get("execution_contract_version") or manifest.get(
                "execution_contract_version"
            ):
                plan = validate_execution_manifest(parent.input_manifest, parent.call_cap)
                child_plan = validate_execution_manifest(manifest, call_cap)
                if (
                    plan is None
                    or child_plan != plan
                    or manifest.get("quote_correction_contract")
                    != parent.input_manifest.get("quote_correction_contract")
                ):
                    raise DomainError("DELEGATION_CONFLICT", "子任务必须继承同一冻结执行预算。")
                count = len(
                    manifest.get("source_ids" if workflow == "analysis" else "unit_version_ids", [])
                )
                limit = plan["source_limit"] * (
                    1 if workflow == "analysis" else plan["unit_limit_per_source"]
                )
                expected = (
                    count
                    * plan[
                        "analysis_calls_per_source"
                        if workflow == "analysis"
                        else "review_calls_per_unit"
                    ]
                )
                if (
                    workflow not in {"analysis", "review"}
                    or not 1 <= count <= limit
                    or call_cap != expected
                ):
                    raise DomainError(
                        "DELEGATION_CONFLICT", "子任务范围或调用预留不符合冻结执行计划。"
                    )
            retry_enabled = (
                parent.input_manifest.get("evidence_retry_contract") == EVIDENCE_RETRY_CONTRACT
            )
            if manifest.get("evidence_retry_contract") != parent.input_manifest.get(
                "evidence_retry_contract"
            ):
                raise DomainError("DELEGATION_CONFLICT", "子任务不能改变冻结的自动补证范围。")
            if "retry_lineage" in manifest or task_key.endswith(":evidence-retry"):
                retry_stage = s.scalar(
                    select(StageArtifact).where(
                        StageArtifact.run_id == parent.id,
                        StageArtifact.stage_key == "evidence_retry_plan",
                    )
                )
                if (
                    not retry_enabled
                    or task_key != workflow + ":evidence-retry"
                    or retry_stage is None
                    or retry_stage.input_hash != parent.input_hash
                    or manifest.get("retry_lineage") != retry_stage.payload
                    or manifest.get("mechanism_read_targets")
                    != retry_stage.payload.get("read_targets", retry_stage.payload["retry_targets"])
                    or not retry_stage.payload["retry_targets"]
                    or retry_stage.payload["source_id"] != parent.input_manifest["source_id"]
                ):
                    raise DomainError(
                        "DELEGATION_CONFLICT", "补证子任务必须使用已冻结的同源补证计划。"
                    )
                if workflow == "analysis" and manifest.get("source_ids") != [
                    retry_stage.payload["source_id"]
                ]:
                    raise DomainError("DELEGATION_SCOPE", "补证不得切换来源。")
                if workflow == "review":
                    retry_analysis = s.scalar(
                        select(StageArtifact).where(
                            StageArtifact.run_id == parent.id,
                            StageArtifact.stage_key == "analysis_retry_child",
                        )
                    )
                    analysis = (
                        s.get(AgentRun, retry_analysis.payload["run_id"])
                        if retry_analysis
                        else None
                    )
                    result = (
                        s.scalar(
                            select(StageArtifact).where(
                                StageArtifact.run_id == analysis.id,
                                StageArtifact.stage_key == "result",
                            )
                        )
                        if analysis
                        else None
                    )
                    if (
                        not analysis
                        or analysis.parent_id != parent.id
                        or analysis.status != "succeeded"
                        or analysis.input_manifest.get("retry_lineage") != retry_stage.payload
                        or result is None
                        or result.input_hash != analysis.input_hash
                        or manifest.get("unit_version_ids")
                        != result.payload.get("unit_version_ids")
                    ):
                        raise DomainError("DELEGATION_SCOPE", "补证复审必须绑定补证分析的新候选。")
            elif retry_enabled and (workflow not in {"analysis", "review"} or task_key != workflow):
                raise DomainError("DELEGATION_SCOPE", "仅允许原始与一次自动补证委派。")
            old = s.scalar(
                select(RunAllocation).where(
                    RunAllocation.parent_id == parent.id, RunAllocation.task_key == task_key
                )
            )
            if old:
                child = s.get(AgentRun, old.child_id)
                if child is None:
                    raise DomainError("DELEGATION_STATE", "已分配的子任务不存在，停止委派。", 500)
                if (
                    child.input_hash != fingerprint(manifest)
                    or old.allocated != call_cap
                    or child.workflow != workflow
                ):
                    raise DomainError("DELEGATION_CONFLICT", "子任务合同变化。")
                return child.id
            children = s.scalars(
                select(RunAllocation).where(RunAllocation.parent_id == parent.id)
            ).all()
            if (
                len(children) >= (4 if retry_enabled else 2)
                or call_cap < 1
                or parent.calls_consumed + parent.held_calls + call_cap > parent.call_cap
            ):
                raise DomainError("DELEGATION_BUDGET", "子任务数量或总额度不足。")
            child = AgentRun(
                principal_id=parent.principal_id,
                namespace_id=parent.namespace_id,
                funding_id=parent.funding_id,
                parent_id=parent.id,
                workflow=workflow,
                input_manifest=manifest,
                input_hash=fingerprint(manifest),
                call_cap=call_cap,
                deadline_at=parent.deadline_at,
            )
            s.add(child)
            s.flush()
            parent.held_calls += call_cap
            s.add(
                RunAllocation(
                    parent_id=parent.id, child_id=child.id, task_key=task_key, allocated=call_cap
                )
            )
            event(s, parent, "task.created", {"child_id": child.id, "allocated_calls": call_cap})
            s.add(Outbox(kind="run", aggregate_id=child.id, payload={}))
            return child.id

    def wait_children(self, run_id: str, epoch: int, worker: str) -> None:
        with self.db.transaction() as s:
            run = leased(s, run_id, epoch, worker)
            children = s.scalars(select(AgentRun).where(AgentRun.parent_id == run.id)).all()
            if not children:
                raise DomainError("DELEGATION_SCOPE", "没有子任务可等待。")
            # A child may finish just before the parent yields its worker.
            ready = bool(children) and all(child.status in TERMINAL for child in children)
            run.status = "queued" if ready else "waiting_children"
            if ready:
                s.add(Outbox(kind="run", aggregate_id=run.id, payload={}))
            run.worker_id = None
            run.lease_until = None
            run.revision += 1
            event(s, run, "run.waiting_children", {})

    def reap(self) -> int:
        count = 0
        with self.db.read() as s:
            identifiers = s.scalars(
                select(AgentRun.id).where(AgentRun.status.notin_(TERMINAL))
            ).all()
        # One short transaction per family avoids retaining unrelated funding
        # locks across the whole scan and makes concurrent reapers safe.
        for identifier in identifiers:
            with self.db.transaction() as s:
                run = lock_run(s, identifier)
                if run is None or run.status in TERMINAL | {"paused", "pause_pending_inflight"}:
                    continue
                if pause_requested(s, run):
                    if run.status in {"queued", "waiting_children"}:
                        park(s, run)
                        count += 1
                    elif run.lease_until and aware(run.lease_until) <= clock(s):
                        stop_expired_worker(s, run)
                        count += 1
                    continue
                authorization_error = None
                try:
                    active_funding(s, run)
                except DomainError as exc:
                    authorization_error = exc.code
                if aware(run.deadline_at) <= clock(s) or authorization_error:
                    run.epoch += 1
                    finish(s, run, "failed", authorization_error or "DEADLINE")
                    count += 1
                elif (
                    run.status == "running"
                    and run.lease_until
                    and aware(run.lease_until) <= clock(s)
                ):
                    # Conditional SQL keeps a concurrently arriving complete
                    # receipt from being downgraded by a stale ORM snapshot.
                    s.execute(
                        update(ModelReceipt)
                        .where(ModelReceipt.run_id == run.id, ModelReceipt.status == "attempted")
                        .values(status="unknown")
                    )
                    run.epoch += 1
                    run.revision += 1
                    run.status, run.worker_id, run.lease_until = "queued", None, None
                    s.add(Outbox(kind="run", aggregate_id=run.id, payload={}))
                    event(s, run, "run.recovering", {})
                    count += 1
        return count
