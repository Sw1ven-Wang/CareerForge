"""Current consent and source lineage for server-bound policy inputs.

These requirements are derived from authoritative run/turn/version records, never
from model-provided identifiers or strings found in a prompt. They are disclosure
requirements, not grants. ModelGateway separately enforces the funding contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..errors import DomainError
from ..models import AgentRun, Conversation, EvolutionCandidate, PolicyVersion, Turn
from ..util import fingerprint
from .dependencies import check_closure
from .evolution_feedback import policy_feedback_inputs

POLICY_INPUT_CONTRACT = "policy-lineage-1.6.1"
ROLE_POLICY_TARGETS = {
    "coordinator": {"routing_policy"},
    "tutor": {"teaching_skill"},
    "answer_checker": {"teaching_skill"},
    "planner": {"research_heuristic"},
    "reflector": {"research_heuristic"},
    "embedding": {"retrieval_recipe"},
    "reranker": {"retrieval_recipe"},
    "query_rewriter": {"retrieval_recipe"},
}


@dataclass(frozen=True)
class PolicyInputs:
    witnesses: list[dict[str, Any]]
    access_dependencies: list[str]
    data_classes: list[str]
    preparation_authorized: bool = False


def policy_version_inputs(db: Session, actor: str, policy: PolicyVersion) -> PolicyInputs:
    """Validate an evolved version, including every inherited candidate's consent."""
    if fingerprint(policy.content) != policy.content_hash:
        raise DomainError("POLICY_HASH", "策略版本完整性校验失败。", 409)
    if not policy.candidate_id:
        return PolicyInputs([], [], [])
    candidate = db.get(EvolutionCandidate, policy.candidate_id, populate_existing=True)
    if (
        candidate is None
        or candidate.namespace_id != policy.namespace_id
        or candidate.status == "consent_revoked"
    ):
        raise DomainError("EXPERIENCE_CONSENT", "该策略的优化授权已经撤回。", 403)
    if (
        candidate.target != policy.target
        or candidate.content_hash != policy.content_hash
        or fingerprint(candidate.content) != candidate.content_hash
    ):
        raise DomainError("POLICY_HASH", "策略候选的来源或完整性已经改变。", 409)
    _, witnesses, dependencies = policy_feedback_inputs(
        db, actor, policy.namespace_id, candidate.experience_ids, candidate.base_policy_id
    )
    closure = check_closure(db, actor, dependencies)
    return PolicyInputs(
        witnesses, dependencies, sorted({"learning_state", *closure.source_data_classes})
    )


def run_policy_inputs(db: Session, run: AgentRun, role: str) -> PolicyInputs:
    """Resolve the exact role's frozen policy inputs from trusted run provenance."""
    targets = ROLE_POLICY_TARGETS.get(role, set())
    if not targets or run.workflow in {"evolution", "evolution_replay"}:
        return PolicyInputs([], [], [])
    # Knowledge-build embeddings encode approved publication text. This workflow
    # does not run retrieval queries or consume the retrieval recipe, so unrelated
    # learned ranking preferences must not expand its disclosure requirements.
    if role == "embedding" and run.workflow in {"knowledge_build", "retrieval_index"}:
        return PolicyInputs([], [], [])
    owner = run
    manifest = run.input_manifest
    refs: dict[str, Any] = {}
    if run.workflow == "chat" and manifest.get("turn_id"):
        turn = db.get(Turn, manifest["turn_id"], populate_existing=True)
        conversation = db.get(Conversation, turn.conversation_id) if turn else None
        if (
            turn is None
            or conversation is None
            or turn.run_id != run.id
            or conversation.principal_id != run.principal_id
            or conversation.namespace_id != run.namespace_id
        ):
            raise DomainError("POLICY_SCOPE", "会话策略不属于本次执行任务。", 403)
        refs = turn.policy_versions
    elif "policies" in manifest:
        refs = manifest["policies"]
    elif run.parent_id:
        parent = db.get(AgentRun, run.parent_id, populate_existing=True)
        if (
            parent is None
            or parent.principal_id != run.principal_id
            or parent.namespace_id != run.namespace_id
        ):
            raise DomainError("POLICY_SCOPE", "子任务策略来源不属于本次执行范围。", 403)
        owner, manifest = parent, parent.input_manifest
        refs = manifest.get("policies", {})
    if not isinstance(refs, dict):
        raise DomainError("POLICY_SCOPE", "冻结策略引用合同无效。", 409)
    if refs and fingerprint(manifest) != owner.input_hash:
        raise DomainError("POLICY_MANIFEST_HASH", "冻结策略任务内容已经改变。", 409)
    witnesses: dict[str, dict[str, Any]] = {}
    dependencies: set[str] = set()
    classes: set[str] = set()
    for target in sorted(targets & refs.keys()):
        ref = refs[target]
        if not isinstance(ref, dict) or not isinstance(ref.get("policy_id"), str):
            raise DomainError("POLICY_SCOPE", "冻结策略身份无效。", 409)
        policy = db.get(PolicyVersion, ref["policy_id"], populate_existing=True)
        if (
            policy is None
            or policy.namespace_id != run.namespace_id
            or policy.target != target
            or policy.content_hash != ref.get("hash")
            or ("content" in ref and fingerprint(ref["content"]) != policy.content_hash)
        ):
            raise DomainError("POLICY_FROZEN_CHANGED", "冻结策略版本或内容已经改变。", 409)
        inputs = policy_version_inputs(db, run.principal_id, policy)
        witnesses.update({item["experience_id"]: item for item in inputs.witnesses})
        dependencies.update(inputs.access_dependencies)
        classes.update(inputs.data_classes)
    return PolicyInputs(
        [witnesses[key] for key in sorted(witnesses)],
        sorted(dependencies),
        sorted(classes),
        manifest.get("policy_input_contract") == POLICY_INPUT_CONTRACT,
    )
