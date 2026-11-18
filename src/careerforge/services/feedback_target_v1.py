"""Opt-in, one-use style feedback on an exact completed answer continuation.

This is a new semantic authority, not a reinterpretation of old preference events.
The target is frozen independently of mutable conversation navigation. Topic hints
remain context, never source permission, factual evidence or durable consent.
"""

from __future__ import annotations

import inspect
from copy import deepcopy
from importlib import import_module
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from ..contracts_feedback_target_v1 import (
    FEEDBACK_TARGET_CHOICE,
    FEEDBACK_TARGET_CONTRACT,
    FEEDBACK_TARGET_KEY,
    AnswerFeedbackConversationInput,
)
from ..contracts_ordinary_checker_v166 import ORDINARY_CHECKER_KEY
from ..errors import DomainError
from ..models import (
    AgentRun,
    Conversation,
    Experience,
    Feedback,
    Idempotency,
    ObjectRow,
    StageArtifact,
    Turn,
)
from ..util import fingerprint
from .dependencies import assert_source_scope, check_closure, depend, register, require_object
from .ordinary_checker import conversation_contract_in, validate_ordinary_turn_in
from .privacy import assert_personal_access
from .sources import SourceService

_SUBMIT = "answer_feedback.submit.v1"
_TURN = "answer_feedback.turn.v1"
_CONSUME = "answer_feedback.consume.v1"
_RUN = "answer_feedback.run.v1"
_TARGET_KIND = "answer_feedback_target"
_TURN_KIND = "answer_feedback_turn"
_MODE_CONFLICT = "目标答案反馈只支持下一次相同问题、相同来源的一次性样式调整。请保留或明确修改你指定的范围和期限；当前设置未改变。"


def creation_request(value):
    return {
        "contract": FEEDBACK_TARGET_CONTRACT,
        "choice": value.model_dump(mode="json"),
        "implementation_hash": fingerprint(
            {
                name: inspect.getsource(import_module("careerforge." + name))
                for name in (
                    "services.feedback_target_v1",
                    "services.evolution",
                    "contracts_feedback_target_v1",
                    "services.learning_events",
                    "services.preference_scope_v166",
                    "services.preference_control_v166",
                )
            }
        ),
    }


def _fail(message="目标答案反馈的固定绑定缺失、冲突或已改变。"):
    raise DomainError("FEEDBACK_TARGET_BINDING", message, 409)


def _receipt(db, actor, operation, key):
    return db.scalar(
        select(Idempotency)
        .where(
            Idempotency.principal_id == actor,
            Idempotency.operation == operation,
            Idempotency.key == key,
        )
        .execution_options(populate_existing=True)
    )


def _token(obj):
    return {
        "contract": FEEDBACK_TARGET_CONTRACT,
        "manifest_id": obj.id,
        "manifest_hash": obj.content_hash,
    }


def _read_token(db, actor, token, kind, namespace, *, live=True):
    if not isinstance(token, dict) or set(token) != {"contract", "manifest_id", "manifest_hash"}:
        _fail()
    obj = require_object(db, actor, token["manifest_id"], kind, active=live)
    if (
        token != _token(obj)
        or obj.namespace_id != namespace
        or obj.payload.get("principal_id") != actor
        or obj.payload.get("namespace_id") != namespace
        or obj.payload.get("contract") != FEEDBACK_TARGET_CONTRACT
    ):
        _fail()
    assert_personal_access(db, actor, expected_generation=obj.payload["privacy_generation"])
    if live:
        check_closure(db, actor, [obj.id])
    return obj


def conversation_choice_in(db, actor, conv):
    """Only a new, server-frozen creation request can enable this contract."""
    ordinary = conversation_contract_in(db, actor, conv)
    request = ordinary.get("request") if ordinary else None
    marker = conv.preferences.get(FEEDBACK_TARGET_CHOICE)
    requested = isinstance(request, dict) and request.get("contract") == FEEDBACK_TARGET_CONTRACT
    if not requested and marker is None:
        return None
    if (
        not requested
        or marker != FEEDBACK_TARGET_CONTRACT
        or ordinary["admission"] != "new_conversation"
    ):
        _fail("此会话没有在创建时明确采纳目标答案反馈合同，不能追溯升级。")
    try:
        choice = AnswerFeedbackConversationInput.model_validate(request.get("choice"))
    except ValueError:
        _fail()
    if choice.contract != marker or request != creation_request(choice):
        _fail()
    return ordinary


