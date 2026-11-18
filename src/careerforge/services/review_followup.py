"""Owner-only gap hints derived from review decisions, never Tutor evidence."""

from sqlalchemy import select

from ..contracts import MechanismReadTarget
from ..contracts_answer_completion_v166 import ANSWER_COMPLETION_STAGE, EVIDENCE_ANSWER_STATE
from ..errors import DomainError
from ..models import (
    AgentRun,
    Claim,
    CodeSymbol,
    KnowledgeUnit,
    ReviewDecision,
    StageArtifact,
    Turn,
    UnitVersion,
)
from ..util import fingerprint
from .dependencies import check_closure, require_object
from .retrieval_relevance import _terms

FOLLOWUP_REASON = "review_partially_supported"


def review_followup(db, actor, knowledge, source, question, namespace):
    question_terms = _terms(question) - _terms("如何 怎样 项目 实现 工具 调用 代码 什么 怎么")
    candidates = []
    rows = db.execute(
        select(ReviewDecision, UnitVersion)
        .join(UnitVersion, UnitVersion.id == ReviewDecision.unit_version_id)
        .join(KnowledgeUnit, KnowledgeUnit.id == UnitVersion.unit_id)
        .join(AgentRun, AgentRun.id == ReviewDecision.reviewer_run_id)
        .where(
            UnitVersion.source_id == source.id,
            KnowledgeUnit.namespace_id == namespace,
            AgentRun.principal_id == actor,
            AgentRun.workflow == "review",
            AgentRun.status.in_(("succeeded", "partially_succeeded")),
        )
        .order_by(UnitVersion.revision_no.desc(), ReviewDecision.id)
        .limit(30)
    )
    for review, version in rows:
        try:
            obj = require_object(db, actor, review.id, "review_decision")
            check_closure(db, actor, [review.id, source.id])
        except DomainError:
            continue
        if (
            obj.namespace_id != namespace
            or obj.payload != review.report
            or review.reviewed_content_hash != version.content_hash
            or review.reviewed_render_hash != version.render_hash
        ):
            continue
        standalone = set((review.report.get("scope_review") or {}).get("standalone_claim_ids", []))
        for decision in review.report.get("claim_decisions", []):
            if (
                decision.get("outcome") != "partially_supported"
                or decision["claim_id"] in standalone
            ):
                continue
            claim = db.get(Claim, decision["claim_id"])
            if claim is None or claim.unit_version_id != version.id:
                continue
            overlap = question_terms & _terms(" ".join([claim.statement, *claim.conditions]))
            if not overlap:
                continue
            refs = []
            for identifier in decision.get("independent_evidence_ids", [])[:2]:
                try:
                    ref = knowledge.evidence_dict(db, actor, identifier)
                except DomainError:
                    continue
                if ref["source_id"] == source.id:
                    refs.append(ref)
            targets = _followup_windows(db, source, refs)
            if targets:
                candidates.append(
                    (
                        len(overlap),
                        version.revision_no,
                        {
                            "reason": FOLLOWUP_REASON,
                            "source_id": source.id,
                            "scope_hash": source.scope_hash,
                            "review_id": review.id,
                            "claim_id": claim.id,
                            "read_targets": targets,
                        },
                    )
                )
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row[0], row[1], row[2]["review_id"]))[2]


def _followup_windows(db, source, refs):
    windows = {}
    for ref in refs:
        path = ref["path"]
        first, last = ref["start_line"], ref["end_line"]
        length = len(source.files[path].splitlines())
        symbol = db.scalar(
            select(CodeSymbol)
            .where(
                CodeSymbol.source_id == source.id,
                CodeSymbol.path == path,
                CodeSymbol.start_line <= first,
                CodeSymbol.end_line >= last,
            )
            .order_by(CodeSymbol.end_line - CodeSymbol.start_line, CodeSymbol.id)
            .limit(1)
        )
        name = symbol.name[:200] if symbol else "review-window"
        if symbol and symbol.start_line < first - 4:
            end = min(length, symbol.start_line + 17)
            target = MechanismReadTarget(
                path=path,
                symbol=name,
                start_line=symbol.start_line,
                end_line=end,
                purpose="boundary",
            )
            windows[(path, symbol.start_line, end)] = target.model_dump(mode="json")
        start = max(1, first - 4)
        end = min(length, max(last, first + 9), start + 29)
        target = MechanismReadTarget(
            path=path, symbol=name, start_line=start, end_line=end, purpose="boundary"
        )
        windows[(path, start, end)] = target.model_dump(mode="json")
    return list(windows.values())[:4]


def bound_followup(db, actor, service, gap, source):
    if FOLLOWUP_REASON not in gap.reason_codes:
        return None
    turn = db.get(Turn, gap.turn_id)
    if turn.run_id:
        stage = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == turn.run_id,
                StageArtifact.stage_key == ANSWER_COMPLETION_STAGE,
            )
        )
        active = db.get(AgentRun, turn.run_id)
        assessment = (
            stage.payload if stage is not None and stage.input_hash == active.input_hash else {}
        )
    else:
        assessment = turn.focus.get(EVIDENCE_ANSWER_STATE, {}).get("assessment", {})
    recorded = assessment.get("review_followup")
    current = review_followup(
        db, actor, service.app.knowledge, source, turn.question, gap.namespace_id
    )
    if (
        not recorded
        or recorded != current
        or assessment.get("binding", {}).get("turn_id") != turn.id
        or assessment.get("assessment_hash")
        != fingerprint({k: v for k, v in assessment.items() if k != "assessment_hash"})
    ):
        raise DomainError(
            "GAP_REVIEW_STALE", "补证对象或审核记录已改变，请重新查询当前知识缺口。", 409
        )
    return recorded
