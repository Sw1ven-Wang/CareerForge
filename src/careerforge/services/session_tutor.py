"""Explicit accepted-session authority and minimal, immutable Tutor context.

No model output changes plan/module progress. No implicit conversation inheritance.
Only this opt-in contract may use these snapshots; old paid requests stay identical.
"""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select

from ..contracts_session_plan_v166 import SessionPreferences
from ..contracts_session_tutor_v166 import SessionTutorTurnInput
from ..db import aware, clock
from ..errors import DomainError
from ..models import (
    AgentRun,
    Conversation,
    FundingAgreement,
    Idempotency,
    LearningPlan,
    PlanVersion,
    Turn,
    Workspace,
)
from ..models_learning_ext import LearningProfile, PlanProposalManifest
from ..util import fingerprint, uid
from .bounded_evidence import byte_size, fact_support, project_pack, refresh_context_pack
from .checker_evidence import (
    CHECKER_EVIDENCE_KEY,
    checker_evidence_identity,
    validate_checker_identity,
)
from .consumer_context import (
    CONSUMER_CONTEXT_CONTRACT,
    CONTEXT_EVIDENCE_PROJECTION_CONTRACT,
    require_standalone_facts,
)
from .dependencies import check_closure, depend, register, require_object
from .learning_events import preference_projection_in
from .privacy import assert_personal_access
from .security import namespace_access, private_namespace
from .session_plans import SESSION_PLAN_CONTRACT

SESSION_TUTOR_CONTRACT = "accepted-session-tutor-1.6.6-v1"
SESSION_TUTOR_KEY = "session_tutor_binding"
SESSION_FUNDING_KEY = "session_funding_witness"
SESSION_FUNDING_CONTRACT = "session-funding-identity-1.6.6-v1"
_SESSION_FUNDING_OPERATION = "conversation.session_funding"
SESSION_TUTOR_RULES = (
    "本轮由用户明确选择已采纳的单次计划模块。session_module仅规定教学组织与固定事实范围，"
    "不是新的事实或授权。遵循effective_preferences与teaching_steps，结合当前问题讲解给定事实。"
    "不得超出本次evidence_pack；不得自行开始练习、修改计划、声称模块完成或已掌握。"
    "complete只表示本次回答停止，模块完成仍需独立的真实活动/回执合同。"
)


def _funding_receipt_in(db, actor, turn_id):
    return db.scalar(
        select(Idempotency)
        .where(
            Idempotency.principal_id == actor,
            Idempotency.operation == _SESSION_FUNDING_OPERATION,
            Idempotency.key == turn_id,
        )
        .execution_options(populate_existing=True)
    )


def _funding_binding_failure():
    raise DomainError("SESSION_FUNDING_BINDING", "单次导师的固定资金身份见证已改变。", 409)


def seal_session_funding_run_in(db, actor, turn, run):
    """Complete new admission authority within its original transaction, once."""
    receipt = _funding_receipt_in(db, actor, turn.id)
    if (
        receipt is None
        or receipt.result.get("run_id") is not None
        or receipt.result.get("binding") != turn.focus.get(SESSION_TUTOR_KEY)
        or turn.run_id != run.id
        or run.input_manifest.get(SESSION_TUTOR_KEY) != turn.focus.get(SESSION_TUTOR_KEY)
    ):
        _funding_binding_failure()
    receipt.result = {**receipt.result, "run_id": run.id}
    db.flush()