def _stage(db, run, key):
    row = db.scalar(
        select(StageArtifact).where(StageArtifact.run_id == run.id, StageArtifact.stage_key == key)
    )
    if row is None or row.input_hash != run.input_hash:
        _fail("目标答案缺少与原运行绑定的已完成阶段。")
    return row.payload


def _selection(db, actor, turn):
    selection = require_object(db, actor, turn.manifest_id, "selection_manifest")
    task = selection.payload.get("task_spec", {})
    conv = db.get(Conversation, turn.conversation_id)
    if (
        selection.namespace_id != conv.namespace_id
        or selection.payload.get("turn_id") != turn.id
        or task.get("turn_id") != turn.id
        or task.get("user_question") != turn.question
        or task.get("focus_revision") != turn.focus_revision
        or selection.payload.get("answer_checker_contract") != turn.focus.get(ORDINARY_CHECKER_KEY)
    ):
        _fail("检索选择不属于这条固定问题。")
    sources = sorted(set(task.get("project_source_ids", [])))
    selected = sorted(set(selection.payload.get("selected_source_ids", [])))
    if not sources or not selected or not set(selected) <= set(sources):
        _fail("目标答案必须有明确且一致的固定来源，不能猜测项目范围。")
    roots = [selection.id, *sources]
    closure = check_closure(db, actor, roots)
    assert_source_scope(db, [selection.id], sources)
    for identifier in sources:
        SourceService.get(db, actor, identifier)
    hashes = {
        identifier: require_object(db, actor, identifier, "source_set").content_hash
        for identifier in sources
    }
    return selection, sources, selected, hashes, closure.closure_hash


def _target_evidence(db, actor, conv, turn):
    if (
        turn is None
        or turn.conversation_id != conv.id
        or turn.status != "completed"
        or not turn.run_id
        or not turn.manifest_id
    ):
        _fail("只能针对本会话已完成且有固定检索证据的普通答案反馈。")
    run = db.get(AgentRun, turn.run_id, populate_existing=True)
    ordinary = validate_ordinary_turn_in(db, actor, turn, run=run, history=True)
    if ordinary is None or run is None or run.status != "succeeded":
        _fail()
    # A newly enabled conversation cannot adopt an older unmarked target turn.
    _read_turn(db, actor, conv, turn)
    selection, sources, selected, hashes, closure = _selection(db, actor, turn)
    orchestration, result = _stage(db, run, "orchestration"), _stage(db, run, "result")
    intent, decision = orchestration.get("intent", {}), orchestration.get("decision", {})
    task = selection.payload["task_spec"]
    if (
        task.get("topic_keys", [])
        != (
            intent.get("retrieval_topic_keys")
            if intent.get("retrieval_topic_keys") is not None
            else intent.get("topic_keys", [])
        )
        or task.get("topic_hints", []) != intent.get("topic_hints", [])
        or task.get("task_type") != intent.get("interpreted_intent")
        or decision.get("action") != "invoke_tutor"
        or result.get("turn_id") != turn.id
        or result.get("retrieval_manifest_id") != selection.id
        or sorted(set(intent.get("project_source_ids", []))) != sources
        or sorted(set(decision.get("source_version_refs", []))) != sources
        or decision.get("principal_id") != actor
        or conv.namespace_id not in decision.get("authorized_namespace_refs", [])
    ):
        _fail("目标答案的运行、路由和检索来源不一致。")
    return {
        "turn_id": turn.id,
        "run_id": run.id,
        "conversation_id": conv.id,
        "question_hash": fingerprint(turn.question),
        "run_input_hash": run.input_hash,
        "turn_focus_hash": fingerprint(turn.focus),
        "ordinary_binding": deepcopy(turn.focus[ORDINARY_CHECKER_KEY]),
        "selection_id": selection.id,
        "selection_hash": selection.content_hash,
        "source_ids": sources,
        "selected_source_ids": selected,
        "source_hashes": hashes,
        "dependency_closure_hash": closure,
        "orchestration_hash": fingerprint(orchestration),
        "result_hash": fingerprint(result),
        "context_only": {
            "hard_topic_keys": selection.payload["task_spec"].get("topic_keys", []),
            "soft_topic_hints": selection.payload["task_spec"].get("topic_hints", []),
            "facts_approved": False,
            "grants_permissions": False,
        },
    }


