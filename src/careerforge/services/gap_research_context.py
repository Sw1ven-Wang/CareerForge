"""Frozen answer omissions guide investigation, never approve source facts."""

from sqlalchemy import select

from ..contracts_answer_completion_v166 import ANSWER_COMPLETION_STAGE
from ..errors import DomainError
from ..models import AgentRun, StageArtifact, Turn
from ..util import fingerprint
from .dependencies import check_closure


def capture(db, actor, turn_id):
    turn = db.get(Turn, turn_id)
    run = db.get(AgentRun, turn.run_id) if turn and turn.run_id else None
    if not run:
        return None
    if run.principal_id != actor or turn.status != "completed":
        raise DomainError("GAP_RESEARCH_BINDING", "补证调查来源不属于已完成问题。", 409)
    rows = {
        s.stage_key: s
        for s in db.scalars(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id,
                StageArtifact.stage_key.in_([ANSWER_COMPLETION_STAGE, "retrieval"]),
            )
        )
    }
    if len(rows) != 2:
        return None
    if any(s.input_hash != run.input_hash for s in rows.values()):
        raise DomainError("GAP_RESEARCH_BINDING", "补证调查来源版本已改变。", 409)
    assessment = rows[ANSWER_COMPLETION_STAGE].payload
    if assessment.get("binding", {}).get("turn_id") != turn_id or assessment.get(
        "assessment_hash"
    ) != fingerprint({k: v for k, v in assessment.items() if k != "assessment_hash"}):
        raise DomainError("GAP_RESEARCH_BINDING", "回答缺口记录无法验证。", 409)
    missing = list(
        dict.fromkeys(
            assessment.get("unsupported_aspects", []) + assessment.get("missing_aspects", [])
        )
    )
    if not missing:
        return None
    root = rows["retrieval"].payload["retrieval_manifest_id"]
    check_closure(db, actor, [root])
    return {
        "contract": "answer-gap-investigation-v1",
        "turn_id": turn_id,
        "assessment_hash": assessment["assessment_hash"],
        "access_dependencies": [root],
        "missing_aspects": missing[:20],
        "facts_approved": False,
    }


def validate(db, actor, snapshot):
    if snapshot != capture(db, actor, snapshot.get("turn_id")):
        raise DomainError("GAP_RESEARCH_BINDING", "固定补证目标与原回答缺口不一致。", 409)
    return snapshot
