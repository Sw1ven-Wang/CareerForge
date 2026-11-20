"""Exact-question gap admission and conservative, evidence-bound reconciliation.

This contract is used only for new schedules. Existing gap/build associations are
replayed without rewriting historical manifests, source reads, or model receipts.
A completed build is not a resolved learner question.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..contracts import KnowledgeBuildInput, TurnInput
from ..contracts_answer_completion_v166 import ANSWER_COMPLETION_STAGE
from ..db import locked
from ..errors import DomainError
from ..models import (
    AgentRun,
    AnswerBlock,
    Conversation,
    Idempotency,
    KnowledgeGap,
    ModelReceipt,
    Publication,
    ReviewDecision,
    StageArtifact,
    Turn,
    UnitVersion,
)
from ..util import digest, fingerprint
from .dependencies import check_closure
from .privacy import assert_personal_access
from .review_followup import bound_followup
from .runtime import TERMINAL
from .security import namespace_access

GAP_SCHEDULE_CONTRACT = "exact-question-gap-schedule-1.6.6"
GAP_RECONCILIATION_CONTRACT = "terminal-state-gap-reconciliation-1.6.6"
GAP_REANSWER_CONTRACT = "authorized-exact-gap-reanswer-v1"
AUTO_GAP_STAGE = "gap.auto_reanswer"
ANSWER_TERMINAL = {"completed", "failed", "interrupted", "cancelled"}


def _recorded_gap_request(db, actor, gap_id):
    record = db.scalar(
        select(Idempotency).where(
            Idempotency.principal_id == actor,
            Idempotency.operation == "run.create",
            Idempotency.key == "gap:" + gap_id,
        )
    )
    if record is None:
        return None
    run = db.get(AgentRun, record.result.get("run_id"))
    if run is None:
        raise DomainError("IDEMPOTENCY_RESULT_MISSING", "已记录的缺口任务不存在，禁止重建。", 409)
    return run


def schedule_legacy_gap(service, actor, gap_id, funding_id, call_cap):
    """Compatibility only for the unversioned six-argument, no-Turn protocol.

    A new API marker, any new scheduling options, any original Turn, or a stored
    marked build excludes this route. No historical manifest/receipt is upgraded.
    """
    with service.db.read() as db:
        generation = assert_personal_access(db, actor)
        gap = db.get(KnowledgeGap, gap_id)
        if gap is None or gap.principal_id != actor:
            raise DomainError("NOT_FOUND", "知识缺口不存在。", 404)
        namespace_access(db, actor, gap.namespace_id, True)
        if gap.turn_id is not None or gap.build_run_id is not None:
            return None
        recorded = _recorded_gap_request(db, actor, gap.id)
        if recorded is not None and "gap_schedule" in recorded.input_manifest:
            raise DomainError("GAP_BUILD_BINDING", "新合同缺口关联已损坏，禁止降级为旧调度。", 409)
        if gap.status == "scheduled":
            return {"run_id": gap.build_run_id, "replayed": True}
        namespace = gap.namespace_id
        topics, sources, reasons = (
            list(gap.topic_keys),
            list(gap.source_ids),
            list(gap.reason_codes),
        )
    body = KnowledgeBuildInput(
        namespace_id=namespace,
        funding_id=funding_id,
        source_id=sources[0] if len(sources) == 1 else None,
        question="补充知识缺口：" + ", ".join(topics or reasons),
        research_scope="focused",
        topic_keys=topics,
        call_cap=call_cap,
        idempotency_key="gap:" + gap_id,
    )
    # The original admission signature/hook stays intact. This private server
    # value adds a fence before budget reservation, without changing wire hashes.
    body._admission_privacy_generation = generation
    run_id = service.app.build.start(actor, body)
    with service.db.transaction() as db:
        assert_personal_access(db, actor, expected_generation=generation)
        current = locked(db, KnowledgeGap, gap_id)
        if current is None or current.principal_id != actor:
            raise DomainError("NOT_FOUND", "知识缺口不存在。", 404)
        namespace_access(db, actor, current.namespace_id, True)
        if current.turn_id is not None or current.build_run_id not in {None, run_id}:
            raise DomainError("GAP_BUILD_BINDING", "缺口身份已改变，禁止关联旧调度。", 409)
        current.build_run_id, current.status = run_id, "scheduled"
    return {"run_id": run_id, "automatic_publication_approval": False}


def _origin(db, actor: str, gap: KnowledgeGap) -> dict[str, Any]:
    turn = db.get(Turn, gap.turn_id) if gap.turn_id else None
    conversation = db.get(Conversation, turn.conversation_id) if turn else None
    if (
        turn is None
        or conversation is None
        or conversation.principal_id != actor
        or conversation.namespace_id != gap.namespace_id
        or turn.status != "completed"
    ):
        raise DomainError("GAP_ORIGIN", "缺口没有可验证的本人已完成问题，不能替换为主题标签。", 409)
    return {
        "gap_id": gap.id,
        "turn_id": turn.id,
        "conversation_id": conversation.id,
        "question": turn.question,
        "question_sha256": digest(turn.question),
        "topic_keys": list(gap.topic_keys),
        "source_ids": list(gap.source_ids),
        "reason_codes": list(gap.reason_codes),
        "namespace_id": gap.namespace_id,
        "principal_id": actor,
    }


def schedule_gap(
    service,
    actor,
    gap_id,
    funding_id,
    call_cap,
    key,
    *,
    contract_version=GAP_SCHEDULE_CONTRACT,
    source_id=None,
    max_projects=1,
    max_units_per_project=2,
    duration_minutes=30,
    planning_call_seconds=120,
    auto_reanswer=False,
    auto_evidence_retry=False,
):
    """Admit source-scoped work and its gap link in the same transaction."""
    with service.db.transaction() as db:
        generation = assert_personal_access(db, actor)
        gap = locked(db, KnowledgeGap, gap_id)
        if gap is None or gap.principal_id != actor:
            raise DomainError("NOT_FOUND", "知识缺口不存在。", 404)
        namespace_access(db, actor, gap.namespace_id, True)
        if contract_version != GAP_SCHEDULE_CONTRACT:
            raise DomainError("GAP_CONTRACT_VERSION", "不支持的缺口调度合同，禁止降级。", 422)
        request = {
            "funding_id": funding_id,
            "call_cap": call_cap,
            "idempotency_key": key,
            "source_id": source_id,
            "max_projects": max_projects,
            "max_units_per_project": max_units_per_project,
            "duration_minutes": duration_minutes,
            "planning_call_seconds": planning_call_seconds,
            **({"auto_reanswer": True} if auto_reanswer else {}),
            **({"auto_evidence_retry": True} if auto_evidence_retry else {}),
        }
        if gap.build_run_id:
            previous = db.get(AgentRun, gap.build_run_id)
            if (
                previous is None
                or previous.principal_id != actor
                or previous.namespace_id != gap.namespace_id
                or previous.workflow != "knowledge_build"
            ):
                raise DomainError("GAP_BUILD_BINDING", "缺口关联任务无法验证。", 409)
            contract = previous.input_manifest.get("gap_schedule")
            if "gap_schedule" in previous.input_manifest:
                _validated_contract(service, db, actor, gap, previous)
                if contract["request"] != request:
                    raise DomainError(
                        "GAP_SCHEDULE_CONFLICT", "缺口已绑定不同的固定来源或预算请求。", 409
                    )
            # Unmarked old runs retain their original contract and legacy replay.
            return {
                "gap_id": gap.id,
                "run_id": previous.id,
                "replayed": True,
                "status": gap.status,
                "build_status": previous.status,
                "contract_version": contract.get("version") if contract else None,
                "automatic_publication_approval": False,
                "resolved": gap.status == "resolved",
            }
        if _recorded_gap_request(db, actor, gap.id) is not None:
            raise DomainError("GAP_BUILD_BINDING", "历史缺口任务关联已丢失，禁止重新调度。", 409)
        origin = _origin(db, actor, gap)
        sources = origin["source_ids"]
        selected = source_id or (sources[0] if len(sources) == 1 else None)
        if not selected or (sources and selected not in sources):
            raise DomainError(
                "GAP_SOURCE_SCOPE", "请明确确认该缺口的单个已有来源；不会自动扩大或下载来源。", 422
            )
        if auto_reanswer and sources not in ([], [selected]):
            raise DomainError("GAP_SOURCE_SCOPE", "自动复答只接受同一个固定来源。", 422)
        if type(max_units_per_project) is not int or not 1 <= max_units_per_project <= 3:
            raise DomainError("GAP_UNIT_LIMIT", "缺口构建单元上限必须在1至3之间。", 422)
        if max_projects != 1:
            raise DomainError("GAP_SOURCE_SCOPE", "单来源缺口调度的项目上限必须为1。", 422)
        source = service.app.sources.get(db, actor, selected)
        followup = bound_followup(db, actor, service, gap, source)
        body = KnowledgeBuildInput(
            namespace_id=gap.namespace_id,
            funding_id=funding_id,
            source_id=selected,
            question=origin["question"],
            auto_evidence_retry=auto_evidence_retry,
            research_scope="focused",
            topic_keys=origin["topic_keys"],
            call_cap=call_cap,
            max_projects=max_projects,
            max_units_per_project=max_units_per_project,
            duration_minutes=duration_minutes,
            planning_call_seconds=planning_call_seconds,
            idempotency_key=key,
            **({"mechanism_read_targets": followup["read_targets"]} if followup else {}),
        )
        # Reuse the product's policy, source, namespace, taxonomy and planning
        # admission. No model request occurs in prepare_in or create_in.
        manifest = service.app.build.prepare_in(db, actor, body)
        from .gap_research_context import capture

        investigation_gap = capture(db, actor, origin["turn_id"])
        if investigation_gap:
            manifest["answer_gap_investigation"] = investigation_gap
        assert_personal_access(db, actor, expected_generation=generation)
        contract = {
            "version": GAP_SCHEDULE_CONTRACT,
            "origin": origin,
            "request": request,
            "selected_source_id": selected,
            "source_scope_hash": source.scope_hash,
            "source_commit": source.commit_sha,
            **(
                {"auto_reanswer_revision": db.get(Conversation, origin["conversation_id"]).revision}
                if auto_reanswer
                else {}
            ),
            **({"review_followup": followup} if followup else {}),
            **(
                {"review_retry_contract": "gap-bounded-review-retry-v1"}
                if auto_evidence_retry
                else {}
            ),
            "build_request_sha256": fingerprint(
                {
                    key: value
                    for key, value in manifest.items()
                    if key in KnowledgeBuildInput.model_fields
                }
            ),
        }
        manifest["gap_schedule"] = contract
        run = service.app.runtime.create_in(
            db,
            actor,
            gap.namespace_id,
            funding_id,
            "knowledge_build",
            manifest,
            call_cap,
            "gap:" + gap.id,
            duration_minutes,
        )
        gap.build_run_id, gap.status = run.id, "scheduled"
        return {
            "gap_id": gap.id,
            "run_id": run.id,
            "replayed": False,
            "status": gap.status,
            "build_status": run.status,
            "contract_version": GAP_SCHEDULE_CONTRACT,
            "gap_schedule": contract,
            "execution_plan": manifest["execution_plan"],
            "automatic_publication_approval": False,
            "resolved": False,
        }


def _validated_contract(service, db, actor, gap, run):
    contract = run.input_manifest.get("gap_schedule")
    if (
        not isinstance(contract, dict)
        or contract.get("version") != GAP_SCHEDULE_CONTRACT
        or fingerprint(run.input_manifest) != run.input_hash
        or run.principal_id != actor
        or run.namespace_id != gap.namespace_id
        or run.workflow != "knowledge_build"
        or run.parent_id is not None
        or contract.get("origin") != _origin(db, actor, gap)
    ):
        raise DomainError("GAP_BUILD_BINDING", "缺口、原始问题与固定构建合同不一致。", 409)
    source = service.app.sources.get(db, actor, contract["selected_source_id"])
    body = KnowledgeBuildInput.model_validate(
        {
            key: value
            for key, value in run.input_manifest.items()
            if key in KnowledgeBuildInput.model_fields
        }
    )
    followup = contract.get("review_followup")
    targets = [target.model_dump(mode="json") for target in body.mechanism_read_targets]
    exclude = {
        "auto_evidence_retry",
        "mechanism_edge_targets",
        "discover_mechanisms",
        "coverage_guided_analysis",
    }
    if contract.get("review_retry_contract"):
        if contract["review_retry_contract"] != "gap-bounded-review-retry-v1":
            raise DomainError("GAP_BUILD_BINDING", "未知的缺口补证重试合同。", 409)
        exclude.remove("auto_evidence_retry")
    if not followup:
        exclude.add("mechanism_read_targets")
    if (
        body.question != contract["origin"]["question"]
        or body.source_id != source.id
        or source.scope_hash != contract["source_scope_hash"]
        or source.commit_sha != contract["source_commit"]
        or targets != (followup or {}).get("read_targets", [])
        or (
            followup
            and (
                followup.get("source_id") != source.id
                or followup.get("scope_hash") != source.scope_hash
            )
        )
        or body.auto_evidence_retry
        != bool(contract.get("review_retry_contract") == "gap-bounded-review-retry-v1")
        or (bool(contract["request"].get("auto_evidence_retry")) != body.auto_evidence_retry)
        or body.coverage_guided_analysis
        or body.mechanism_edge_targets
        or body.discover_mechanisms
        or fingerprint(
            body.model_dump(
                mode="json",
                exclude=exclude,
            )
        )
        != contract["build_request_sha256"]
    ):
        raise DomainError("GAP_BUILD_BINDING", "来源或构建参数与原始问题不一致。", 409)
    return contract


def _stage(db, run, key):
    row = db.scalar(
        select(StageArtifact).where(StageArtifact.run_id == run.id, StageArtifact.stage_key == key)
    )
    if row is None:
        return {}
    if row.input_hash != run.input_hash:
        raise DomainError("GAP_BUILD_BINDING", "构建结果与固定请求不一致。", 409)
    return row.payload


def _verified_question_complete(service, db, actor, run, result, publications):
    """Recompute sufficiency from the real committed coverage response and anchors."""
    from pydantic import ValidationError

    from ..contracts_research_v163 import ResearchSufficiencyOutput
    from .research_coverage import evaluate_task_coverage, validate_research_run

    validate_research_run(db, actor, run)
    task = _stage(db, run, "research_context").get("research_task")
    sufficiency = _stage(db, run, "task_sufficiency")
    receipt = db.scalar(
        select(ModelReceipt).where(
            ModelReceipt.run_id == run.id, ModelReceipt.stage_key == "task.sufficiency"
        )
    )
    if (
        not task
        or not publications
        or receipt is None
        or receipt.status != "completed"
        or receipt.role != "coverage"
        or receipt.payer_id != actor
        or not receipt.response
        or receipt.response.get("synthetic") is not False
    ):
        return False
    try:
        response = ResearchSufficiencyOutput.model_validate(receipt.response.get("structured"))
    except ValidationError:
        return False
    source_units = {}
    for pack in publications.values():
        source_units.setdefault(pack["source_id"], []).append(pack["unit_version_id"])
    expected = [
        {
            "source_id": item.source_id,
            "unit_version_ids": sorted(source_units.get(item.source_id, [])),
            "coverage": item.coverage.model_dump(mode="json"),
        }
        for item in response.source_coverages
    ]
    if (
        any(item.source_id not in source_units for item in response.source_coverages)
        or sufficiency.get("task_coverages") != expected
        or set(sufficiency.get("publication_ids", [])) != set(publications)
        or sufficiency.get("sufficiency_errors")
    ):
        raise DomainError("GAP_COVERAGE_BINDING", "问题覆盖与独立审核回执不一致。", 409)
    child_id = _stage(db, run, "analysis_child").get("run_id")
    child = db.get(AgentRun, child_id) if child_id else None
    if child is None or child.parent_id != run.id or child.workflow != "analysis":
        raise DomainError("GAP_COVERAGE_BINDING", "问题覆盖缺少本次分析来源。", 409)
    analysis = _stage(db, child, "result")
    anchors, _ = service.app.build._published_evidence(actor, list(publications))
    computed = evaluate_task_coverage(task, analysis.get("task_coverages", []), expected, anchors)
    from .scoped_review import mark_partial_publication_coverage

    computed = mark_partial_publication_coverage(computed, list(publications.values()))
    if computed != result.get("task_coverage"):
        raise DomainError("GAP_COVERAGE_BINDING", "问题覆盖未由本次独立审核和已发布事实推出。", 409)
    return computed.get("question", {}).get("complete") is True


def _published(service, db, actor, run, result, source_id):
    valid, rejected = {}, []
    for publication_id in result.get("publication_ids", []):
        pack = service.app.knowledge.publication_pack(db, actor, publication_id)
        pub = db.get(Publication, publication_id)
        unit = db.get(UnitVersion, pub.unit_version_id)
        review = db.get(ReviewDecision, pub.review_id)
        analysis_run = db.get(AgentRun, unit.analysis_run_id)
        review_run = db.get(AgentRun, review.reviewer_run_id)
        if (
            pack.get("synthetic") is not False
            or pack["source_id"] != source_id
            or analysis_run is None
            or analysis_run.parent_id != run.id
            or review_run is None
            or review_run.parent_id != run.id
            or analysis_run.id == review_run.id
        ):
            rejected.append(publication_id)
        else:
            valid[publication_id] = pack
    return valid, rejected


def reanswer_gap(service, actor, gap_id, funding_id, *, automatic=False):
    """Admit one metered exact question against fresh independently reviewed publications."""
    key = "gap-reanswer:" + gap_id
    with service.db.read() as db:
        assert_personal_access(db, actor)
        gap = db.get(KnowledgeGap, gap_id)
        if gap is None or gap.principal_id != actor:
            raise DomainError("NOT_FOUND", "知识缺口不存在。", 404)
        namespace_access(db, actor, gap.namespace_id, True)
        run = db.get(AgentRun, gap.build_run_id) if gap.build_run_id else None
        if run is None or run.status not in {"succeeded", "partially_succeeded"}:
            raise DomainError("GAP_REANSWER_NOT_READY", "补证任务尚无可复答的结果。", 409)
        contract = _validated_contract(service, db, actor, gap, run)
        origin = contract["origin"]
        if automatic and (
            contract["request"].get("auto_reanswer") is not True
            or run.funding_id != funding_id
            or contract["request"]["funding_id"] != funding_id
        ):
            raise DomainError("GAP_REANSWER_AUTHORIZATION", "缺少原批次复答授权。", 409)
        if origin["source_ids"] not in ([], [contract["selected_source_id"]]):
            raise DomainError("GAP_SOURCE_SCOPE", "单来源补证不能覆盖原始跨仓问题。", 409)
        publications, _ = _published(
            service, db, actor, run, _stage(db, run, "result"), contract["selected_source_id"]
        )
        if not publications:
            raise DomainError("GAP_REANSWER_NOT_READY", "补证尚无有效的独立审核发布。", 409)
        binding = {
            "contract_version": GAP_REANSWER_CONTRACT,
            "gap_id": gap.id,
            "build_run_id": run.id,
            "original_turn_id": origin["turn_id"],
            "question_sha256": origin["question_sha256"],
            "source_id": contract["selected_source_id"],
            "publication_ids": sorted(publications),
            "topic_keys": origin["topic_keys"],
        }
        conv = db.get(Conversation, origin["conversation_id"])
        previous = db.scalar(
            select(Turn).where(Turn.conversation_id == conv.id, Turn.idempotency_key == key)
        )
        if previous:
            # Idempotent replay keeps the originally admitted evidence set.
            previous_binding = previous.focus.get("gap_reanswer", {})
            if "evidence_publication_ids" in previous_binding:
                binding["evidence_publication_ids"] = previous_binding["evidence_publication_ids"]
        else:
            evidence_ids = set(publications)
            for pub in service.app.knowledge.selected_publications(
                db, actor, [contract["selected_source_id"]]
            ):
                pack = service.app.knowledge.publication_pack(db, actor, pub.id)
                if (
                    pack["source_id"] == contract["selected_source_id"]
                    and pack.get("synthetic") is False
                ):
                    evidence_ids.add(pub.id)
            binding["evidence_publication_ids"] = sorted(evidence_ids)
        value = TurnInput(
            question=origin["question"],
            expected_revision=(
                previous.focus_revision
                if previous
                else contract["auto_reanswer_revision"]
                if automatic
                else conv.revision
            ),
            funding_id=funding_id,
            idempotency_key=key,
        )
        conversation_id = conv.id
    outcome = service.app.conversations.submit_gap_reanswer(actor, conversation_id, value, binding)
    return {
        **outcome,
        "gap_id": gap_id,
        "original_turn_id": binding["original_turn_id"],
        "publication_ids": binding["publication_ids"],
        "call_cap": 24,
    }


def process_auto_gap(service, build_run_id, *, after_answer=False):
    """Durable dispatcher callback. All spending still enters normal chat admission."""
    with service.db.read() as db:
        build = db.get(AgentRun, build_run_id)
        contract = (build.input_manifest.get("gap_schedule") or {}) if build else {}
        if not contract.get("request", {}).get("auto_reanswer"):
            return
        actor, funding_id = build.principal_id, build.funding_id
        gap_id = contract["origin"]["gap_id"]
        if not after_answer and _stage(db, build, AUTO_GAP_STAGE):
            return
    if after_answer:
        try:
            reconcile_gap(service, actor, gap_id)
        except DomainError:
            # The authorization fence takes priority over writing reconciliation.
            pass
        return
    try:
        submitted = reanswer_gap(service, actor, gap_id, funding_id, automatic=True)
        outcome = {
            "status": "submitted",
            "turn_id": submitted["turn_id"],
            "run_id": submitted["run_id"],
        }
    except DomainError as exc:
        outcome = {"status": "blocked", "reason_code": exc.code}
    with service.db.transaction() as db:
        build = locked(db, AgentRun, build_run_id)
        if not _stage(db, build, AUTO_GAP_STAGE):
            db.add(
                StageArtifact(
                    run_id=build.id,
                    stage_key=AUTO_GAP_STAGE,
                    input_hash=build.input_hash,
                    payload=outcome,
                )
            )
    if outcome["status"] == "blocked":
        try:
            reconcile_gap(service, actor, gap_id)
        except DomainError:
            pass


def _answer_reasons(service, db, actor, contract, run, result, publications, answer_turn_id):
    reasons = []
    turn = db.get(Turn, answer_turn_id) if answer_turn_id else None
    bound = turn.focus.get("gap_reanswer") if turn else None
    if not bound and not _verified_question_complete(service, db, actor, run, result, publications):
        reasons.append("original_question_coverage_incomplete")
    if contract["origin"]["source_ids"] not in ([], [contract["selected_source_id"]]):
        reasons.append("original_multi_source_gap_not_fully_covered")
    if not answer_turn_id:
        return [*reasons, "exact_question_reanswer_required"]
    conv = db.get(Conversation, turn.conversation_id) if turn else None
    if (
        turn is None
        or conv is None
        or conv.principal_id != actor
        or conv.namespace_id != run.namespace_id
        or turn.id == contract["origin"]["turn_id"]
        or turn.question != contract["origin"]["question"]
    ):
        raise DomainError("GAP_ANSWER_BINDING", "必须使用本人对原始问题逐字重问的新回答。", 409)
    if turn.created_at <= run.created_at:
        reasons.append("answer_predates_new_analysis")
    if turn.status != "completed":
        reasons.append("answer_not_completed")
    answer_run = db.get(AgentRun, turn.run_id) if turn.run_id else None
    if (
        answer_run is None
        or answer_run.workflow != "chat"
        or answer_run.principal_id != actor
        or answer_run.status != "succeeded"
    ):
        reasons.append("answer_run_not_succeeded")
    if turn.focus.get("project_source_ids") != [contract["selected_source_id"]]:
        reasons.append("answer_source_scope_changed")
    if bound:
        if (
            bound.get("contract_version") != GAP_REANSWER_CONTRACT
            or bound.get("gap_id") != contract["origin"]["gap_id"]
            or bound.get("build_run_id") != run.id
            or bound.get("question_sha256") != digest(turn.question)
            or bound.get("publication_ids") != sorted(publications)
            or not answer_run
            or answer_run.input_manifest.get("gap_reanswer") != bound
        ):
            raise DomainError("GAP_ANSWER_BINDING", "复答与本次补证发布不一致。", 409)
        evidence_ids = bound.get("evidence_publication_ids", bound["publication_ids"])
        if len(evidence_ids) != len(set(evidence_ids)) or not set(publications) <= set(
            evidence_ids
        ):
            raise DomainError("GAP_ANSWER_BINDING", "复答未保留本次新增审核知识。", 409)
        for identifier in evidence_ids:
            pack = service.app.knowledge.publication_pack(db, actor, identifier)
            if (
                pack["source_id"] != contract["selected_source_id"]
                or pack.get("synthetic") is not False
            ):
                raise DomainError("GAP_ANSWER_BINDING", "复答证据不属于已授权固定源码。", 409)
        assessment = _stage(db, answer_run, ANSWER_COMPLETION_STAGE)
        if (
            assessment.get("task_satisfied") is not True
            or assessment.get("status") != "complete"
            or assessment.get("synthetic") is not False
            or assessment.get("binding", {}).get("turn_id") != turn.id
            or assessment.get("assessment_hash")
            != fingerprint(
                {key: value for key, value in assessment.items() if key != "assessment_hash"}
            )
        ):
            reasons.append("answer_completeness_not_verified")
    manifest = (
        db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == turn.run_id, StageArtifact.stage_key == "retrieval"
            )
        )
        if turn.run_id
        else None
    )
    if manifest is None or answer_run is None or manifest.input_hash != answer_run.input_hash:
        reasons.append("answer_retrieval_missing")
    else:
        check_closure(db, actor, [manifest.payload["retrieval_manifest_id"]])
        from .dependencies import require_object

        selection = require_object(
            db, actor, manifest.payload["retrieval_manifest_id"], "selection_manifest"
        )
        if selection.payload.get("task_spec", {}).get("project_source_ids") != [
            contract["selected_source_id"]
        ]:
            reasons.append("answer_retrieval_source_scope_changed")
        if bound and selection.payload.get("task_spec", {}).get(
            "pinned_publication_ids"
        ) != bound.get("evidence_publication_ids", bound["publication_ids"]):
            reasons.append("answer_retrieval_publication_scope_changed")
    blocks = list(db.scalars(select(AnswerBlock).where(AnswerBlock.turn_id == turn.id)))
    fact_ids, cited_publications = set(), set()
    facts = []
    for block in blocks:
        check_closure(db, actor, [block.id])
        from .dependencies import require_object

        obj = require_object(db, actor, block.id, "answer_block")
        if (
            block.content_hash != fingerprint(block.content)
            or obj.payload != block.content
            or obj.content_hash != block.content_hash
        ):
            raise DomainError("GAP_ANSWER_BINDING", "回答内容摘要已改变。", 409)
        content = block.content
        if content.get("kind") == "evidence_gap":
            reasons.append("answer_still_has_gap")
        if content.get("kind") in {"repository_fact", "concept_fact"}:
            facts.append(content)
            if (
                content.get("synthetic") is not False
                or content.get("checked") not in {"exact", "semantic_checked", "exact_fallback"}
                or content.get("relevance_checked") != "independent_model"
            ):
                reasons.append("answer_fact_not_independently_checked")
            fact_ids.update(content.get("fact_atom_ids", []))
            cited_publications.update(content.get("publication_ids", []))
            if not content.get("citations") or any(
                c.get("source_id") != contract["selected_source_id"] for c in content["citations"]
            ):
                reasons.append("answer_citation_scope_changed")
    if not facts:
        reasons.append("no_checked_fact_answer")
    fresh_facts = {fact["fact_atom_id"] for pack in publications.values() for fact in pack["facts"]}
    if not (cited_publications & set(publications)) or not (fact_ids & fresh_facts):
        reasons.append("new_reviewed_publication_not_used")
    return list(dict.fromkeys(reasons))


def _answer_state(db, actor, contract, run, answer_turn_id):
    if not answer_turn_id:
        return None, None
    turn = db.get(Turn, answer_turn_id)
    conv = db.get(Conversation, turn.conversation_id) if turn else None
    if (
        turn is None
        or conv is None
        or conv.principal_id != actor
        or conv.namespace_id != run.namespace_id
        or turn.id == contract["origin"]["turn_id"]
        or turn.question != contract["origin"]["question"]
    ):
        raise DomainError("GAP_ANSWER_BINDING", "必须使用本人对原始问题逐字重问的新回答。", 409)
    answer_run = db.get(AgentRun, turn.run_id) if turn.run_id else None
    if turn.run_id and (
        answer_run is None
        or answer_run.principal_id != actor
        or answer_run.namespace_id != run.namespace_id
        or answer_run.workflow != "chat"
        or fingerprint(answer_run.input_manifest) != answer_run.input_hash
    ):
        raise DomainError("GAP_ANSWER_BINDING", "回答任务身份或固定输入不一致。", 409)
    return turn, answer_run


def _reconciliation_state(db, actor, run, contract, turn, answer_run, publications):
    """Bind cache identity to observed versions, never just a mutable turn ID."""
    from .dependencies import require_object

    runs = [run, *db.scalars(select(AgentRun).where(AgentRun.parent_id == run.id))]
    if answer_run is not None:
        runs.append(answer_run)
    runs.sort(key=lambda value: value.id)
    if any(
        row.principal_id != actor
        or row.namespace_id != run.namespace_id
        or fingerprint(row.input_manifest) != row.input_hash
        for row in runs
    ):
        raise DomainError("GAP_BUILD_BINDING", "关联任务身份或输入不一致。", 409)
    ids = [row.id for row in runs]
    receipts = list(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id.in_(ids))))
    pending = []
    if any(row.status not in TERMINAL for row in runs):
        pending.append(
            "build_not_terminal" if run.status not in TERMINAL else "related_run_not_terminal"
        )
    if turn is not None and turn.status not in ANSWER_TERMINAL:
        pending.append("answer_not_terminal")
    if any(row.status != "completed" for row in receipts):
        # Unknown attempts remain untouched. Polling is not authority to retry.
        pending.append("model_receipt_state_uncertain")
    stages = list(
        db.scalars(
            select(StageArtifact).where(
                StageArtifact.run_id.in_(ids),
                ~StageArtifact.stage_key.startswith("gap.reconciliation"),
            )
        )
    )
    hashes = {row.id: row.input_hash for row in runs}
    if any(row.input_hash != hashes[row.run_id] for row in stages):
        raise DomainError("GAP_BUILD_BINDING", "关联阶段与固定任务输入不一致。", 409)
    blocks = (
        list(db.scalars(select(AnswerBlock).where(AnswerBlock.turn_id == turn.id))) if turn else []
    )
    source = require_object(db, actor, contract["selected_source_id"], "source_set")
    dependencies = {source.id}
    dependencies.update(publications)
    dependencies.update(block.id for block in blocks)
    if turn and turn.manifest_id:
        dependencies.add(turn.manifest_id)
    # Revalidate every answer dependency even when the build failed, and before
    # consulting a historical terminal cache entry.
    closure = check_closure(db, actor, sorted(dependencies))
    objects = [require_object(db, actor, identifier) for identifier in sorted(dependencies)]
    for block in blocks:
        obj = require_object(db, actor, block.id, "answer_block")
        if (
            block.content_hash != fingerprint(block.content)
            or obj.payload != block.content
            or obj.content_hash != block.content_hash
        ):
            raise DomainError("GAP_ANSWER_BINDING", "回答内容摘要已改变。", 409)
    return {
        "contract_version": GAP_RECONCILIATION_CONTRACT,
        "gap_contract_hash": fingerprint(contract),
        "runs": [
            {
                "id": row.id,
                "input_hash": row.input_hash,
                "status": row.status,
                "revision": row.revision,
                "epoch": row.epoch,
                "budget_settled": row.budget_settled,
            }
            for row in runs
        ],
        "stages": [
            {
                "id": row.id,
                "run_id": row.run_id,
                "stage_key": row.stage_key,
                "input_hash": row.input_hash,
                "payload_hash": fingerprint(row.payload),
            }
            for row in sorted(stages, key=lambda value: value.id)
        ],
        "receipts": [
            {
                "id": row.id,
                "run_id": row.run_id,
                "stage_key": row.stage_key,
                "status": row.status,
                "request_hash": row.request_hash,
                "response_hash": fingerprint(row.response),
                "role": row.role,
                "profile_version": row.profile_version,
            }
            for row in sorted(receipts, key=lambda value: value.id)
        ],
        "answer": {
            "id": turn.id,
            "status": turn.status,
            "run_id": turn.run_id,
            "question_hash": digest(turn.question),
            "focus_revision": turn.focus_revision,
            "focus_hash": fingerprint(turn.focus),
            "policy_hash": fingerprint(turn.policy_versions),
            "manifest_id": turn.manifest_id,
        }
        if turn
        else None,
        "answer_blocks": [
            {"id": row.id, "ordinal": row.ordinal, "content_hash": row.content_hash}
            for row in sorted(blocks, key=lambda value: value.id)
        ],
        "publications": {key: fingerprint(value) for key, value in sorted(publications.items())},
        "dependency_closure": {
            "checked_objects": closure.checked_objects,
            "closure_hash": closure.closure_hash,
        },
        "dependencies": [
            {
                "id": row.id,
                "revision": row.revision,
                "state": row.state,
                "content_hash": row.content_hash,
            }
            for row in objects
        ],
    }, pending


def reconcile_gap(service, actor, gap_id, answer_turn_id=None):
    """Revalidate current evidence; cache only a fully observed terminal state."""
    with service.db.transaction() as db:
        assert_personal_access(db, actor)
        gap = locked(db, KnowledgeGap, gap_id)
        if gap is None or gap.principal_id != actor:
            raise DomainError("NOT_FOUND", "知识缺口不存在。", 404)
        namespace_access(db, actor, gap.namespace_id, True)
        run = db.get(AgentRun, gap.build_run_id) if gap.build_run_id else None
        if run is None:
            return {
                "gap_id": gap.id,
                "status": gap.status,
                "resolved": False,
                "reason_codes": ["no_scheduled_build"],
            }
        if "gap_schedule" not in run.input_manifest:
            return {
                "gap_id": gap.id,
                "run_id": run.id,
                "status": gap.status,
                "build_status": run.status,
                "resolved": False,
                "reason_codes": ["legacy_build_contract_unchanged"],
            }
        contract = _validated_contract(service, db, actor, gap, run)
        if answer_turn_id is None:
            answer_turn_id = db.scalar(
                select(Turn.id).where(
                    Turn.conversation_id == contract["origin"]["conversation_id"],
                    Turn.idempotency_key == "gap-reanswer:" + gap.id,
                )
            )
        turn, answer_run = _answer_state(db, actor, contract, run, answer_turn_id)
        reasons, publications = [], {}
        result = _stage(db, run, "result")
        if run.status not in TERMINAL:
            reasons.append("build_not_terminal")
        elif run.status not in {"succeeded", "partially_succeeded"}:
            reasons.append("build_not_successful")
        else:
            publications, rejected = _published(
                service, db, actor, run, result, contract["selected_source_id"]
            )
            if rejected:
                reasons.append("publication_not_fresh_independent_real_source")
            if not publications:
                reasons.append("no_valid_new_publication")
            reasons.extend(
                _answer_reasons(
                    service, db, actor, contract, run, result, publications, answer_turn_id
                )
            )
        state, pending = _reconciliation_state(
            db, actor, run, contract, turn, answer_run, publications
        )
        state_hash = fingerprint(state)
        base = {
            "contract_version": GAP_SCHEDULE_CONTRACT,
            "gap_id": gap.id,
            "run_id": run.id,
            "original_turn_id": contract["origin"]["turn_id"],
            "question_sha256": contract["origin"]["question_sha256"],
            "answer_turn_id": answer_turn_id,
            "build_status": run.status,
            "publication_ids": sorted(publications),
            "historical_turn_rewritten": False,
            "additional_model_calls": 0,
            **(
                {"auto_reanswer": _stage(db, run, AUTO_GAP_STAGE) or {"status": "pending"}}
                if contract["request"].get("auto_reanswer")
                else {}
            ),
            **(
                {
                    "answer_calls_consumed": answer_run.calls_consumed if answer_run else 0,
                    "supplement_calls_consumed": run.calls_consumed,
                    "answer_retrieval_manifest_id": turn.manifest_id,
                }
                if turn and turn.focus.get("gap_reanswer")
                else {}
            ),
        }
        if pending:
            # No gap status change and no permanent decision while a prerequisite
            # can still complete or an unknown model attempt remains unresolved.
            return {
                **base,
                "status": gap.status,
                "resolved": False,
                "reason_codes": list(dict.fromkeys([*reasons, *pending])),
                "reconciliation_status": "pending",
                "replayed": False,
                "reconciliation_contract_version": GAP_RECONCILIATION_CONTRACT,
                "state_hash": state_hash,
                "final": False,
            }
        resolved = not reasons
        status = (
            "resolved"
            if resolved
            else "awaiting_answer"
            if reasons == ["exact_question_reanswer_required"]
            else "unresolved"
        )
        core = {
            **base,
            "status": status,
            "resolved": resolved,
            "reason_codes": list(dict.fromkeys(reasons)),
        }
        payload = {
            **core,
            "reconciliation_contract_version": GAP_RECONCILIATION_CONTRACT,
            "state_hash": state_hash,
            "state_snapshot": state,
            "reconciliation_status": "terminal",
            "final": True,
        }
        stage_key = "gap.reconciliation.v2:" + state_hash
        existing = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id, StageArtifact.stage_key == stage_key
            )
        )
        if existing is not None:
            if existing.input_hash != run.input_hash or existing.payload != payload:
                raise DomainError(
                    "GAP_RECONCILIATION_BINDING", "终态缺口决议与实际状态版本不一致。", 409
                )
            gap.status = status
            return {**existing.payload, "replayed": True}
        legacy = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id,
                StageArtifact.stage_key
                == "gap.reconciliation:" + (answer_turn_id or "build_terminal"),
            )
        )
        if legacy is not None:
            if legacy.input_hash != run.input_hash:
                raise DomainError("GAP_BUILD_BINDING", "缺口终态记录与任务不一致。", 409)
            # A historical final conclusion is replayable only if the freshly
            # revalidated terminal evidence reaches the identical conclusion.
            # The RC5 pending-cache defect is retained as history, not replayed.
            if legacy.payload == core:
                gap.status = status
                return {**payload, "replayed": True, "legacy_terminal_replay": True}
        gap.status = status
        db.add(
            StageArtifact(
                run_id=run.id, stage_key=stage_key, input_hash=run.input_hash, payload=payload
            )
        )
        return payload