def feedback_replay_in(db, actor, conv, value):
    if conversation_choice_in(db, actor, conv) is None:
        _fail("请先明确创建采用目标答案反馈合同的新会话；旧会话不升级。")
    old = _receipt(db, actor, _SUBMIT, value.idempotency_key)
    if old is None:
        return None
    if old.request_hash != fingerprint(value.model_dump(mode="json")):
        raise DomainError("IDEMPOTENCY_CONFLICT", "反馈键对应不同的完整请求。", 409)
    token = old.result.get("answer_feedback", {}).get("binding")
    if token:
        _validate_target(db, actor, conv, token, old)
    return deepcopy(old.result)


def prepare_target_in(db, actor, conv, turn, content, feedback_id, clarifications):
    from .learning_events import capture_dialogue_preferences_in
    from .preference_scope_v166 import dialogue_preference_scope

    # Target-only fallback is permitted only after validating this completed
    # answer's immutable provenance, never from mutable current navigation.
    _target_evidence(db, actor, conv, turn)
    plans = capture_dialogue_preferences_in(
        db,
        actor,
        conv,
        turn,
        content=content,
        feedback_id=feedback_id,
        clarifications=clarifications,
        prepare_only=True,
        target_answer_scope=True,
    )
    if not plans:
        if not clarifications:
            clarifications.append(_MODE_CONFLICT)
        return None
    if len(plans) != 1:
        clarifications.append(_MODE_CONFLICT)
        return None
    plan = plans[0]
    value = plan["value"]
    if (
        value.key != "requested_style"
        or value.lifetime != "turn"
        or value.durable_consent
        or value.operation != "set"
    ):
        clarifications.append(_MODE_CONFLICT)
        return None
    # Detect the parser's unqualified fallback without inferring a topic from
    # routing hints. All explicit names/references retain the old parser's scope.
    neutral, error = dialogue_preference_scope(db, actor, plan["scope_text"], {})
    _, unnamed = dialogue_preference_scope(db, actor, plan["scope_text"], {}, require_named=True)
    unqualified = not error and neutral.kind == "global" and bool(unnamed)
    if not unqualified and value.scope.kind == "global":
        clarifications.append(_MODE_CONFLICT)
        return None
    return {
        "preferences": {value.key: value.value},
        "scope": {"kind": "target_answer"} if unqualified else value.scope.model_dump(mode="json"),
        "lifetime": "turn",
        "durable_consent": False,
    }


def save_feedback_in(db, actor, conv, turn, row, value, result, plan):
    token = None
    if plan is not None:
        target = _target_evidence(db, actor, conv, turn)
        payload = {
            "contract": FEEDBACK_TARGET_CONTRACT,
            "principal_id": actor,
            "namespace_id": conv.namespace_id,
            "privacy_generation": assert_personal_access(db, actor),
            "conversation_id": conv.id,
            "conversation_binding": deepcopy(conv.preferences[ORDINARY_CHECKER_KEY]),
            "target": target,
            "source_feedback_id": row.id,
            "feedback_content_hash": fingerprint(row.content),
            "request_hash": fingerprint(value.model_dump(mode="json")),
            "idempotency_key": value.idempotency_key,
            **plan,
        }
        obj = register(db, actor, conv.namespace_id, _TARGET_KIND, payload)
        for identifier in [
            target["selection_id"],
            target["ordinary_binding"]["manifest_id"],
            *target["source_ids"],
        ]:
            depend(db, actor, obj.id, identifier, "targets_answer")
        token = _token(obj)
    if token is not None and result.get("experience_id"):
        episode = db.get(Experience, result["experience_id"])
        if episode is None or episode.feedback_id != row.id or not value.consent:
            _fail()
        # Preserve the explicit temporary scope in consent-bound reflection
        # inputs. This annotation grants neither durable style nor promotion.
        episode.payload = {
            **episode.payload,
            "answer_feedback": {
                "binding": token,
                "scope": plan["scope"],
                "lifetime": "turn",
                "durable_consent": False,
                "global_policy_changed": False,
            },
        }
    result["answer_feedback"] = {
        "contract": FEEDBACK_TARGET_CONTRACT,
        "conversation_id": conv.id,
        "accepted": token is not None,
        "status": "pending_matching_turn" if token else "clarification_required",
        "binding": token,
        "delivered": False,
    }
    db.add(
        Idempotency(
            principal_id=actor,
            operation=_SUBMIT,
            key=value.idempotency_key,
            request_hash=fingerprint(value.model_dump(mode="json")),
            result=deepcopy(result),
        )
    )
    return result