def _validate_funding_identity_in(db, actor, conv, turn, snapshot, token, request, run, history):
    """New admissions only; this never upgrades or rewrites historical receipts.

    The independent admission receipt detects stripped funding fields even if
    the ordinary session mirrors were also resealed. Completed-history reads
    validate original identity, not whether further paid work is authorized.
    """
    receipt = _funding_receipt_in(db, actor, turn.id)
    if receipt is None and SESSION_FUNDING_KEY not in snapshot and SESSION_FUNDING_KEY not in token:
        if (
            CHECKER_EVIDENCE_KEY in token
            and token[CHECKER_EVIDENCE_KEY] == checker_evidence_identity()
        ):
            _funding_binding_failure()
        return
    authority = {
        "contract": SESSION_FUNDING_CONTRACT,
        "funding_id": request.funding_id,
        "payer_id": actor if request.funding_id else None,
        "namespace_id": conv.namespace_id,
    }
    marker = {"contract": SESSION_FUNDING_CONTRACT, "authority_hash": fingerprint(authority)}
    if (
        receipt is None
        or snapshot.get(SESSION_FUNDING_KEY) != authority
        or token.get(SESSION_FUNDING_KEY) != marker
        or receipt.request_hash != snapshot["request_hash"]
        or receipt.result != {"binding": token, "authority": authority, "run_id": turn.run_id}
        or bool(request.funding_id) != bool(turn.run_id)
    ):
        _funding_binding_failure()
    if not turn.run_id:
        if run is not None:
            _funding_binding_failure()
        return
    if run is None:
        run = db.get(AgentRun, turn.run_id, populate_existing=True)
    if (
        run is None
        or run.id != turn.run_id
        or run.principal_id != actor
        or run.namespace_id != conv.namespace_id
        or run.workflow != "chat"
        or run.input_manifest.get("turn_id") != turn.id
        or run.input_manifest.get(SESSION_TUTOR_KEY) != token
        or run.input_manifest.get("funding_id") != authority["funding_id"]
        or run.funding_id != authority["funding_id"]
        or fingerprint(run.input_manifest) != run.input_hash
    ):
        _funding_binding_failure()
    if not history:
        funding = db.scalar(
            select(FundingAgreement)
            .where(FundingAgreement.id == run.funding_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if (
            funding is None
            or funding.status != "active"
            or aware(funding.expires_at) <= clock(db)
            or funding.payer_id != authority["payer_id"]
            or funding.namespace_id != authority["namespace_id"]
        ):
            raise DomainError("FUNDING_UNAVAILABLE", "单次导师的费用授权或付款身份已改变。", 403)


def _authority(
    db, actor, conv, request, *, history=False, allow_learning_pause=False, immutable_delivery=False
):
    generation = assert_personal_access(db, actor)
    ns = private_namespace(db, actor)
    if conv.principal_id != actor or conv.namespace_id != ns.id or request.namespace_id != ns.id:
        raise DomainError("SESSION_TUTOR_SCOPE", "单次导师请求只能绑定本人当前私人会话。", 403)
    namespace_access(db, actor, ns.id, True)
    plan_query = select(LearningPlan).where(LearningPlan.id == request.plan_id)
    if not immutable_delivery:
        plan_query = plan_query.with_for_update(read=True)
    plan = db.scalar(plan_query.execution_options(populate_existing=True))
    version = db.get(PlanVersion, request.version_id)
    if (
        not plan
        or plan.principal_id != actor
        or plan.namespace_id != ns.id
        or not version
        or version.plan_id != plan.id
        or request.module_id not in version.module_ids
    ):
        raise DomainError("NOT_FOUND", "所选计划、版本或模块不可访问。", 404)
    profile_query = select(LearningProfile).where(LearningProfile.principal_id == actor)
    if not immutable_delivery:
        profile_query = profile_query.with_for_update(read=True)
    profile = db.scalar(profile_query.execution_options(populate_existing=True))
    if not history and (
        plan.active_version_id != version.id
        or profile is None
        or profile.namespace_id != ns.id
        or profile.active_plan_id != plan.id
        or profile.active_module_id != request.module_id
    ):
        raise DomainError("SESSION_TUTOR_INACTIVE", "须显式选择当前已采纳计划的活动模块。", 409)
    if not history and plan.revision != request.expected_plan_revision:
        raise DomainError("REVISION_CONFLICT", "所选计划已经变化。", 409)
    if not history and db.scalar(
        select(Workspace.id).where(
            Workspace.principal_id == actor,
            Workspace.assistance_mode == "independent",
            Workspace.sealed.is_(False),
        )
    ):
        raise DomainError("INDEPENDENT_ATTEMPT", "独立练习期间不能启动或恢复导师。")
    module = require_object(db, actor, request.module_id, "plan_module")
    proposal = db.scalar(
        select(PlanProposalManifest).where(PlanProposalManifest.plan_version_id == version.id)
    )
    if (
        module.namespace_id != ns.id
        or module.payload.get("session_contract") != SESSION_PLAN_CONTRACT
        or proposal is None
        or proposal.principal_id != actor
        or proposal.content_hash != fingerprint(proposal.payload)
        or proposal.payload.get("input", {}).get("session_contract") != SESSION_PLAN_CONTRACT
        or proposal.payload.get("module_ids") != version.module_ids
        or proposal.payload.get("module_hashes", {}).get(module.id) != module.content_hash
    ):
        raise DomainError("SESSION_TUTOR_PLAN", "所选模块缺少完整的单次计划合同。", 409)
    check_closure(db, actor, [module.id, proposal.selection_manifest_id])
    frozen = proposal.payload.get("input", {})
    if (frozen.get("goal") or {}).get("origin_contract") == "command-goal-v1":
        from ..models import GoalVersion
        from ..models_dialogue import WorkflowSession
        from .goal_plan_v1 import goal_payload, validate_plan_scope_in
        from .research_coverage import read_goal_version

        validate_plan_scope_in(db, actor, frozen)
        goal = frozen["goal"]
        snapshot = read_goal_version(db, actor, goal["goal_version_id"])
        if goal_payload(db.get(GoalVersion, goal["goal_version_id"]), snapshot) != goal:
            raise DomainError("PLAN_GOAL_CHANGED", "导师的新增目标版本已变化。", 409)
        scope = frozen.get("dialogue_scope")
        workflow = db.get(WorkflowSession, scope.get("workflow_id")) if scope else None
        if (
            scope
            and (
                workflow is None
                or workflow.conversation_id != conv.id
                or workflow.control_state
                not in (
                    {"active", "pause_requested", "paused"} if allow_learning_pause else {"active"}
                )
            )
            and not history
        ):
            raise DomainError("SESSION_TUTOR_SCOPE", "导师必须使用未暂停的原目标会话。", 409)
    return generation, plan, version, module, proposal


def freeze_session_tutor_in(app, db, actor, conv, turn, value, *, module_guidance=False):
    generation, plan, version, module, proposal = _authority(db, actor, conv, value)
    app.learning._validated_plan_manifest(db, actor, version)
    content = module.payload
    pack = app.knowledge.publication_pack(
        db, actor, content["publication_id"], consumer_context_contract=CONSUMER_CONTEXT_CONTRACT
    )
    fact_ids = content["focus_fact_ids"]
    require_standalone_facts(pack, set(fact_ids))
    scope = {
        "topic_keys": [link["topic_key"] for link in pack["topic_links"]],
        "project_source_ids": [pack["source_id"]],
        "preferences": conv.preferences,
    }
    projection = preference_projection_in(
        db, actor, scope, conversation_id=conv.id, turn_id=turn.id
    )
    session_preferences = SessionPreferences.model_validate(
        proposal.payload["input"]["session_request"]["preferences"]
    ).model_dump(exclude_none=True)
    # Existing conversation/turn opt-outs outrank a plan's one-session defaults.
    # Explicit fields in this new request outrank both. Nothing is written back.
    profile = db.scalar(select(LearningProfile).where(LearningProfile.principal_id == actor))
    effective = {
        "diagnostics_enabled": profile.preferences.get("diagnostics_enabled", True),
        **projection["preferences"],
        **session_preferences,
    }
    preference_sources = {
        key: {"kind": "conversation_default" if key in conv.preferences else "profile_default"}
        for key in effective
    }
    for event_id in projection["applied_event_ids"]:
        event = require_object(db, actor, event_id, "learning_preference_event").payload
        preference_sources[event["key"]] = {
            "kind": event["lifetime"] + "_preference_event",
            "event_id": event_id,
        }
    preference_sources.update({key: {"kind": "accepted_session"} for key in session_preferences})
    for event_id in projection["applied_event_ids"]:
        event = require_object(db, actor, event_id, "learning_preference_event").payload
        if event["lifetime"] in {"conversation", "turn"}:
            effective[event["key"]] = event["value"]
            preference_sources[event["key"]] = {
                "kind": event["lifetime"] + "_preference_event",
                "event_id": event_id,
            }
    explicit = value.preferences.model_dump(exclude_none=True)
    effective.update(explicit)
    preference_sources.update({key: {"kind": "turn_request"} for key in explicit})
    effective = SessionPreferences.model_validate(
        {k: v for k, v in effective.items() if k in SessionPreferences.model_fields}
    ).model_dump(exclude_none=True)
    selection_id = uid()
    bindings, citations = fact_support(app.knowledge, db, actor, pack)
    unit = project_pack(
        pack,
        set(fact_ids),
        bindings,
        citations,
        selection_id,
        force_projection=True,
        allowed_facet_ids=set(),
    )
    # Reuse the existing request-byte bound. No raised budget or partial fact clipping.
    if byte_size(unit) > 100_000:
        raise DomainError("MODEL_CONTEXT_LIMIT", "完整模块证据超过单次导师上下文限制。", 413)
    record = {
        "publication_id": pack["publication_id"],
        "publication_hash": fingerprint(pack),
        "selected_fact_ids": fact_ids,
        "fact_support": {fid: bindings[fid] for fid in fact_ids},
        "selected_facet_ids": [],
        "projection_hash": fingerprint(unit),
        "mode": "fact_projection",
    }
    schedule = next(
        item for item in proposal.payload["session_schedule"] if item["module_id"] == module.id
    )
    from .learning_ext import RUBRIC_TEXT, STEP_TEXT

    if (
        not set(content["steps"]) <= STEP_TEXT.keys()
        or not set(content["rubric_keys"]) <= RUBRIC_TEXT.keys()
    ):
        raise DomainError("SESSION_TUTOR_MODULE", "模块组织字段不属于受限教学合同。", 409)
    request = value.model_dump(mode="json")
    snapshot = {
        **({"module_guidance": True} if module_guidance else {}),
        "contract": SESSION_TUTOR_CONTRACT,
        CHECKER_EVIDENCE_KEY: checker_evidence_identity(),
        SESSION_FUNDING_KEY: {
            "contract": SESSION_FUNDING_CONTRACT,
            "funding_id": value.funding_id,
            "payer_id": actor if value.funding_id else None,
            "namespace_id": conv.namespace_id,
        },
        "principal_id": actor,
        "namespace_id": conv.namespace_id,
        "conversation_id": conv.id,
        "turn_id": turn.id,
        "learning_session_id": uid(),
        "privacy_generation": generation,
        "request": request,
        "request_hash": fingerprint(request),
        "plan_manifest_hash": proposal.content_hash,
        "module_hash": module.content_hash,
        "effective_preferences": effective,
        "preference_sources": {key: preference_sources[key] for key in effective},
        "budget": {
            "model_calls_authorized": value.model_call_cap if value.funding_id else 0,
            "requested_model_call_cap": value.model_call_cap,
            "ideal_minimum_calls_without_rework": 2 * len(fact_ids) + 1,
            "meets_ideal_minimum": bool(
                value.funding_id and value.model_call_cap >= 2 * len(fact_ids) + 1
            ),
            "fact_count": len(fact_ids),
            "maximum_fact_steps": 12,
            "coverage_guaranteed": False,
            "basis": "one_accepted_fact_per_step_no_rework_plus_one_completion_check",
        },
        "learning_preferences": {**projection, "preferences": effective},
        "module": {
            "plan_id": plan.id,
            "version_id": version.id,
            "module_id": module.id,
            "module_content_hash": module.content_hash,
            "plan_revision": plan.revision,
            "conversation_revision": value.expected_revision,
            "publication_id": pack["publication_id"],
            "unit_version_id": pack["unit_version_id"],
            "source_id": pack["source_id"],
            "focus_fact_ids": fact_ids,
            "steps": content["steps"],
            "teaching_steps": [STEP_TEXT[key] for key in content["steps"]],
            "rubric_keys": content["rubric_keys"],
            "activity_kind": schedule["activity_kind"],
            "estimated_minutes": schedule["estimated_minutes"],
            "start_offset_minutes": schedule["start_offset_minutes"],
            "end_offset_minutes": schedule["end_offset_minutes"],
            "module_completed": False,
            "competence_verified": False,
        },
        "task_spec": {
            "turn_id": turn.id,
            "user_question": turn.question,
            "task_type": "explain_implementation",
            "project_source_ids": [pack["source_id"]],
            "topic_keys": scope["topic_keys"],
        },
        "evidence_projection": {
            "contract": CONTEXT_EVIDENCE_PROJECTION_CONTRACT,
            "consumer_context_contract": CONSUMER_CONTEXT_CONTRACT,
            "units": [record],
        },
    }
    manifest = register(db, actor, conv.namespace_id, "selection_manifest", snapshot, selection_id)
    depend(db, actor, manifest.id, module.id, "binds_session_module")
    depend(db, actor, manifest.id, pack["publication_id"], "selects_source")
    token = {
        "contract": SESSION_TUTOR_CONTRACT,
        CHECKER_EVIDENCE_KEY: snapshot[CHECKER_EVIDENCE_KEY],
        SESSION_FUNDING_KEY: {
            "contract": SESSION_FUNDING_CONTRACT,
            "authority_hash": fingerprint(snapshot[SESSION_FUNDING_KEY]),
        },
        "manifest_id": manifest.id,
        "snapshot_hash": manifest.content_hash,
        "request_hash": snapshot["request_hash"],
    }
    db.add(
        Idempotency(
            principal_id=actor,
            operation="conversation.session_tutor",
            key=turn.id,
            request_hash=snapshot["request_hash"],
            result=token,
        )
    )
    db.add(
        Idempotency(
            principal_id=actor,
            operation=_SESSION_FUNDING_OPERATION,
            key=turn.id,
            request_hash=snapshot["request_hash"],
            result={"binding": token, "authority": snapshot[SESSION_FUNDING_KEY], "run_id": None},
        )
    )
    db.flush()
    from .session_progress import create_teaching_session_in

    create_teaching_session_in(db, actor, snapshot, token)
    return token, snapshot


def validate_session_tutor_in(
    db,
    actor,
    turn,
    *,
    run=None,
    history=False,
    allow_learning_pause=False,
    immutable_delivery=False,
):
    if immutable_delivery:
        from .session_turn import has_delivery_in

        if (
            not history
            or run is None
            or run.graph_version != "learning-tutor-v1"
            or not has_delivery_in(db, run)
        ):
            raise DomainError("LEARNING_DELIVERY_REQUIRED", "不可变读取需要真实交付见证。", 409)
    token = turn.focus.get(SESSION_TUTOR_KEY)
    receipt = db.scalar(
        select(Idempotency).where(
            Idempotency.principal_id == actor,
            Idempotency.operation == "conversation.session_tutor",
            Idempotency.key == turn.id,
        )
    )
    if (receipt is not None and receipt.result != token) or (token is not None and receipt is None):
        raise DomainError("SESSION_TUTOR_BINDING", "显式导师请求的固定绑定不能删除或改写。", 409)
    run_token = run.input_manifest.get(SESSION_TUTOR_KEY) if run else None
    if token is None:
        if run_token is not None or _funding_receipt_in(db, actor, turn.id) is not None:
            raise DomainError("SESSION_TUTOR_BINDING", "任务与会话绑定不一致。", 409)
        return None
    if not isinstance(token, dict) or token.get("contract") != SESSION_TUTOR_CONTRACT:
        raise DomainError("SESSION_TUTOR_BINDING", "未知单次导师绑定合同。", 409)
    if receipt.request_hash != token.get("request_hash"):
        raise DomainError("SESSION_TUTOR_BINDING", "显式导师请求内容已变化。", 409)
    if run and (run_token != token or run.input_manifest.get("turn_id") != turn.id):
        raise DomainError("SESSION_TUTOR_BINDING", "任务与会话绑定不一致。", 409)
    conv = db.get(Conversation, turn.conversation_id)
    if not conv or conv.principal_id != actor:
        raise DomainError("NOT_FOUND", "会话不可访问。", 404)
    manifest = require_object(db, actor, token["manifest_id"], "selection_manifest")
    snapshot = manifest.payload
    if (
        manifest.content_hash != token.get("snapshot_hash")
        or snapshot.get("contract") != SESSION_TUTOR_CONTRACT
        or snapshot.get("principal_id") != actor
        or snapshot.get("namespace_id") != conv.namespace_id
        or snapshot.get("conversation_id") != conv.id
        or snapshot.get("turn_id") != turn.id
        or token.get("request_hash") != fingerprint(snapshot.get("request"))
        or snapshot.get("request_hash") != token.get("request_hash")
    ):
        raise DomainError("SESSION_TUTOR_BINDING", "固定导师请求的身份或内容已改变。", 409)
    request = SessionTutorTurnInput.model_validate(snapshot["request"])
    _validate_funding_identity_in(db, actor, conv, turn, snapshot, token, request, run, history)
    validate_checker_identity(token, snapshot)
    if run and run.call_cap != request.model_call_cap:
        raise DomainError("SESSION_TUTOR_BINDING", "导师调用额度与显式请求不一致。", 409)
    if turn.question != request.question or turn.focus_revision != request.expected_revision:
        raise DomainError("SESSION_TUTOR_BINDING", "导师问题或会话版本已改变。", 409)
    assert_personal_access(db, actor, expected_generation=snapshot["privacy_generation"])
    if allow_learning_pause:
        from .learning_requests import validate_delivered_request_in

        if run is None or run.graph_version != "learning-tutor-v1" or history:
            raise DomainError("LEARNING_PAUSE_BINDING", "暂停心跳仅限固定学习运行。", 409)
        validate_delivered_request_in(
            db, run, run.input_manifest.get("learning_tutor_request"), allow_pause=True
        )
    _, _, _, module, proposal = _authority(
        db,
        actor,
        conv,
        request,
        history=history,
        allow_learning_pause=allow_learning_pause,
        immutable_delivery=immutable_delivery,
    )
    if (
        module.content_hash != snapshot["module_hash"]
        or proposal.content_hash != snapshot["plan_manifest_hash"]
    ):
        raise DomainError("SESSION_TUTOR_BINDING", "采纳模块的固定内容已改变。", 409)
    if any(
        turn.focus.get(key) != snapshot["task_spec"][key]
        for key in ("topic_keys", "project_source_ids")
    ):
        raise DomainError("SESSION_TUTOR_BINDING", "固定导师模块范围已改变。", 409)
    if (
        turn.focus.get("preferences") != snapshot["effective_preferences"]
        or turn.focus.get("learning_preferences") != snapshot["learning_preferences"]
    ):
        raise DomainError("SESSION_TUTOR_BINDING", "固定导师偏好已改变。", 409)
    session = require_object(db, actor, snapshot["learning_session_id"], "learning_session")
    if (
        session.payload.get("session_tutor_binding") != token
        or session.payload.get("turn_id") != turn.id
        or session.payload.get("module") != snapshot["module"]
    ):
        raise DomainError("SESSION_TUTOR_BINDING", "教学活动会话与导师快照不一致。", 409)
    check_closure(db, actor, [manifest.id, session.id])
    return snapshot


def validate_session_funding_in(db, actor, run, token):
    from .dependencies import assert_source_scope
    from .runtime import active_funding

    funding = active_funding(db, run)
    deps = [token["manifest_id"]]
    closure = check_closure(db, actor, deps)
    classes = {"learning_state", *closure.source_data_classes}
    if not {"tutor", "answer_checker"} <= set(funding.allowed_roles) or not classes <= set(
        funding.allowed_data_classes
    ):
        raise DomainError("EGRESS_OR_ROLE_DENIED", "本次导师所需角色或数据外发类别未获授权。", 403)
    assert_source_scope(db, deps, funding.allowed_source_ids)


def validate_session_run_in(db, run, *, allow_learning_pause=False):
    # This server admission anchor survives removal of BOTH the run's forward
    # hints and Turn.run_id. It is not mutable caller-selected conversation state.
    authority = db.scalar(
        select(Idempotency)
        .where(
            Idempotency.operation == _SESSION_FUNDING_OPERATION,
            Idempotency.result["run_id"].as_string() == run.id,
        )
        .execution_options(populate_existing=True)
    )
    if authority is not None:
        turn = db.get(Turn, authority.key, populate_existing=True)
        if turn is None:
            _funding_binding_failure()
        validate_session_tutor_in(
            db, authority.principal_id, turn, run=run, allow_learning_pause=allow_learning_pause
        )
        return
    # New funding authority is found by the server-side reverse link, not only
    # removable hints in a run manifest. Keep the original unmarked path below.
    bound = db.scalar(
        select(Turn).where(Turn.run_id == run.id).execution_options(populate_existing=True)
    )
    if bound is not None:
        conv = db.get(Conversation, bound.conversation_id, populate_existing=True)
        token = bound.focus.get(SESSION_TUTOR_KEY)
        if (isinstance(token, dict) and SESSION_FUNDING_KEY in token) or (
            conv is not None and _funding_receipt_in(db, conv.principal_id, bound.id) is not None
        ):
            if conv is None:
                _funding_binding_failure()
            validate_session_tutor_in(
                db, conv.principal_id, bound, run=run, allow_learning_pause=allow_learning_pause
            )
            return
    run_token = run.input_manifest.get(SESSION_TUTOR_KEY)
    if isinstance(run_token, dict) and SESSION_FUNDING_KEY in run_token:
        _funding_binding_failure()
    if run.workflow != "chat":
        return
    turn_id = run.input_manifest.get("turn_id")
    turn = db.get(Turn, turn_id) if turn_id else None
    if turn is None:
        if SESSION_TUTOR_KEY in run.input_manifest:
            raise DomainError("SESSION_TUTOR_BINDING", "固定导师任务缺少会话。", 409)
        return
    validate_session_tutor_in(db, run.principal_id, turn, run=run)


def session_evidence_in(knowledge, db, actor, turn, run=None):
    snapshot = validate_session_tutor_in(db, actor, turn, run=run)
    if snapshot is None:
        raise DomainError("SESSION_TUTOR_BINDING", "缺少显式单次导师绑定。", 409)
    manifest_id = turn.focus[SESSION_TUTOR_KEY]["manifest_id"]
    unit = refresh_context_pack(
        knowledge, db, actor, snapshot["module"]["publication_id"], manifest_id
    )
    return {
        "retrieval_manifest_id": manifest_id,
        "knowledge_units": [unit],
        "uncovered_questions": [],
        "session_tutor_binding": deepcopy(turn.focus[SESSION_TUTOR_KEY]),
    }


def session_model_context(db, actor, focus, run, units):
    token = focus.get(SESSION_TUTOR_KEY)
    if token is None:
        return {}, []
    if not run:
        return {}, []
    active = db.get(AgentRun, run[0])
    turn = db.get(Turn, active.input_manifest.get("turn_id")) if active else None
    if turn is None:
        raise DomainError("SESSION_TUTOR_BINDING", "固定导师任务缺少会话。", 409)
    snapshot = validate_session_tutor_in(db, actor, turn, run=active)
    if turn.focus.get(SESSION_TUTOR_KEY) != token:
        raise DomainError("SESSION_TUTOR_BINDING", "调用使用了其他导师快照。", 409)
    available = {f["fact_atom_id"] for unit in units for f in unit["facts"]}
    module = snapshot["module"]
    if (
        not available
        or not available <= set(module["focus_fact_ids"])
        or any(unit["publication_id"] != module["publication_id"] for unit in units)
    ):
        raise DomainError("SESSION_TUTOR_BINDING", "导师候选事实超出固定模块。", 409)
    return {
        "session_module": {
            **module,
            "contract": SESSION_TUTOR_CONTRACT,
            "snapshot_hash": token["snapshot_hash"],
            "binding_manifest_id": token["manifest_id"],
            "learning_session_id": snapshot["learning_session_id"],
            "focus_fact_ids": sorted(available),
            "teaching_steps": [
                text
                for step, text in zip(module["steps"], module["teaching_steps"], strict=True)
                if step != "transfer_variant"
                or snapshot["effective_preferences"].get("practice", True)
            ],
            "effective_preferences": snapshot["effective_preferences"],
            "preference_sources": snapshot["preference_sources"],
            "content_scope": "closed_teaching_codes_and_current_call_reviewed_facts",
        }
    }, [token["manifest_id"]]
