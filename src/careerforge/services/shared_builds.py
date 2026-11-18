"""System-funded, public-input singleflight; subscriptions never transfer payment."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..contracts import KnowledgeBuildInput
from ..contracts_runtime_ext import SharedBuildInput, SubscribeInput, UnsubscribeInput
from ..db import aware, clock
from ..errors import DomainError
from ..models import (
    AgentRun,
    FundingAgreement,
    Idempotency,
    Namespace,
    ObjectRow,
    Publication,
    ReviewDecision,
    UnitVersion,
)
from ..models_runtime_ext import SharedBuild, SharedBuildSlot, TaskSubscription
from ..util import fingerprint
from .dependencies import assert_source_scope, check_closure
from .privacy import assert_personal_access
from .runtime import TERMINAL, finish, lock_run
from .security import namespace_access, namespace_admin, principal
from .sources import SourceService

if TYPE_CHECKING:
    from ..application import Application


def _key_lock(db: Session, namespace: str, key: str) -> None:
    if db.get_bind().dialect.name == "postgresql":
        value = int(fingerprint({"lock": namespace, "key": key})[:16], 16) - 2**63
        db.execute(select(func.pg_advisory_xact_lock(value)))


def validate_shared_execution(db: Session, run: AgentRun) -> None:
    """Called by the runtime before every shared root/child business commit/call."""
    root_id = run.parent_id or run.id
    shared = db.get(SharedBuild, root_id)
    if shared is None:
        return
    root = db.get(AgentRun, root_id)
    funding = db.get(FundingAgreement, shared.funding_id)
    namespace = db.get(Namespace, shared.namespace_id)
    if (
        root is None
        or funding is None
        or namespace is None
        or namespace.kind != "shared_public"
        or funding.kind != "system"
        or funding.namespace_id != namespace.id
        or root.namespace_id != namespace.id
        or funding.payer_id != shared.system_principal_id
        or root.principal_id != shared.system_principal_id
        or run.principal_id != shared.system_principal_id
        or run.namespace_id != namespace.id
        or run.funding_id != shared.funding_id
        or root.funding_id != shared.funding_id
        or set(funding.allowed_data_classes) != {"public_source"}
        or not principal(db, shared.system_principal_id).is_admin
        or root.input_hash != shared.contract_hash
        or fingerprint(root.input_manifest) != shared.contract_hash
        or root.input_manifest.get("jd_text") is not None
        or root.input_manifest.get("source_id") != shared.source_id
        or root.input_manifest.get("repository_url") is not None
    ):
        raise DomainError("SHARED_BUILD_SCOPE", "共享任务合同或系统资金范围已改变，停止执行。", 403)
    source = SourceService.get(db, shared.system_principal_id, shared.source_id)
    source_object = db.get(ObjectRow, source.id)
    source_namespace = db.get(Namespace, source_object.namespace_id) if source_object else None
    if source_namespace is None or source_namespace.kind != "shared_public":
        raise DomainError("PRIVATE_TO_PUBLIC", "共享任务不能依赖私人来源。", 403)
    assert_source_scope(db, [source.id], funding.allowed_source_ids)
    if not check_closure(db, shared.system_principal_id, [source.id]).public_only:
        raise DomainError("PRIVATE_TO_PUBLIC", "共享执行的传递来源包含私人资料。", 403)


class SharedBuildService:
    def __init__(self, app: Application):
        self.app = app
        self.db = app.db
        self.runtime = app.runtime

    @staticmethod
    def _public_mapping(db: Session, actor: str, run_id: str) -> tuple[SharedBuild, AgentRun]:
        principal(db, actor)
        shared = db.get(SharedBuild, run_id)
        run = db.get(AgentRun, run_id)
        if shared is None or run is None:
            raise DomainError("NOT_FOUND", "共享任务不存在。", 404)
        ns = namespace_access(db, actor, shared.namespace_id)
        funding = db.get(FundingAgreement, shared.funding_id)
        source = db.get(ObjectRow, shared.source_id)
        source_ns = db.get(Namespace, source.namespace_id) if source else None
        if (
            ns.kind != "shared_public"
            or run.namespace_id != ns.id
            or funding is None
            or funding.kind != "system"
            or funding.namespace_id != ns.id
            or run.funding_id != funding.id
            or run.principal_id != shared.system_principal_id
            or funding.payer_id != shared.system_principal_id
            or run.input_hash != shared.contract_hash
            or fingerprint(run.input_manifest) != shared.contract_hash
            or source_ns is None
            or source_ns.kind != "shared_public"
        ):
            raise DomainError("SHARED_BUILD_SCOPE", "共享任务已不符合公开范围，停止访问。", 403)
        return shared, run

    @staticmethod
    def _subscription(db: Session, actor: str, run_id: str) -> TaskSubscription | None:
        return db.scalar(
            select(TaskSubscription).where(
                TaskSubscription.run_id == run_id, TaskSubscription.subscriber_id == actor
            )
        )

    @staticmethod
    def _old_operation(
        db: Session, actor: str, operation: str, key: str, request_hash: str
    ) -> Idempotency | None:
        old = db.scalar(
            select(Idempotency).where(
                Idempotency.principal_id == actor,
                Idempotency.operation == operation,
                Idempotency.key == key,
            )
        )
        if old is not None and old.request_hash != request_hash:
            raise DomainError("IDEMPOTENCY_CONFLICT", "相同请求键对应不同内容。")
        return old

    def create(self, actor: str, body: SharedBuildInput) -> dict[str, Any]:
        request_hash = fingerprint(body.model_dump(mode="json"))
        with self.db.transaction() as db:
            # This is a new actor control-plane request, not continuation of an
            # existing public-system execution. Do not race erased idempotency data.
            assert_personal_access(db, actor)
            ns = namespace_admin(db, actor, body.namespace_id)
            if ns.kind != "shared_public" or not principal(db, actor).is_admin:
                raise DomainError("SYSTEM_FUNDING", "只有系统管理员可创建公共系统资助任务。", 403)
            _key_lock(db, "shared-create", actor + ":" + body.idempotency_key)
            existing_operation = db.scalar(
                select(Idempotency).where(
                    Idempotency.principal_id == actor,
                    Idempotency.operation == "shared_build.create",
                    Idempotency.key == body.idempotency_key,
                )
            )
            if existing_operation is not None:
                existing_run = db.get(AgentRun, existing_operation.result.get("run_id"))
                if existing_run is not None and not existing_run.input_manifest.get(
                    "execution_contract_version"
                ):
                    new_fields = {"duration_minutes", "planning_call_seconds"}
                    if not (new_fields & body.model_fields_set):
                        request_hash = fingerprint(body.model_dump(mode="json", exclude=new_fields))
            old = self._old_operation(
                db, actor, "shared_build.create", body.idempotency_key, request_hash
            )
            if old is not None:
                return self._detail_in(db, actor, str(old.result["run_id"]), administrator=True)
            # Validate incoming authority even when singleflight reuses another
            # system task. Do not take funding ahead of run.create's key lock.
            funding = db.get(FundingAgreement, body.funding_id)
            if (
                funding is None
                or funding.kind != "system"
                or funding.payer_id != actor
                or funding.namespace_id != ns.id
                or funding.status != "active"
                or aware(funding.expires_at) <= clock(db)
                or set(funding.allowed_data_classes) != {"public_source"}
            ):
                raise DomainError(
                    "SYSTEM_FUNDING", "缺少匹配的纯公共系统出资授权；不会使用订阅者余额。", 403
                )
            source = SourceService.get(db, actor, body.source_id)
            source_object = db.get(ObjectRow, source.id)
            source_ns = db.get(Namespace, source_object.namespace_id) if source_object else None
            if source_ns is None or source_ns.kind != "shared_public":
                raise DomainError(
                    "PRIVATE_TO_PUBLIC", "共享构建仅接受已固定的公共来源，不接受私人任务或JD。", 403
                )
            check_closure(db, actor, [source.id])
            assert_source_scope(db, [source.id], funding.allowed_source_ids)
            # No subscriber question/history/goal enters this neutral system contract.
            question = "分析已登记公共源码中的技术实现。目标主题：" + (
                ", ".join(body.topic_keys) or "项目整体结构"
            )
            build = KnowledgeBuildInput(
                namespace_id=ns.id,
                funding_id=body.funding_id,
                source_id=source.id,
                question=question,
                topic_keys=body.topic_keys,
                call_cap=body.call_cap,
                max_projects=1,
                max_units_per_project=body.max_units_per_project,
                duration_minutes=body.duration_minutes,
                planning_call_seconds=body.planning_call_seconds,
                idempotency_key="shared:" + body.idempotency_key,
            )
            manifest = self.app.build.prepare_in(db, actor, build)
            semantic = {
                key: value
                for key, value in manifest.items()
                if key not in {"funding_id", "idempotency_key"}
            }
            semantic["no_subscribers_policy"] = body.no_subscribers_policy
            scope_hash = fingerprint(semantic)
            _key_lock(db, "shared-scope", ns.id + ":" + scope_hash)
            slot = db.scalar(
                select(SharedBuildSlot)
                .where(
                    SharedBuildSlot.namespace_id == ns.id, SharedBuildSlot.scope_hash == scope_hash
                )
                .with_for_update()
            )
            current = (
                db.get(AgentRun, slot.current_run_id) if slot and slot.current_run_id else None
            )
            if current is not None and (
                current.status not in TERMINAL
                or (
                    current.status in {"succeeded", "partially_succeeded"}
                    and not body.force_new_generation
                )
            ):
                if body.force_new_generation and current.status not in TERMINAL:
                    raise DomainError("SHARED_BUILD_ACTIVE", "相同公共合同仍在运行，不能重复创建。")
                run = current
            else:
                run = self.runtime.create_in(
                    db,
                    actor,
                    ns.id,
                    funding.id,
                    "knowledge_build",
                    manifest,
                    body.call_cap,
                    "shared:" + body.idempotency_key,
                    body.duration_minutes,
                )
                db.add(
                    SharedBuild(
                        run_id=run.id,
                        namespace_id=ns.id,
                        system_principal_id=actor,
                        funding_id=funding.id,
                        source_id=source.id,
                        scope_hash=scope_hash,
                        contract_hash=run.input_hash,
                        no_subscribers_policy=body.no_subscribers_policy,
                    )
                )
                if slot is None:
                    slot = SharedBuildSlot(namespace_id=ns.id, scope_hash=scope_hash)
                    db.add(slot)
                slot.current_run_id = run.id
            db.add(
                Idempotency(
                    principal_id=actor,
                    operation="shared_build.create",
                    key=body.idempotency_key,
                    request_hash=request_hash,
                    result={"run_id": run.id},
                )
            )
            db.flush()
            validate_shared_execution(db, run)
            return self._detail_in(db, actor, run.id, administrator=True)

    def subscribe(self, actor: str, run_id: str, body: SubscribeInput) -> dict[str, Any]:
        with self.db.transaction() as db:
            assert_personal_access(db, actor)
            _key_lock(db, "shared-subscription-operation", actor + ":" + body.idempotency_key)
            shared, _ = self._public_mapping(db, actor, run_id)
            run = lock_run(db, run_id)
            if run is None:
                raise DomainError("NOT_FOUND", "共享任务不存在。", 404)
            db.refresh(shared)  # ever_subscribed may change while waiting on the root lock.
            identity = fingerprint({"run_id": run_id, "rights": body.rights})
            old = self._old_operation(db, actor, "shared.subscribe", body.idempotency_key, identity)
            row = self._subscription(db, actor, run_id)
            if old is None:
                if row is None:
                    row = TaskSubscription(
                        run_id=run_id, subscriber_id=actor, rights=list(body.rights)
                    )
                    db.add(row)
                elif not row.active or row.rights != body.rights:
                    row.active, row.rights, row.detached_at = True, list(body.rights), None
                    row.revision += 1
                shared.ever_subscribed = True
                db.flush()
                db.add(
                    Idempotency(
                        principal_id=actor,
                        operation="shared.subscribe",
                        key=body.idempotency_key,
                        request_hash=identity,
                        result={"run_id": run_id, "subscription_id": row.id},
                    )
                )
            return self._detail_in(db, actor, run_id, allow_detached=True)

    def unsubscribe(self, actor: str, run_id: str, body: UnsubscribeInput) -> dict[str, Any]:
        with self.db.transaction() as db:
            assert_personal_access(db, actor)
            _key_lock(db, "shared-subscription-operation", actor + ":" + body.idempotency_key)
            shared, _ = self._public_mapping(db, actor, run_id)
            run = lock_run(db, run_id)
            if run is None:
                raise DomainError("NOT_FOUND", "共享任务不存在。", 404)
            db.refresh(shared)  # ever_subscribed may change while waiting on the root lock.
            identity = fingerprint({"run_id": run_id})
            old = self._old_operation(
                db, actor, "shared.unsubscribe", body.idempotency_key, identity
            )
            row = self._subscription(db, actor, run_id)
            if old is None:
                if row is not None and row.active:
                    row.active, row.detached_at = False, clock(db)
                    row.revision += 1
                    db.flush()
                    remains = db.scalar(
                        select(TaskSubscription.id)
                        .where(TaskSubscription.run_id == run_id, TaskSubscription.active.is_(True))
                        .limit(1)
                    )
                    if (
                        remains is None
                        and shared.ever_subscribed
                        and shared.no_subscribers_policy == "cancel_after_last_detach"
                        and run.status not in TERMINAL
                    ):
                        run.epoch += 1
                        finish(db, run, "cancelled", "NO_SUBSCRIBERS_POLICY")
                db.add(
                    Idempotency(
                        principal_id=actor,
                        operation="shared.unsubscribe",
                        key=body.idempotency_key,
                        request_hash=identity,
                        result={"run_id": run_id},
                    )
                )
            return self._detail_in(db, actor, run_id, allow_detached=True)

    def detach_owned_in(self, db: Session, actor: str) -> int:
        """Trusted personal-erasure hook; keep the frozen zero-subscriber policy.

        Called only after the actor's personal fence has committed. The same root
        lock as normal subscribe/detach serializes against other subscribers; no
        personal authorization check may prevent this cleanup operation.
        """
        roots = db.execute(
            select(AgentRun.id, AgentRun.funding_id)
            .join(TaskSubscription, TaskSubscription.run_id == AgentRun.id)
            .where(TaskSubscription.subscriber_id == actor, TaskSubscription.active.is_(True))
            .order_by(AgentRun.funding_id, AgentRun.id)
        ).all()
        detached = 0
        for root in roots:
            run = lock_run(db, root.id)
            shared = db.get(SharedBuild, root.id, populate_existing=True)
            row = self._subscription(db, actor, root.id)
            if run is None or shared is None or row is None or not row.active:
                continue
            row.active, row.detached_at = False, clock(db)
            row.revision += 1
            detached += 1
            db.flush()
            remains = db.scalar(
                select(TaskSubscription.id)
                .where(TaskSubscription.run_id == run.id, TaskSubscription.active.is_(True))
                .limit(1)
            )
            if (
                remains is None
                and shared.ever_subscribed
                and shared.no_subscribers_policy == "cancel_after_last_detach"
                and run.status not in TERMINAL
            ):
                run.epoch += 1
                finish(db, run, "cancelled", "NO_SUBSCRIBERS_POLICY")
        return detached

    def detail(self, actor: str, run_id: str) -> dict[str, Any]:
        with self.db.read() as db:
            assert_personal_access(db, actor)
            return self._detail_in(db, actor, run_id)

    def _detail_in(
        self,
        db: Session,
        actor: str,
        run_id: str,
        administrator: bool = False,
        allow_detached: bool = False,
    ) -> dict[str, Any]:
        shared, run = self._public_mapping(db, actor, run_id)
        subscription = self._subscription(db, actor, run_id)
        admin = administrator or (
            actor == shared.system_principal_id and principal(db, actor).is_admin
        )
        if not admin and (subscription is None or (not subscription.active and not allow_detached)):
            raise DomainError("NOT_FOUND", "没有此共享任务的有效订阅。", 404)
        rights = (
            {"progress", "result"}
            if admin
            else set(subscription.rights if subscription and subscription.active else [])
        )
        result: dict[str, Any] = {
            "run_id": run.id,
            "payer_kind": "system",
            "subscription_is_payment_consent": False,
            "subscribed": bool(subscription and subscription.active),
            "subscription_revision": subscription.revision if subscription else None,
            "rights": sorted(rights),
            "no_subscribers_policy": shared.no_subscribers_policy,
        }
        if "progress" in rights:
            result.update(
                status=run.status,
                revision=run.revision,
                calls_consumed=run.calls_consumed,
                call_cap=run.call_cap,
                source_id=shared.source_id,
                deadline_at=run.deadline_at,
                duration_minutes=run.input_manifest.get("duration_minutes"),
                execution_plan=run.input_manifest.get("execution_plan"),
                checkpoint=self.app.build.checkpoint_in(db, actor, run),
            )
        if "result" in rights:
            from ..models import StageArtifact

            stage = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run.id, StageArtifact.stage_key == "result"
                )
            )
            if stage and stage.input_hash != shared.contract_hash:
                raise DomainError("SHARED_BUILD_SCOPE", "共享结果阶段不匹配冻结合同。", 403)
            identifiers = stage.payload.get("publication_ids", []) if stage else []
            if not isinstance(identifiers, list) or any(
                not isinstance(identifier, str) for identifier in identifiers
            ):
                raise DomainError("SHARED_BUILD_SCOPE", "共享结果投影格式无效。", 403)
            publications = []
            for identifier in identifiers:
                publication = db.get(Publication, identifier)
                version = db.get(UnitVersion, publication.unit_version_id) if publication else None
                review = db.get(ReviewDecision, publication.review_id) if publication else None
                analysis_run = db.get(AgentRun, version.analysis_run_id) if version else None
                review_run = db.get(AgentRun, review.reviewer_run_id) if review else None
                if (
                    publication is None
                    or publication.namespace_id != shared.namespace_id
                    or version is None
                    or version.source_id != shared.source_id
                    or analysis_run is None
                    or analysis_run.parent_id != run.id
                    or analysis_run.funding_id != run.funding_id
                    or review_run is None
                    or review_run.parent_id != run.id
                    or review_run.funding_id != run.funding_id
                ):
                    raise DomainError(
                        "SHARED_BUILD_SCOPE", "共享结果不属于冻结来源和本次系统任务。", 403
                    )
                pack = self.app.knowledge.publication_pack(db, actor, publication.id)
                if pack["source_id"] != shared.source_id:
                    raise DomainError("SHARED_BUILD_SCOPE", "共享结果来源与公开合同不一致。", 403)
                publications.append(publication.id)
            result["publication_ids"] = publications
        return result