def _validate_target(db, actor, conv, token, receipt=None):
    obj = _read_token(db, actor, token, _TARGET_KIND, conv.namespace_id)
    data = obj.payload
    receipt = receipt or _receipt(db, actor, _SUBMIT, data["idempotency_key"])
    if (
        receipt is None
        or receipt.request_hash != data["request_hash"]
        or receipt.result.get("answer_feedback", {}).get("binding") != token
        or data["conversation_id"] != conv.id
        or data["conversation_binding"] != conv.preferences.get(ORDINARY_CHECKER_KEY)
        or data["lifetime"] != "turn"
        or data["durable_consent"] is not False
    ):
        _fail()
    feedback = db.scalar(
        select(Feedback)
        .where(Feedback.id == data["source_feedback_id"])
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        feedback is None
        or feedback.principal_id != actor
        or feedback.turn_id != data["target"]["turn_id"]
        or feedback.signal != "explicit_feedback"
        or feedback.origin != "user_explicit"
        or fingerprint(feedback.content) != data["feedback_content_hash"]
    ):
        _fail("原反馈已撤回或改变，不能消费其样式授权。")
    target = db.get(Turn, data["target"]["turn_id"], populate_existing=True)
    if _target_evidence(db, actor, conv, target) != data["target"]:
        _fail("目标答案的来源或固定证据已改变。")
    return obj


def _scope_matches(scope, focus):
    kind = scope["kind"]
    if kind == "target_answer":
        return True
    if kind == "topic":
        return scope["identifier"] in set(
            focus.get("topic_keys", []) + focus.get("topic_hints", [])
        )
    if kind == "source":
        return scope["identifier"] in focus.get("project_source_ids", [])
    if kind == "project_facts":
        return bool(focus.get("project_source_ids"))
    _fail()


def _consumed_in(db, actor, conv, target):
    """Two immutable receipts agree; deleting a claim cannot make it reusable."""
    claim = _receipt(db, actor, _CONSUME, target.id)
    witnesses = [
        row
        for row in db.scalars(
            select(ObjectRow).where(
                ObjectRow.namespace_id == conv.namespace_id, ObjectRow.kind == _TURN_KIND
            )
        )
        if row.payload.get("conversation_id") == conv.id
        and any(token.get("manifest_id") == target.id for token in row.payload.get("targets", []))
    ]
    if not claim and not witnesses:
        return False
    if claim is None or len(witnesses) != 1:
        _fail()
    witness = _read_token(
        db, actor, _token(witnesses[0]), _TURN_KIND, conv.namespace_id, live=False
    )
    turn = db.get(Turn, witness.payload["turn_id"])
    receipt = _receipt(db, actor, _TURN, witness.payload["turn_id"])
    if (
        claim.request_hash != target.content_hash
        or claim.result != {"turn_id": witness.payload["turn_id"], "binding": _token(witness)}
        or _token(target) not in witness.payload["targets"]
        or turn is None
        or turn.conversation_id != conv.id
        or turn.focus.get(FEEDBACK_TARGET_KEY) != _token(witness)
        or receipt is None
        or receipt.request_hash != witness.content_hash
        or receipt.result != _token(witness)
    ):
        _fail()
    return True


