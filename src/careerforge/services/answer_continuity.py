"""Server-owned conversational continuity, never factual or mastery authority."""

from __future__ import annotations

import math

from ..contracts_answer_completion_v166 import EVIDENCE_CONTINUITY_CONTRACT
from ..db import locked
from ..errors import DomainError
from ..models import Conversation
from .policies import frozen_policies
from .privacy import assert_personal_access
from .rollouts import RolloutService
from .security import namespace_access


def continuation_projection(intent, turn_id):
    """The same bounded focus fields for ordinary delivery and durable replay."""
    return {
        "topic_keys": intent.topic_keys,
        "retrieval_topic_keys": (
            intent.retrieval_topic_keys
            if intent.retrieval_topic_keys is not None
            else intent.topic_keys
        ),
        "topic_hints": intent.topic_hints,
        "retrieval_scope_version": intent.retrieval_scope_version,
        "project_source_ids": intent.project_source_ids,
        "last_turn_id": turn_id,
        "task_type": intent.interpreted_intent.value,
    }


def continued_focus(current, projection, blocks):
    focus = {**current, **projection}
    sources = projection["project_source_ids"]
    if sources and focus.get("code_focus", {}).get("source_id") not in sources:
        focus.pop("code_focus", None)
    if blocks and blocks[0].get("navigation"):
        focus["code_focus"] = blocks[0]["navigation"]
    return focus


def finalize_evidence_continuity_in(db, actor, turn, plan, assessment, blocks):
    """Called only inside the transaction committing this answer's completion."""
    observation = assessment.get("continuity_observation", {})
    latency = observation.get("latency_ms")
    if (
        plan.get("continuity_contract") != EVIDENCE_CONTINUITY_CONTRACT
        or plan.get("focus_revision") != turn.focus_revision
        or not isinstance(plan.get("continuation_focus"), dict)
        or plan["continuation_focus"].get("last_turn_id") != turn.id
        or observation.get("measurement_scope") != "processing_attempt_through_assessment"
        or type(latency) not in (int, float)
        or not math.isfinite(latency)
        or latency < 0
    ):
        raise DomainError("ANSWER_CONTINUITY_BINDING", "回答续接状态与固定任务不一致。", 409)
    current = locked(db, Conversation, turn.conversation_id)
    assert_personal_access(db, actor, expected_generation=plan["privacy_generation"])
    namespace_access(db, actor, current.namespace_id, True)
    frozen_policies(db, actor, current.namespace_id, turn.policy_versions)
    applied = current.revision == turn.focus_revision + 1
    if applied:
        current.focus = continued_focus(current.focus, plan["continuation_focus"], blocks)
    # This is the existing operational monitor, including its canary decisions.
    # It records no learning/mastery claim and is unique per rollout and turn.
    RolloutService.observe_turn_in(db, turn.id, blocks, latency, "completed")
    return {
        "contract_version": EVIDENCE_CONTINUITY_CONTRACT,
        "assessment_hash": assessment["assessment_hash"],
        "focus_status": "applied" if applied else "superseded",
        "observed_conversation_revision": current.revision,
    }
