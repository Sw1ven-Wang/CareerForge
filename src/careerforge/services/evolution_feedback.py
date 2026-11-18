"""Consent-bound collection of actual trace events, never correctness labels."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts_learning_ext import TraceCaptureInput
from ..db import locked
from ..errors import DomainError
from ..models import (
    AgentRun,
    AnswerBlock,
    Conversation,
    EvolutionCandidate,
    Experience,
    Feedback,
    KnowledgeGap,
    PolicyVersion,
    Principal,
    Turn,
)
from ..util import fingerprint
from .security import namespace_admin, private_namespace


def _turn_trace(turn: Turn) -> dict[str, Any]:
    return {
        "turn_id": turn.id,
        "question_hash": fingerprint(" ".join(turn.question.split()).casefold()),
        "policy_versions_hash": fingerprint(turn.policy_versions),
        "focus_revision": turn.focus_revision,
        "status": turn.status,
    }


def _block_trace(block: AnswerBlock) -> dict[str, Any]:
    if block.content_hash != fingerprint(block.content):
        raise DomainError("TRACE_HASH", "回答轨迹完整性校验失败。")
    return {"block_id": block.id, "turn_id": block.turn_id, "content_hash": block.content_hash}


def _gap_trace(gap: KnowledgeGap) -> dict[str, Any]:
    # Scheduling a gap later does not change the original missing-evidence event.
    return {
        "gap_id": gap.id,
        "turn_id": gap.turn_id,
        "topic_keys": gap.topic_keys,
        "source_ids": gap.source_ids,
        "reason_codes": gap.reason_codes,
    }


def validate_trace_episode(db: Session, actor: str, payload: dict[str, Any]) -> None:
    frozen = payload.get("trace_manifest")
    if frozen is None:
        return
    if fingerprint(frozen) != payload.get("trace_manifest_hash"):
        raise DomainError("TRACE_HASH", "经验的冻结轨迹摘要已变化。")
    for item in frozen["turns"]:
        turn = db.get(Turn, item["turn_id"])
        conv = db.get(Conversation, turn.conversation_id) if turn else None
        if not turn or not conv or conv.principal_id != actor or _turn_trace(turn) != item:
            raise DomainError("TRACE_CHANGED", "经验对应的真实会话轨迹已变化。")
    for item in frozen["blocks"]:
        block = db.get(AnswerBlock, item["block_id"])
        if not block or _block_trace(block) != item:
            raise DomainError("TRACE_CHANGED", "经验对应的真实回答轨迹已变化。")
    for item in frozen["gaps"]:
        gap = db.get(KnowledgeGap, item["gap_id"])
        if not gap or gap.principal_id != actor or _gap_trace(gap) != item:
            raise DomainError("TRACE_CHANGED", "经验对应的真实检索缺口已变化。")


def collect_trace_feedback(service: Any, actor: str, value: TraceCaptureInput) -> dict[str, Any]:
    """A deliberate per-turn consent action, not ambient behavior surveillance.

    Caller selects signal *types*, not signal values or scores. All measurements
    below come from completed server records. No prompt/source body is copied.
    """
    with service.db.transaction() as db:
        locked(db, Principal, actor)
        ns = private_namespace(db, actor)
        turn = db.get(Turn, value.turn_id)
        conv = db.get(Conversation, turn.conversation_id) if turn else None
        if not turn or not conv or conv.principal_id != actor or conv.namespace_id != ns.id:
            raise DomainError("NOT_FOUND", "轨迹不存在。", 404)
        if turn.status not in {"completed", "failed", "interrupted"}:
            raise DomainError("TRACE_PENDING", "只采集已结束的轨迹，不能预先虚构结果。", 422)
        blocks = db.scalars(select(AnswerBlock).where(AnswerBlock.turn_id == turn.id)).all()
        block_refs = [_block_trace(b) for b in blocks]
        gaps = db.scalars(
            select(KnowledgeGap).where(
                KnowledgeGap.turn_id == turn.id,
                KnowledgeGap.principal_id == actor,
            )
        ).all()
        prior = [
            t
            for t in db.scalars(
                select(Turn).where(
                    Turn.conversation_id == conv.id,
                    Turn.focus_revision < turn.focus_revision,
                    Turn.status.in_(["completed", "failed", "interrupted"]),
                )
            ).all()
            if _turn_trace(t)["question_hash"] == _turn_trace(turn)["question_hash"]
        ]
        publications, families = [], set()
        for identifier in sorted({p for b in blocks for p in b.content.get("publication_ids", [])}):
            # Only current readable evidence contributes source lineage. A rejected
            # output itself is an event, not permission to reuse withdrawn evidence.
            try:
                pack = service.app.knowledge.publication_pack(db, actor, identifier)
            except DomainError:
                continue
            publications.append(identifier)
            families.add(pack["repository"])
        observed: dict[str, tuple[str, str, str, dict[str, Any]]] = {}
        failures = [b for b in blocks if b.content.get("blocked_reason")]
        if failures:
            observed["guard_failure"] = (
                "system_verified",
                "uncertain",
                "服务实际阻断了回答输出；不据此认定知识真伪或用户能力。",
                {
                    "guard_failure_count": len(failures),
                    "reason_codes": sorted({str(b.content["blocked_reason"]) for b in failures}),
                },
            )
        evidence_gaps = [
            block
            for block in blocks
            if block.kind == "evidence_gap"
            and block.content.get("gap_category") not in {"budget", "authorization"}
        ]
        if gaps or evidence_gaps:
            observed["retrieval_gap"] = (
                "system_verified",
                "retrieval",
                "服务实际记录了证据缺口；没有证据不等于项目不支持。",
                {
                    "gap_count": len(gaps),
                    "evidence_gap_blocks": len(evidence_gaps),
                },
            )
        if prior:
            observed["reask"] = (
                "behavior_inferred",
                "uncertain",
                "同一会话出现规范化后相同的问题；原因未知，不等于没听懂或回答错误。",
                {
                    "matching_prior_turn_ids": [t.id for t in prior],
                    "weak_signal_not_quality_label": True,
                },
            )
        collected, absent = [], []
        for signal in value.signals:
            if signal not in observed:
                absent.append(signal)
                continue
            origin, diagnosis, summary, measurements = observed[signal]
            old = db.scalar(
                select(Feedback).where(
                    Feedback.principal_id == actor,
                    Feedback.turn_id == turn.id,
                    Feedback.signal == signal,
                    Feedback.origin == origin,
                )
            )
            if old is not None:
                episode = db.scalar(select(Experience).where(Experience.feedback_id == old.id))
                if old.consent_revision < 1 or episode is None or episode.payload.get("deleted"):
                    raise DomainError(
                        "TRACE_CONSENT_WITHDRAWN",
                        "该轨迹的优化授权已撤回，不能重复采集以绕过撤回。",
                        403,
                    )
                validate_trace_episode(db, actor, episode.payload)
                collected.append(
                    {
                        "feedback_id": old.id,
                        "experience_id": episode.id,
                        "signal": signal,
                        "signal_origin": origin,
                        "replayed": True,
                    }
                )
                continue
            trace = {
                "turns": [
                    _turn_trace(turn),
                    *([_turn_trace(t) for t in prior] if signal == "reask" else []),
                ],
                "blocks": block_refs,
                "gaps": [_gap_trace(g) for g in gaps],
            }
            row = Feedback(
                principal_id=actor,
                turn_id=turn.id,
                signal=signal,
                origin=origin,
                content=summary,
                consent_revision=1,
            )
            db.add(row)
            db.flush()
            episode = Experience(
                namespace_id=ns.id,
                feedback_id=row.id,
                diagnosis=diagnosis,
                payload={
                    "turn_id": turn.id,
                    "signal": signal,
                    "signal_origin": origin,
                    "feedback": summary,
                    "measurements": measurements,
                    "observed_not_mastery": True,
                    "no_correctness_label": True,
                    "group_id": "conversation:" + conv.id,
                    "user_question_group": "conversation:" + conv.id,
                    "question_hash": fingerprint(turn.question.strip().casefold()),
                    "repository_families": sorted(families),
                    "publication_ids": publications,
                    "time_partition": turn.created_at.strftime("%Y-%m"),
                    "trace_refs": [turn.id, *[b.id for b in blocks], *[g.id for g in gaps]],
                    "trace_manifest": trace,
                    "trace_manifest_hash": fingerprint(trace),
                    "policy_versions": turn.policy_versions,
                    "private_scope_only": True,
                    "redaction_policy": "metadata_only_no_question_or_source_body",
                },
            )
            db.add(episode)
            db.flush()
            collected.append(
                {
                    "feedback_id": row.id,
                    "experience_id": episode.id,
                    "signal": signal,
                    "signal_origin": origin,
                    "replayed": False,
                }
            )
        return {
            "collected": collected,
            "not_observed": absent,
            "correctness_or_ability_inferred": False,
            "global_policy_changed": False,
            "knowledge_changed": False,
            "new_model_calls": 0,
        }


def consent_bound_episodes(
    db: Session, actor: str, namespace_id: str, identifiers: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read exact episodes under shared consent locks until this transaction ends.

    Withdrawal's first transaction touches only Feedback. It cannot deadlock with
    a run/candidate lock, and no candidate or receipt can commit behind its fence.
    Witnesses are audit metadata, not a new consent grant or model input.
    """
    namespace_admin(db, actor, namespace_id)
    if (
        not isinstance(identifiers, list)
        or not 1 <= len(identifiers) <= 50
        or any(not isinstance(item, str) or not item for item in identifiers)
        or len(set(identifiers)) != len(identifiers)
    ):
        raise DomainError("EXPERIENCES", "经验数量必须在1–50且身份不可重复。", 422)
    episodes, witnesses = [], []
    for identifier in identifiers:
        episode = db.get(Experience, identifier, populate_existing=True)
        feedback = (
            db.scalar(
                select(Feedback)
                .where(Feedback.id == episode.feedback_id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            if episode is not None
            else None
        )
        if (
            episode is None
            or episode.namespace_id != namespace_id
            or feedback is None
            or feedback.principal_id != actor
            or feedback.consent_revision < 1
            or episode.payload.get("deleted")
        ):
            raise DomainError("EXPERIENCE_CONSENT", "经验不存在、越域或已撤回授权。", 403)
        validate_trace_episode(db, actor, episode.payload)
        episodes.append(episode.payload)
        witnesses.append(
            {
                "experience_id": episode.id,
                "feedback_id": feedback.id,
                "consent_revision": feedback.consent_revision,
                "payload_hash": fingerprint(episode.payload),
            }
        )
    return episodes, witnesses


def policy_feedback_inputs(
    db: Session, actor: str, namespace_id: str, identifiers: list[str], base_policy_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Include inherited policy inputs: a candidate cannot launder revoked feedback.

    The baseline policy is also present in reflection/replay prompts. Its transitive
    training lineage is therefore part of the actual dependency/consent boundary.
    """
    episodes, witnesses = consent_bound_episodes(db, actor, namespace_id, identifiers)
    seen_experiences = set(identifiers)
    seen_policies: set[str] = set()
    policy_id = base_policy_id
    while policy_id:
        if policy_id in seen_policies or len(seen_policies) >= 128:
            raise DomainError("POLICY_CYCLE", "策略依赖循环或超出安全深度。", 409)
        seen_policies.add(policy_id)
        policy = db.get(PolicyVersion, policy_id, populate_existing=True)
        if (
            policy is None
            or policy.namespace_id != namespace_id
            or fingerprint(policy.content) != policy.content_hash
        ):
            raise DomainError("POLICY_BASE_CHANGED", "策略基线或来源完整性已经改变。", 409)
        if not policy.candidate_id:
            break
        candidate = db.get(EvolutionCandidate, policy.candidate_id, populate_existing=True)
        if (
            candidate is None
            or candidate.namespace_id != namespace_id
            or candidate.status == "consent_revoked"
        ):
            raise DomainError("EXPERIENCE_CONSENT", "继承策略的优化授权已经撤回。", 403)
        if (
            candidate.target != policy.target
            or candidate.content_hash != policy.content_hash
            or fingerprint(candidate.content) != candidate.content_hash
        ):
            raise DomainError("POLICY_BASE_CHANGED", "继承策略的候选来源已经改变。", 409)
        extra = [i for i in candidate.experience_ids if i not in seen_experiences]
        if extra:
            more_episodes, more_witnesses = consent_bound_episodes(db, actor, namespace_id, extra)
            episodes.extend(more_episodes)
            witnesses.extend(more_witnesses)
            seen_experiences.update(extra)
        if len(seen_experiences) > 1024:
            raise DomainError("EXPERIENCES", "策略谱系的经验数量超过安全上限。", 422)
        policy_id = candidate.base_policy_id
    dependencies = sorted({p for e in episodes for p in e.get("publication_ids", [])})
    return episodes, sorted(witnesses, key=lambda item: item["experience_id"]), dependencies


def model_feedback_witnesses(db: Session, run: AgentRun) -> list[dict[str, Any]]:
    """Mandatory lineage checks for reflection/replay, including historical runs.

    Keep this out of the provider request hash so already recorded legacy receipts
    retain their exact identity. Each check still uses current consent authority.
    """
    if run.workflow not in {"evolution", "evolution_replay"}:
        return []
    manifest = run.input_manifest
    if manifest.get("consent_withdrawn"):
        raise DomainError("EXPERIENCE_CONSENT", "该任务的优化授权已经撤回。", 403)
    if fingerprint(manifest) != run.input_hash:
        raise DomainError("EVOLUTION_MANIFEST_HASH", "冻结进化任务内容已改变。", 409)
    if run.workflow == "evolution":
        episodes, witnesses = consent_bound_episodes(
            db, run.principal_id, run.namespace_id, manifest.get("experience_ids", [])
        )
        if episodes != manifest.get("episodes"):
            raise DomainError("EXPERIENCE_CHANGED", "反思的冻结经验内容已经改变。", 409)
        _, witnesses, _ = policy_feedback_inputs(
            db,
            run.principal_id,
            run.namespace_id,
            manifest["experience_ids"],
            manifest["base_policy_id"],
        )
        frozen = manifest.get("reflection_input", {}).get("feedback_witnesses")
        if frozen is not None and witnesses != frozen:
            raise DomainError("EXPERIENCE_CHANGED", "反思的冻结策略经验已经改变。", 409)
        return witnesses
    candidate = db.get(EvolutionCandidate, manifest.get("candidate_id"), populate_existing=True)
    if (
        candidate is None
        or candidate.namespace_id != run.namespace_id
        or candidate.status not in {"proposed", "evaluated"}
        or candidate.content_hash != manifest.get("candidate_hash")
        or fingerprint(candidate.content) != manifest.get("candidate_hash")
    ):
        raise DomainError("REPLAY_INPUT_CHANGED", "回放候选已改变或撤回。", 409)
    _, witnesses, _ = policy_feedback_inputs(
        db, run.principal_id, run.namespace_id, candidate.experience_ids, candidate.base_policy_id
    )
    frozen = manifest.get("feedback_witnesses")
    if frozen is not None and witnesses != frozen:
        raise DomainError("EXPERIENCE_CHANGED", "回放的冻结经验内容已经改变。", 409)
    return witnesses


def feedback_source_dependencies(db: Session, witnesses: list[dict[str, Any]]) -> list[str]:
    """Server-owned references from already validated consent witnesses."""
    roots = set()
    for witness in witnesses:
        episode = db.get(Experience, witness["experience_id"], populate_existing=True)
        if episode is None or fingerprint(episode.payload) != witness["payload_hash"]:
            raise DomainError("EXPERIENCE_CHANGED", "模型输入的经验依赖已经改变。", 409)
        roots.update(episode.payload.get("publication_ids", []))
    return sorted(roots)