def freeze_preferences_in(db, actor, conv, turn, projection, *, session=False):
    if conversation_choice_in(db, actor, conv) is None:
        return projection
    if session:
        _fail("目标答案反馈会话仅支持普通对话，不能隐式用于计划导师。")
    eligible = []
    for raw in db.scalars(
        select(ObjectRow).where(
            ObjectRow.namespace_id == conv.namespace_id, ObjectRow.kind == _TARGET_KIND
        )
    ):
        if raw.payload.get("conversation_id") == conv.id and raw.payload.get("target", {}).get(
            "question_hash"
        ) == fingerprint(turn.question):
            receipt = _receipt(db, actor, _SUBMIT, raw.payload.get("idempotency_key"))
            if receipt is None or receipt.result.get("answer_feedback", {}).get(
                "binding"
            ) != _token(raw):
                _fail()
    for receipt in db.scalars(
        select(Idempotency).where(
            Idempotency.principal_id == actor, Idempotency.operation == _SUBMIT
        )
    ):
        outcome = receipt.result.get("answer_feedback", {})
        if outcome.get("conversation_id") != conv.id or not outcome.get("binding"):
            continue
        token = outcome["binding"]
        obj = _read_token(db, actor, token, _TARGET_KIND, conv.namespace_id, live=False)
        data = obj.payload
        if _consumed_in(db, actor, conv, obj):
            continue
        if data["conversation_id"] != conv.id or data["target"]["question_hash"] != fingerprint(
            turn.question
        ):
            continue
        if sorted(set(turn.focus.get("project_source_ids", []))) != data["target"]["source_ids"]:
            _fail("相同问题已切换来源，不能消费原答案反馈。请确认目标答案。")
        _validate_target(db, actor, conv, token, receipt)
        if _scope_matches(data["scope"], turn.focus):
            eligible.append(obj)
    if len(eligible) > 1:
        _fail("有多条未消费的相同答案反馈，请明确保留哪一条。")
    projected = deepcopy(projection)
    if eligible:
        projected["preferences"].update(eligible[0].payload["preferences"])
    payload = {
        "contract": FEEDBACK_TARGET_CONTRACT,
        "principal_id": actor,
        "namespace_id": conv.namespace_id,
        "privacy_generation": assert_personal_access(db, actor),
        "conversation_id": conv.id,
        "conversation_binding": deepcopy(conv.preferences[ORDINARY_CHECKER_KEY]),
        "turn_id": turn.id,
        "question_hash": fingerprint(turn.question),
        "focus_revision": turn.focus_revision,
        "entry_source_ids": sorted(set(turn.focus.get("project_source_ids", []))),
        "ordinary_binding": deepcopy(turn.focus[ORDINARY_CHECKER_KEY]),
        "targets": [_token(obj) for obj in eligible],
        "projection": projected,
    }
    identifier = str(uuid5(NAMESPACE_URL, "careerforge:answer-feedback-turn:" + turn.id))
    frozen = register(db, actor, conv.namespace_id, _TURN_KIND, payload, identifier)
    depend(
        db, actor, frozen.id, turn.focus[ORDINARY_CHECKER_KEY]["manifest_id"], "freezes_feedback"
    )
    for obj in eligible:
        depend(db, actor, frozen.id, obj.id, "freezes_feedback")
        db.add(
            Idempotency(
                principal_id=actor,
                operation=_CONSUME,
                key=obj.id,
                request_hash=obj.content_hash,
                result={"turn_id": turn.id, "binding": _token(frozen)},
            )
        )
    db.add(
        Idempotency(
            principal_id=actor,
            operation=_TURN,
            key=turn.id,
            request_hash=frozen.content_hash,
            result=_token(frozen),
        )
    )
    turn.focus = {**turn.focus, FEEDBACK_TARGET_KEY: _token(frozen)}
    return projected


def _read_turn(db, actor, conv, turn):
    identifier = str(uuid5(NAMESPACE_URL, "careerforge:answer-feedback-turn:" + turn.id))
    receipt = _receipt(db, actor, _TURN, turn.id)
    token = turn.focus.get(FEEDBACK_TARGET_KEY)
    obj = _read_token(db, actor, token, _TURN_KIND, conv.namespace_id)
    data = obj.payload
    if (
        obj.id != identifier
        or receipt is None
        or receipt.result != token
        or receipt.request_hash != obj.content_hash
        or data["turn_id"] != turn.id
        or data["conversation_id"] != conv.id
        or data["question_hash"] != fingerprint(turn.question)
        or data["focus_revision"] != turn.focus_revision
        or data["ordinary_binding"] != turn.focus.get(ORDINARY_CHECKER_KEY)
        or data["conversation_binding"] != conv.preferences.get(ORDINARY_CHECKER_KEY)
        or data["entry_source_ids"] != sorted(set(turn.focus.get("project_source_ids", [])))
        or data["projection"] != turn.focus.get("learning_preferences")
        or data["projection"]["preferences"] != turn.focus.get("preferences")
    ):
        _fail()
    for target in data["targets"]:
        consume = _receipt(db, actor, _CONSUME, target["manifest_id"])
        if (
            consume is None
            or consume.request_hash != target["manifest_hash"]
            or consume.result != {"turn_id": turn.id, "binding": token}
        ):
            _fail()
    return obj


