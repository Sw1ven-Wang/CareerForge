"""Authorized, bounded canaries for typed policies. Measurements come from server turns.

A trial does not replace the champion. In-flight turns retain frozen versions even after
rollback; every answer still passes the same evidence guard. No feedback is a truth label.
"""

from __future__ import annotations

from statistics import mean

from sqlalchemy import select

from ..db import locked
from ..errors import DomainError
from ..models import (
    Conversation,
    EvolutionCandidate,
    Outbox,
    PolicyHead,
    PolicyObservation,
    PolicyRollout,
    PolicyVersion,
    Turn,
)
from ..util import digest, fingerprint
from .policies import validate_policy
from .security import namespace_admin

HARD_GATE_FAILURES = {
    "ANSWER_FACT",
    "ANSWER_SCOPE",
    "ANSWER_CONDITIONS",
    "ANSWER_ORIGIN",
    "ANSWER_UNSUPPORTED",
    "ANSWER_NOT_EXACT",
    "ANSWER_HIGH_RISK",
    "REPLAY_CAPABILITY",
    "EGRESS_OR_ROLE_DENIED",
    "EVIDENCE_UNAVAILABLE",
    "ARTIFACT_HASH",
    "SOURCE_HASH",
    "LEASE_LOST",
}


class RolloutService:
    def __init__(self, app):
        self.app, self.db = app, app.db

    def start(
        self,
        actor,
        candidate_id,
        evaluation_id,
        expected_revision,
        percent=10,
        minimum_per_arm=10,
        maximum_turns=200,
        max_guard_regression=0.0,
        max_latency_ratio=1.5,
    ):
        if (
            not 1 <= percent <= 50
            or not 1 <= minimum_per_arm <= 1000
            or not 2 * minimum_per_arm <= maximum_turns <= 10000
        ):
            raise DomainError("ROLLOUT_LIMIT", "灰度流量与样本边界无效。", 422)
        if not 0 <= max_guard_regression <= 0.1 or not 1 <= max_latency_ratio <= 3:
            raise DomainError("ROLLOUT_LIMIT", "回滚阈值无效。", 422)
        if self.db.settings.mode != "test" and minimum_per_arm < 10:
            raise DomainError("ROLLOUT_LIMIT", "正式灰度每组至少10条；这不是统计质量证明。", 422)
        with self.db.transaction() as db:
            candidate = locked(db, EvolutionCandidate, candidate_id)
            if not candidate:
                raise DomainError("NOT_FOUND", "候选不存在。", 404)
            ns = namespace_admin(db, actor, candidate.namespace_id)
            if ns.kind != "private":
                raise DomainError("ROLLOUT_SCOPE", "当前灰度实现仅支持已授权的个人域。", 403)
            self.app.evolution._episodes(db, actor, ns.id, candidate.experience_ids)
            report, dataset, base = self.app.evolution.checked_evaluation(
                db, actor, candidate, evaluation_id
            )
            head = db.scalar(
                select(PolicyHead)
                .where(PolicyHead.namespace_id == ns.id, PolicyHead.target == candidate.target)
                .with_for_update()
            )
            if head.revision != expected_revision or head.policy_id != base.id:
                raise DomainError("POLICY_CAS", "策略基线已改变。")
            if db.scalar(
                select(PolicyRollout.id).where(
                    PolicyRollout.namespace_id == ns.id,
                    PolicyRollout.target == candidate.target,
                    PolicyRollout.status == "active",
                )
            ):
                raise DomainError("ROLLOUT_ACTIVE", "同一目标已有灰度。")
            versions = db.scalars(
                select(PolicyVersion).where(
                    PolicyVersion.namespace_id == ns.id, PolicyVersion.target == candidate.target
                )
            ).all()
            trial = PolicyVersion(
                namespace_id=ns.id,
                target=candidate.target,
                version=max(v.version for v in versions) + 1,
                content=validate_policy(candidate.target, candidate.content),
                content_hash=candidate.content_hash,
                candidate_id=candidate.id,
            )
            db.add(trial)
            db.flush()
            rollout = PolicyRollout(
                namespace_id=ns.id,
                candidate_id=candidate.id,
                evaluation_id=report.id,
                target=candidate.target,
                base_policy_id=base.id,
                trial_policy_id=trial.id,
                head_revision=head.revision,
                traffic_percent=percent,
                minimum_per_arm=minimum_per_arm,
                maximum_turns=maximum_turns,
                max_guard_regression=max_guard_regression,
                max_latency_ratio=max_latency_ratio,
                approved_by=actor,
            )
            db.add(rollout)
            db.flush()
            db.add(
                Outbox(
                    kind="policy.audit",
                    aggregate_id=rollout.id,
                    payload={"event": "policy.canary.started", "actor": actor},
                )
            )
            return {
                "rollout_id": rollout.id,
                "status": "active",
                "champion_unchanged": True,
                "trial_policy_id": trial.id,
            }

    def assign_in(self, db, actor, namespace_id, turn_id, policies):
        result = {k: dict(v) for k, v in policies.items()}
        for rollout in db.scalars(
            select(PolicyRollout).where(
                PolicyRollout.namespace_id == namespace_id, PolicyRollout.status == "active"
            )
        ):
            current = result.get(rollout.target)
            if not current or current["policy_id"] != rollout.base_policy_id:
                continue
            candidate = db.get(EvolutionCandidate, rollout.candidate_id)
            try:
                if candidate is None:
                    raise DomainError("CANDIDATE_MISSING", "灰度候选缺失。")
                self.app.evolution._episodes(db, actor, namespace_id, candidate.experience_ids)
                self.app.evolution.checked_evaluation(db, actor, candidate, rollout.evaluation_id)
            except DomainError:
                rollout.status = "reverted"
                rollout.reason = "release_evidence_invalidated"
                db.add(
                    Outbox(
                        kind="policy.audit",
                        aggregate_id=rollout.id,
                        payload={
                            "event": "policy.canary.reverted",
                            "reason": rollout.reason,
                        },
                    )
                )
                continue
            assigned = sum(
                any(p.get("rollout_id") == rollout.id for p in t.policy_versions.values())
                for t in db.scalars(
                    select(Turn)
                    .join(Conversation)
                    .where(
                        Conversation.namespace_id == namespace_id,
                    )
                )
            )
            if assigned >= rollout.maximum_turns:
                rollout.status = "stopped"
                rollout.reason = "assignment_limit_reached_no_automatic_promotion"
                continue
            bucket = int(digest(rollout.id + ":" + actor + ":" + turn_id)[:8], 16) % 100
            arm = "candidate" if bucket < rollout.traffic_percent else "control"
            selected = db.get(
                PolicyVersion,
                rollout.trial_policy_id if arm == "candidate" else rollout.base_policy_id,
            )
            if selected.content_hash != fingerprint(selected.content):
                raise DomainError("POLICY_HASH", "灰度策略损坏。")
            result[rollout.target] = {
                **current,
                "policy_id": selected.id,
                "version": selected.version,
                "hash": selected.content_hash,
                "content": selected.content,
                "rollout_id": rollout.id,
                "rollout_arm": arm,
            }
        return result

    def observe_turn(self, turn_id, blocks, latency_ms, status):
        """Internal callback only. No public endpoint accepts invented score/latency data."""
        with self.db.transaction() as db:
            self.observe_turn_in(db, turn_id, blocks, latency_ms, status)

    @staticmethod
    def observe_turn_in(db, turn_id, blocks, latency_ms, status):
        """Same idempotent monitor, usable in an existing delivery transaction."""
        turn = db.get(Turn, turn_id)
        if not turn:
            return
        for spec in turn.policy_versions.values():
            rid = spec.get("rollout_id")
            if not rid:
                continue
            rollout = locked(db, PolicyRollout, rid)
            if not rollout or rollout.status != "active":
                continue
            if db.scalar(
                select(PolicyObservation.id).where(
                    PolicyObservation.rollout_id == rid, PolicyObservation.turn_id == turn_id
                )
            ):
                continue
            db.add(
                PolicyObservation(
                    rollout_id=rid,
                    turn_id=turn_id,
                    arm=spec["rollout_arm"],
                    guard_blocks=sum(bool(b.get("blocked_reason")) for b in blocks),
                    evidence_gaps=sum(b.get("kind") == "evidence_gap" for b in blocks),
                    latency_ms=max(0, float(latency_ms)),
                    status=status,
                )
            )
            db.flush()
            metrics = RolloutService._metrics(db, rollout)
            reason = (
                "hard_gate_violation"
                if any(
                    b.get("blocked_reason") in HARD_GATE_FAILURES
                    or bool(set(b.get("details", [])) & HARD_GATE_FAILURES)
                    for b in blocks
                )
                else None
            )
            if not reason and (
                metrics["candidate_count"] >= rollout.minimum_per_arm
                and metrics["control_count"] >= rollout.minimum_per_arm
            ):
                if (
                    metrics["candidate_guard_rate"]
                    > metrics["control_guard_rate"] + rollout.max_guard_regression
                ):
                    reason = "guard_regression"
                elif metrics["candidate_error_rate"] > metrics["control_error_rate"]:
                    reason = "error_regression"
                elif metrics["latency_ratio"] > rollout.max_latency_ratio:
                    reason = "latency_regression"
            if reason:
                rollout.status = "reverted"
                rollout.reason = reason
                db.add(
                    Outbox(
                        kind="policy.audit",
                        aggregate_id=rid,
                        payload={"event": "policy.canary.reverted", "reason": reason},
                    )
                )
            elif metrics["total"] >= rollout.maximum_turns:
                rollout.status = "stopped"
                rollout.reason = "bounded_trial_complete_no_automatic_global_promotion"

    @staticmethod
    def _metrics(db, rollout):
        rows = db.scalars(
            select(PolicyObservation).where(PolicyObservation.rollout_id == rollout.id)
        ).all()
        arms = {a: [r for r in rows if r.arm == a] for a in ["candidate", "control"]}
        data = {"total": len(rows)}
        for arm, values in arms.items():
            data[arm + "_count"] = len(values)
            data[arm + "_guard_rate"] = (
                mean(bool(r.guard_blocks) for r in values) if values else 0.0
            )
            data[arm + "_error_rate"] = (
                mean(r.status != "completed" for r in values) if values else 0.0
            )
            data[arm + "_latency_ms"] = mean(r.latency_ms for r in values) if values else 0.0
        data["latency_ratio"] = data["candidate_latency_ms"] / max(1.0, data["control_latency_ms"])
        data["interpretation"] = "operational_regression_monitor_not_learning_effectiveness"
        return data

    def detail(self, actor, identifier):
        with self.db.read() as db:
            row = db.get(PolicyRollout, identifier)
            if not row:
                raise DomainError("NOT_FOUND", "灰度不存在。", 404)
            namespace_admin(db, actor, row.namespace_id)
            return {
                "rollout_id": row.id,
                "status": row.status,
                "reason": row.reason,
                "target": row.target,
                "traffic_percent": row.traffic_percent,
                "metrics": self._metrics(db, row),
            }

    def stop(self, actor, identifier):
        with self.db.transaction() as db:
            row = locked(db, PolicyRollout, identifier)
            if not row:
                raise DomainError("NOT_FOUND", "灰度不存在。", 404)
            namespace_admin(db, actor, row.namespace_id)
            if row.status == "active":
                row.status = "stopped"
                row.reason = "operator_stop"
            return {"status": row.status, "champion_unchanged": True}
