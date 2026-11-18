"""Versioned immutable pointer-effect receipts; historical success is not live authority."""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy import func, select

from ..db import locked
from ..errors import DomainError
from ..models import (
    EvolutionCandidate,
    EvolutionEvaluation,
    Idempotency,
    Outbox,
    PolicyHead,
    PolicyVersion,
    Principal,
)
from ..util import fingerprint
from .policy_inputs import policy_version_inputs
from .privacy import assert_personal_access
from .security import namespace_admin

TARGETS = {"teaching_skill", "routing_policy", "retrieval_recipe", "research_heuristic"}
CONTRACT = "evolution-command-effect-v1"


def _fail(message="演化命令的原始效果回执缺失或不一致。"):
    raise DomainError("EVOLUTION_EFFECT_RECEIPT", message, 409)


class EvolutionCommands:
    def __init__(self, app):
        self.app, self.db = app, app.db

    def _request(self, actor, operation, key, payload, scope):
        if not isinstance(key, str) or not key or len(key) > 150:
            raise DomainError("EVOLUTION_COMMAND", "命令键必须为1至150个字符。", 422)
        if scope is not None and (
            not isinstance(scope, dict)
            or len(scope) > 20
            or any(
                not isinstance(k, str)
                or len(k) > 100
                or (v is not None and type(v) not in {str, int})
                or (isinstance(v, str) and len(v) > 200)
                for k, v in scope.items()
            )
        ):
            raise DomainError("EVOLUTION_COMMAND", "命令范围只能包含有界身份引用。", 422)
        if type(payload.get("expected_revision")) is not int or payload["expected_revision"] < 1:
            raise DomainError("EVOLUTION_COMMAND", "必须提供原始策略版本。", 422)
        return {
            "contract": CONTRACT,
            "actor": actor,
            "operation": operation,
            **payload,
            "scope": deepcopy(scope),
        }

    def _prior(self, db, actor, operation, key, request):
        # Principal is the inherited account mutex. Never upgrade it to FOR UPDATE.
        user = locked(db, Principal, actor)
        if user is None or not user.active:
            raise DomainError("UNAUTHENTICATED", "身份不可用。", 401)
        assert_personal_access(db, actor)
        if db.get_bind().dialect.name == "postgresql":
            lock_id = (
                int(fingerprint({"actor": actor, "operation": operation, "key": key})[:16], 16)
                - 2**63
            )
            db.execute(select(func.pg_advisory_xact_lock(lock_id)))
        old = db.scalar(
            select(Idempotency).where(
                Idempotency.principal_id == actor,
                Idempotency.operation == operation,
                Idempotency.key == key,
            )
        )
        if old and old.request_hash != fingerprint(request):
            raise DomainError("IDEMPOTENCY_CONFLICT", "相同演化命令键对应不同请求。", 409)
        return old

    @staticmethod
    def _head(db, namespace_id, target):
        return db.scalar(
            select(PolicyHead).where(
                PolicyHead.namespace_id == namespace_id, PolicyHead.target == target
            )
        )

    def _authorize(self, db, actor, namespace_id):
        namespace = namespace_admin(db, actor, namespace_id)
        if namespace.kind == "shared_public" and not db.get(Principal, actor).is_admin:
            raise DomainError("PUBLIC_PROMOTION", "公共策略变更需要系统治理批准。", 403)

    def _availability(self, db, actor, effect):
        head = self._head(db, effect["namespace_id"], effect["target"])
        policy = db.get(PolicyVersion, effect["to_policy_id"])
        if not policy or not head or head.policy_id != policy.id:
            return False
        if policy.content_hash != fingerprint(policy.content):
            return False
        if policy.candidate_id:
            candidate = db.get(EvolutionCandidate, policy.candidate_id)
            if not candidate or candidate.status != "promoted":
                return False
        try:
            policy_version_inputs(db, actor, policy)
        except DomainError:
            return False
        return True

    def _read(self, db, actor, receipt, request):
        value = receipt.result
        if not isinstance(value, dict) or set(value) != {"effect", "effect_hash"}:
            _fail()
        effect = value["effect"]
        if not isinstance(effect, dict) or value["effect_hash"] != fingerprint(effect):
            _fail()
        required = {
            "contract",
            "actor",
            "operation",
            "request_hash",
            "namespace_id",
            "target",
            "candidate_id",
            "from_policy_id",
            "to_policy_id",
            "before_revision",
            "after_revision",
            "before_hash",
            "after_hash",
            "audit_id",
            "audit_aggregate_id",
            "audit_payload",
            "result",
        }
        if request["operation"] == "evolution.promote.v1":
            required |= {"evaluation_id", "candidate_hash", "base_hash", "dataset_hash"}
        if (
            set(effect) != required
            or not isinstance(effect["result"], dict)
            or not isinstance(effect["audit_payload"], dict)
            or type(effect["before_revision"]) is not int
            or type(effect["after_revision"]) is not int
        ):
            _fail()
        if (
            effect.get("contract") != CONTRACT
            or effect.get("request_hash") != fingerprint(request)
            or effect.get("actor") != actor
            or effect.get("operation") != request["operation"]
        ):
            _fail()
        self._authorize(db, actor, effect["namespace_id"])
        before, after = (
            db.get(PolicyVersion, effect[name]) for name in ("from_policy_id", "to_policy_id")
        )
        if (
            not before
            or not after
            or any(
                (p.namespace_id, p.target) != (effect["namespace_id"], effect["target"])
                for p in (before, after)
            )
            or before.content_hash != effect["before_hash"]
            or after.content_hash != effect["after_hash"]
            or effect["before_revision"] != request["expected_revision"]
            or effect["after_revision"] != effect["before_revision"] + 1
            or effect["result"].get("policy_id") != after.id
            or effect["result"].get("head_revision") != effect["after_revision"]
        ):
            _fail()
        audit = db.get(Outbox, effect["audit_id"])
        if (
            not audit
            or audit.kind != "policy.audit"
            or audit.aggregate_id != effect["audit_aggregate_id"]
            or audit.payload != effect["audit_payload"]
        ):
            _fail()
        candidate = db.get(EvolutionCandidate, effect["candidate_id"])
        if (
            not candidate
            or candidate.namespace_id != effect["namespace_id"]
            or candidate.target != effect["target"]
        ):
            _fail()
        if request["operation"] == "evolution.promote.v1":
            evaluation = db.get(EvolutionEvaluation, request["evaluation_id"])
            if (
                request["candidate_id"] != candidate.id
                or after.candidate_id != candidate.id
                or candidate.base_policy_id != before.id
                or not evaluation
                or evaluation.candidate_id != candidate.id
                or evaluation.id != effect["evaluation_id"]
                or audit.aggregate_id != candidate.id
                or candidate.content_hash != effect["candidate_hash"]
                or after.content_hash != effect["candidate_hash"]
                or before.content_hash != effect["base_hash"]
                or effect["result"]
                != {
                    "policy_id": after.id,
                    "head_revision": effect["after_revision"],
                    "active_from": "next_task_only",
                }
                or evaluation.candidate_hash != effect["candidate_hash"]
                or evaluation.base_hash != effect["base_hash"]
                or evaluation.report.get("dataset_hash") != effect["dataset_hash"]
                or audit.payload
                != {
                    "event": "policy.promoted",
                    "policy_id": after.id,
                    "evaluation_id": evaluation.id,
                    "actor": actor,
                }
            ):
                _fail()
        elif (
            request["namespace_id"] != effect["namespace_id"]
            or request["target"] != effect["target"]
            or request["expected_from_policy_id"] != before.id
            or request["expected_to_policy_id"] != after.id
            or audit.aggregate_id != after.id
            or effect["result"]
            != {
                "policy_id": after.id,
                "head_revision": effect["after_revision"],
                "inflight_tasks_changed": False,
            }
            or before.candidate_id != candidate.id
            or candidate.base_policy_id != after.id
            or audit.payload
            != {
                "event": "policy.rolled_back",
                "from_policy_id": before.id,
                "policy_id": after.id,
                "candidate_id": candidate.id,
                "head_revision": effect["after_revision"],
                "actor": actor,
            }
        ):
            _fail()
        return {
            **deepcopy(effect["result"]),
            "command_receipt_id": receipt.id,
            "effect_hash": value["effect_hash"],
            "audit_id": audit.id,
            "current_available": self._availability(db, actor, effect),
        }

    def _save(self, db, actor, operation, key, request, effect):
        row = Idempotency(
            principal_id=actor,
            operation=operation,
            key=key,
            request_hash=fingerprint(request),
            result={"effect": effect, "effect_hash": fingerprint(effect)},
        )
        db.add(row)
        db.flush()
        return self._read(db, actor, row, request)

    def _audit(self, db, prior_ids, expected):
        db.flush()
        rows = [
            row
            for row in db.scalars(select(Outbox).where(Outbox.kind == "policy.audit"))
            if row.id not in prior_ids and row.payload == expected
        ]
        if len(rows) != 1:
            _fail("策略事务必须包含唯一原始审计记录。")
        return rows[0]

    def promote(self, actor, candidate_id, evaluation_id, expected_revision, key, *, scope=None):
        with self.db.transaction() as db:
            return self.promote_in(
                db, actor, candidate_id, evaluation_id, expected_revision, key, scope=scope
            )

    def promote_in(
        self, db, actor, candidate_id, evaluation_id, expected_revision, key, *, scope=None
    ):
        operation = "evolution.promote.v1"
        request = self._request(
            actor,
            operation,
            key,
            dict(
                candidate_id=candidate_id,
                evaluation_id=evaluation_id,
                expected_revision=expected_revision,
            ),
            scope,
        )
        prior = self._prior(db, actor, operation, key, request)
        if prior:
            return self._read(db, actor, prior, request)
        candidate = db.get(EvolutionCandidate, candidate_id)
        if not candidate:
            raise DomainError("NOT_FOUND", "候选不存在。", 404)
        self._authorize(db, actor, candidate.namespace_id)
        if candidate.target not in TARGETS:
            raise DomainError("EVOLUTION_TARGET", "策略目标不适用。", 422)
        prior_audits = set(db.scalars(select(Outbox.id).where(Outbox.kind == "policy.audit")))
        result = self.app.evolution.promote_in(
            db, actor, candidate_id, evaluation_id, expected_revision
        )
        audit = self._audit(
            db,
            prior_audits,
            {
                "event": "policy.promoted",
                "policy_id": result["policy_id"],
                "evaluation_id": evaluation_id,
                "actor": actor,
            },
        )
        evaluation = db.get(EvolutionEvaluation, evaluation_id)
        effect = dict(
            contract=CONTRACT,
            actor=actor,
            operation=operation,
            request_hash=fingerprint(request),
            namespace_id=candidate.namespace_id,
            target=candidate.target,
            candidate_id=candidate.id,
            from_policy_id=candidate.base_policy_id,
            to_policy_id=result["policy_id"],
            before_hash=db.get(PolicyVersion, candidate.base_policy_id).content_hash,
            after_hash=db.get(PolicyVersion, result["policy_id"]).content_hash,
            before_revision=expected_revision,
            after_revision=result["head_revision"],
            evaluation_id=evaluation_id,
            candidate_hash=evaluation.candidate_hash,
            base_hash=evaluation.base_hash,
            dataset_hash=evaluation.report.get("dataset_hash"),
            audit_id=audit.id,
            audit_aggregate_id=audit.aggregate_id,
            audit_payload=deepcopy(audit.payload),
            result=deepcopy(result),
        )
        return self._save(db, actor, operation, key, request, effect)

    def rollback(
        self,
        actor,
        namespace_id,
        target,
        expected_revision,
        key,
        expected_from_policy_id,
        expected_to_policy_id,
        *,
        scope=None,
    ):
        with self.db.transaction() as db:
            return self.rollback_in(
                db,
                actor,
                namespace_id,
                target,
                expected_revision,
                key,
                expected_from_policy_id,
                expected_to_policy_id,
                scope=scope,
            )

    def rollback_in(
        self,
        db,
        actor,
        namespace_id,
        target,
        expected_revision,
        key,
        expected_from_policy_id,
        expected_to_policy_id,
        *,
        scope=None,
    ):
        operation = "evolution.rollback.v1"
        request = self._request(
            actor,
            operation,
            key,
            dict(
                namespace_id=namespace_id,
                target=target,
                expected_revision=expected_revision,
                expected_from_policy_id=expected_from_policy_id,
                expected_to_policy_id=expected_to_policy_id,
            ),
            scope,
        )
        prior = self._prior(db, actor, operation, key, request)
        if prior:
            return self._read(db, actor, prior, request)
        self._authorize(db, actor, namespace_id)
        head = self._head(db, namespace_id, target)
        if (
            target not in TARGETS
            or not head
            or head.revision != expected_revision
            or head.policy_id != expected_from_policy_id
            or head.previous_policy_id != expected_to_policy_id
        ):
            raise DomainError("ROLLBACK_CONFLICT", "回滚必须绑定原始起点、目标及版本。", 409)
        current = db.get(PolicyVersion, expected_from_policy_id)
        if current is None or not current.candidate_id:
            raise DomainError("ROLLBACK_CONFLICT", "回滚起点缺少已批准候选。", 409)
        prior_audits = set(db.scalars(select(Outbox.id).where(Outbox.kind == "policy.audit")))
        result = self.app.evolution.rollback_in(db, actor, namespace_id, target, expected_revision)
        if result["policy_id"] != expected_to_policy_id:
            _fail()
        audit = self._audit(
            db,
            prior_audits,
            {
                "event": "policy.rolled_back",
                "from_policy_id": expected_from_policy_id,
                "policy_id": expected_to_policy_id,
                "candidate_id": current.candidate_id,
                "head_revision": result["head_revision"],
                "actor": actor,
            },
        )
        effect = dict(
            contract=CONTRACT,
            actor=actor,
            operation=operation,
            request_hash=fingerprint(request),
            namespace_id=namespace_id,
            target=target,
            candidate_id=current.candidate_id,
            from_policy_id=expected_from_policy_id,
            to_policy_id=expected_to_policy_id,
            before_hash=current.content_hash,
            after_hash=db.get(PolicyVersion, expected_to_policy_id).content_hash,
            before_revision=expected_revision,
            after_revision=result["head_revision"],
            audit_id=audit.id,
            audit_aggregate_id=audit.aggregate_id,
            audit_payload=deepcopy(audit.payload),
            result=deepcopy(result),
        )
        return self._save(db, actor, operation, key, request, effect)