def validate_turn_in(db, actor, turn, *, run=None):
    conv = db.get(Conversation, turn.conversation_id, populate_existing=True)
    choice = conversation_choice_in(db, actor, conv)
    receipt = _receipt(db, actor, _TURN, turn.id)
    if choice is None and receipt is None and FEEDBACK_TARGET_KEY not in turn.focus:
        return None
    if choice is None:
        _fail()
    obj = _read_turn(db, actor, conv, turn)
    if run is not None and run.input_manifest.get(FEEDBACK_TARGET_KEY) != _token(obj):
        _fail()
    for token in obj.payload["targets"]:
        _validate_target(db, actor, conv, token)
    if obj.payload["targets"] and turn.manifest_id:
        selection, sources, selected, hashes, _ = _selection(db, actor, turn)
        binding = require_object(
            db, actor, obj.payload["targets"][0]["manifest_id"], _TARGET_KIND
        ).payload
        target = binding["target"]
        if (
            sources != target["source_ids"]
            or selected != target["selected_source_ids"]
            or hashes != target["source_hashes"]
        ):
            _fail("本次检索切换了目标答案的固定来源，停止导师调用。")
        if not _scope_matches(binding["scope"], selection.payload["task_spec"]):
            _fail("本次检索不满足反馈明确指定的范围，停止导师调用。")
    return obj


def seal_run_in(db, actor, turn, run):
    token = turn.focus.get(FEEDBACK_TARGET_KEY)
    if token:
        db.add(
            Idempotency(
                principal_id=actor,
                operation=_RUN,
                key=run.id,
                request_hash=run.input_hash,
                result={"turn_id": turn.id, "binding": token},
            )
        )
        db.flush()


def validate_run_in(db, run):
    # Run IDs are globally unique. Discover server authority independently of
    # mutable principal/workflow/turn links so stripping them cannot opt out.
    receipt = db.scalar(
        select(Idempotency)
        .where(Idempotency.operation == _RUN, Idempotency.key == run.id)
        .execution_options(populate_existing=True)
    )
    if receipt is None:
        turn = db.scalar(select(Turn).where(Turn.run_id == run.id))
        if FEEDBACK_TARGET_KEY in run.input_manifest or (
            turn is not None and FEEDBACK_TARGET_KEY in turn.focus
        ):
            _fail()
        return
    turn = db.get(Turn, receipt.result.get("turn_id"), populate_existing=True)
    if (
        turn is None
        or receipt.principal_id != run.principal_id
        or turn.run_id != run.id
        or run.workflow != "chat"
        or run.input_manifest.get("turn_id") != turn.id
        or run.input_manifest.get(FEEDBACK_TARGET_KEY) != receipt.result.get("binding")
        or fingerprint(run.input_manifest) != run.input_hash
        or receipt.request_hash != run.input_hash
    ):
        _fail()
    validate_turn_in(db, run.principal_id, turn, run=run)


def model_context_in(db, actor, focus, run, selection_id):
    """Only new consumed bindings add context; old provider bytes stay unchanged."""
    if FEEDBACK_TARGET_KEY not in focus:
        return {}
    if not run:
        return {}
    active = db.get(AgentRun, run[0], populate_existing=True)
    if active is None:
        _fail()
    validate_run_in(db, active)
    turn = db.get(Turn, active.input_manifest["turn_id"])
    obj = validate_turn_in(db, actor, turn, run=active)
    if (
        obj is None
        or focus[FEEDBACK_TARGET_KEY] != _token(obj)
        or focus.get("preferences") != obj.payload["projection"]["preferences"]
    ):
        _fail()
    if not obj.payload["targets"]:
        return {}
    if turn.manifest_id != selection_id:
        _fail()
    return {
        FEEDBACK_TARGET_KEY: {
            **_token(obj),
            "targets": obj.payload["targets"],
            "purpose": "one_use_presentation_only",
            "facts_approved": False,
            "grants_permissions": False,
        }
    }
