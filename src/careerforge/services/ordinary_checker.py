"""Owner-adopted ordinary checker contract, frozen per conversation and per turn.

A conversation contract controls future admissions only. Turn authority comes
from a separate immutable object and server receipt, never its mutable parent.
No plan, module, goal, prior question or private history enters checker context.
"""

from __future__ import annotations

import inspect
from copy import deepcopy
from importlib import import_module
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from ..contracts_ordinary_checker_v166 import (
    ORDINARY_CHECKER_CONTRACT,
    ORDINARY_CHECKER_KEY,
    AdoptConversationCheckerInput,
)
from ..db import aware, clock
from ..errors import DomainError
from ..models import AgentRun, Conversation, FundingAgreement, Idempotency, ObjectRow, Turn
from ..util import fingerprint
from .bounded_evidence import fact_support
from .checker_evidence import (
    CHECKER_EVIDENCE_RULES,
    checker_evidence_identity,
    project_verified_evidence,
)
from .dependencies import check_closure, depend, register, require_object
from .privacy import assert_personal_access
from .security import namespace_access

_CONVERSATION_OPERATION = "conversation.checker_contract"
_TURN_OPERATION = "conversation.checker_turn"
_ADOPT_OPERATION = "conversation.adopt_checker.v1"
_RUN_OPERATION = "conversation.ordinary_run"
ORDINARY_RUN_CONTRACT = "ordinary-run-identity-1.6.6-v1"
ORDINARY_CHECKER_RULES = CHECKER_EVIDENCE_RULES.replace(
    "session_scope是服务端已验证的当前模块与这一条事实的绑定，不是完整计划，不证明其他模块事实。",
    "question_scope是服务端已验证的当前普通问题与这一条事实的绑定，"
    "不代表学习计划或模块，不包含其他会话历史、个人目标或完整项目范围。",
)


def ordinary_checker_identity() -> dict:
    modules = (
        "ordinary_checker",
        "conversations",
        "answers",
        "answer_relevance",
        "answer_completion",
        "retrieval",
        "bounded_evidence",
        "question_coverage",
        "runtime",
        "dependencies",
    )
    return {
        "contract": ORDINARY_CHECKER_CONTRACT,
        "implementation_hash": fingerprint(
            {
                "shared_evidence": checker_evidence_identity(),
                "implementation": {
                    name: inspect.getsource(import_module(f"careerforge.services.{name}"))
                    for name in modules
                },
                "http_route": inspect.getsource(import_module("careerforge.api")),
                "model_gateway": inspect.getsource(import_module("careerforge.adapters.model")),
                "dto": inspect.getsource(
                    import_module("careerforge.contracts_ordinary_checker_v166")
                ),
            }
        ),
    }


def _object_id(kind, identifier):
    return str(uuid5(NAMESPACE_URL, f"careerforge:ordinary-checker:{kind}:{identifier}"))


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


def _fail(message="普通对话的固定审核合同已改变。"):
    raise DomainError("ORDINARY_CHECKER_BINDING", message, 409)


def _read_binding(db, actor, kind, identifier, marker, marker_present):
    operation = _CONVERSATION_OPERATION if kind == "conversation" else _TURN_OPERATION
    obj_id = _object_id(kind, identifier)
    raw = db.get(ObjectRow, obj_id, populate_existing=True)
    receipt = _receipt(db, actor, operation, identifier)
    if raw is None and receipt is None and not marker_present:
        return None
    if raw is None or receipt is None or not marker_present:
        _fail()
    obj = require_object(db, actor, obj_id, "ordinary_checker_" + kind)
    token = {"manifest_id": obj.id, "manifest_hash": obj.content_hash, **obj.payload["identity"]}
    if (
        marker != token
        or receipt.result != token
        or receipt.request_hash != obj.content_hash
        or obj.payload.get("kind") != kind
        or obj.payload.get("principal_id") != actor
        or obj.payload.get(kind + "_id") != identifier
        or obj.payload["identity"].get("contract") != ORDINARY_CHECKER_CONTRACT
    ):
        _fail()
    check_closure(db, actor, [obj.id])
    return deepcopy(obj.payload)


def _save_binding(db, actor, namespace_id, kind, identifier, payload):
    obj = register(
        db,
        actor,
        namespace_id,
        "ordinary_checker_" + kind,
        payload,
        identifier=_object_id(kind, identifier),
    )
    token = {"manifest_id": obj.id, "manifest_hash": obj.content_hash, **payload["identity"]}
    db.add(
        Idempotency(
            principal_id=actor,
            operation=_CONVERSATION_OPERATION if kind == "conversation" else _TURN_OPERATION,
            key=identifier,
            request_hash=obj.content_hash,
            result=token,
        )
    )
    db.flush()
    return token


def conversation_contract_in(db, actor, conv):
    if conv.principal_id != actor:
        raise DomainError("NOT_FOUND", "会话不可访问。", 404)
    generation = assert_personal_access(db, actor)
    namespace_access(db, actor, conv.namespace_id)
    snapshot = _read_binding(
        db,
        actor,
        "conversation",
        conv.id,
        conv.preferences.get(ORDINARY_CHECKER_KEY),
        ORDINARY_CHECKER_KEY in conv.preferences,
    )
    if snapshot is not None and (
        snapshot["privacy_generation"] != generation
        or snapshot["namespace_id"] != conv.namespace_id
    ):
        _fail()
    return snapshot


def create_conversation_contract_in(db, actor, conv, *, admission, request=None):
    generation = assert_personal_access(db, actor)
    namespace_access(db, actor, conv.namespace_id, True)
    if conv.principal_id != actor or admission not in {"new_conversation", "owner_adoption"}:
        _fail()
    payload = {
        "kind": "conversation",
        "principal_id": actor,
        "namespace_id": conv.namespace_id,
        "conversation_id": conv.id,
        "privacy_generation": generation,
        "identity": ordinary_checker_identity(),
        "admission": admission,
        "admitted_revision": conv.revision,
        "request": request,
    }
    token = _save_binding(db, actor, conv.namespace_id, "conversation", conv.id, payload)
    conv.preferences = {**conv.preferences, ORDINARY_CHECKER_KEY: token}
    return token


def adopt_conversation_contract_in(db, actor, conv, value: AdoptConversationCheckerInput):
    current = conversation_contract_in(db, actor, conv)
    request = {"conversation_id": conv.id, **value.model_dump(mode="json")}
    request_hash = fingerprint(request)
    receipt = _receipt(db, actor, _ADOPT_OPERATION, value.idempotency_key)
    if receipt is not None:
        if receipt.request_hash != request_hash:
            raise DomainError("IDEMPOTENCY_CONFLICT", "相同升级键对应不同的完整请求。", 409)
        if current is None or receipt.result.get(ORDINARY_CHECKER_KEY) != conv.preferences.get(
            ORDINARY_CHECKER_KEY
        ):
            _fail()
        return deepcopy(receipt.result)
    if conv.revision != value.expected_revision:
        raise DomainError("REVISION_CONFLICT", "会话已变化，不能采纳审核版本。", 409)
    if db.scalar(
        select(Turn.id).where(
            Turn.conversation_id == conv.id, Turn.status.in_(["queued", "running"])
        )
    ):
        raise DomainError("CONVERSATION_BUSY", "当前回答尚未结束，不能升级审核合同。", 409)
    if current is not None:
        raise DomainError("CHECKER_ALREADY_ADOPTED", "会话已固定采用此审核合同。", 409)
    token = create_conversation_contract_in(
        db, actor, conv, admission="owner_adoption", request=request
    )
    conv.revision += 1
    result = {"conversation_id": conv.id, "revision": conv.revision, ORDINARY_CHECKER_KEY: token}
    db.add(
        Idempotency(
            principal_id=actor,
            operation=_ADOPT_OPERATION,
            key=value.idempotency_key,
            request_hash=request_hash,
            result=result,
        )
    )
    db.flush()
    return result


def freeze_ordinary_turn_in(db, actor, conv, turn, value):
    snapshot = conversation_contract_in(db, actor, conv)
    if snapshot is None:
        return None
    if snapshot["identity"] != ordinary_checker_identity():
        _fail("会话审核实现已变化，不能静默用新实现重新解释旧采纳合同。")
    payload = {
        "kind": "turn",
        "principal_id": actor,
        "namespace_id": conv.namespace_id,
        "conversation_id": conv.id,
        "turn_id": turn.id,
        "privacy_generation": snapshot["privacy_generation"],
        "identity": snapshot["identity"],
        "run_authority_contract": ORDINARY_RUN_CONTRACT,
        "conversation_manifest_id": conv.preferences[ORDINARY_CHECKER_KEY]["manifest_id"],
        "conversation_manifest_hash": conv.preferences[ORDINARY_CHECKER_KEY]["manifest_hash"],
        "focus_revision": turn.focus_revision,
        "question_hash": fingerprint(turn.question),
        "request_hash": fingerprint(value.model_dump(mode="json")),
        "answer_contracts": {
            **{
                key: turn.focus.get(key)
                for key in ("answer_contract", "answer_completion_contract")
            },
            **(
                {"answer_aspect_contract": turn.focus["answer_aspect_contract"]}
                if "answer_aspect_contract" in turn.focus
                else {}
            ),
        },
        "funding_id": value.funding_id,
        "payer_id": actor if value.funding_id else None,
    }
    token = _save_binding(db, actor, conv.namespace_id, "turn", turn.id, payload)
    depend(db, actor, token["manifest_id"], payload["conversation_manifest_id"], "admitted_under")
    turn.focus = {**turn.focus, ORDINARY_CHECKER_KEY: token}
    return token


def _run_authority_in(db, run_id):
    return db.scalar(
        select(Idempotency)
        .where(Idempotency.operation == _RUN_OPERATION, Idempotency.key == run_id)
        .execution_options(populate_existing=True)
    )


def seal_ordinary_run_in(db, actor, turn, run):
    """Seal a new marked admission without changing its existing token/run hash."""
    token = turn.focus.get(ORDINARY_CHECKER_KEY)
    if token is None:
        return
    if (
        turn.run_id != run.id
        or run.principal_id != actor
        or run.input_manifest.get("turn_id") != turn.id
        or run.input_manifest.get(ORDINARY_CHECKER_KEY) != token
        or _run_authority_in(db, run.id) is not None
    ):
        _fail()
    db.add(
        Idempotency(
            principal_id=actor,
            operation=_RUN_OPERATION,
            key=run.id,
            request_hash=run.input_hash,
            result={
                "contract": ORDINARY_RUN_CONTRACT,
                "run_id": run.id,
                "turn_id": turn.id,
                "binding": token,
            },
        )
    )
    db.flush()


def validate_ordinary_turn_in(db, actor, turn, *, run=None, history=False):
    snapshot = _read_binding(
        db,
        actor,
        "turn",
        turn.id,
        turn.focus.get(ORDINARY_CHECKER_KEY),
        ORDINARY_CHECKER_KEY in turn.focus,
    )
    if snapshot is None:
        if run is not None and ORDINARY_CHECKER_KEY in run.input_manifest:
            _fail()
        return None
    conv = db.get(Conversation, turn.conversation_id, populate_existing=True)
    assert_personal_access(db, actor, expected_generation=snapshot["privacy_generation"])
    if conv is None or conv.principal_id != actor:
        raise DomainError("NOT_FOUND", "会话不可访问。", 404)
    namespace_access(db, actor, conv.namespace_id)
    if (
        snapshot["conversation_id"] != conv.id
        or snapshot["namespace_id"] != conv.namespace_id
        or snapshot["focus_revision"] != turn.focus_revision
        or snapshot["question_hash"] != fingerprint(turn.question)
        or snapshot.get("answer_contracts")
        != {
            **{
                key: turn.focus.get(key)
                for key in ("answer_contract", "answer_completion_contract")
            },
            **(
                {"answer_aspect_contract": turn.focus["answer_aspect_contract"]}
                if "answer_aspect_contract" in turn.focus
                else {}
            ),
        }
        or bool(snapshot["funding_id"]) != bool(turn.run_id)
        or "session_tutor_binding" in turn.focus
    ):
        _fail()
    parent = require_object(
        db, actor, snapshot["conversation_manifest_id"], "ordinary_checker_conversation"
    )
    if parent.content_hash != snapshot["conversation_manifest_hash"]:
        _fail()
    # Reading completed blocks validates their original snapshot, not today's
    # implementation. Pending paid bodies never switch to a new implementation.
    if not history and snapshot["identity"] != ordinary_checker_identity():
        _fail("审核实现版本已变化；旧请求不得以新版重新执行。")
    if turn.run_id:
        if run is None:
            run = db.get(AgentRun, turn.run_id, populate_existing=True)
        if (
            run is None
            or run.id != turn.run_id
            or run.principal_id != actor
            or run.namespace_id != conv.namespace_id
            or run.workflow != "chat"
            or run.input_manifest.get("turn_id") != turn.id
            or run.input_manifest.get(ORDINARY_CHECKER_KEY) != turn.focus[ORDINARY_CHECKER_KEY]
            or run.input_manifest.get("funding_id") != snapshot["funding_id"]
            or run.funding_id != snapshot["funding_id"]
            or fingerprint(run.input_manifest) != run.input_hash
        ):
            _fail()
        authority = _run_authority_in(db, run.id)
        if (
            "run_authority_contract" in snapshot
            or authority is not None
            or snapshot["identity"] == ordinary_checker_identity()
        ) and (
            snapshot.get("run_authority_contract") != ORDINARY_RUN_CONTRACT
            or authority is None
            or authority.principal_id != actor
            or authority.request_hash != run.input_hash
            or authority.result
            != {
                "contract": ORDINARY_RUN_CONTRACT,
                "run_id": run.id,
                "turn_id": turn.id,
                "binding": turn.focus[ORDINARY_CHECKER_KEY],
            }
        ):
            _fail()
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
                or funding.payer_id != snapshot["payer_id"]
                or funding.payer_id != actor
                or funding.namespace_id != snapshot["namespace_id"]
            ):
                raise DomainError(
                    "FUNDING_UNAVAILABLE", "普通审核的费用授权或付款身份已改变。", 403
                )
    elif run is not None:
        _fail()
    return snapshot


def validate_ordinary_run_in(db, run):
    # The immutable admission anchor survives workflow reclassification and
    # simultaneous removal of the mutable forward and reverse Turn links.
    authority = _run_authority_in(db, run.id)
    if authority is not None:
        turn = db.get(Turn, authority.result.get("turn_id"), populate_existing=True)
        if turn is None:
            _fail()
        validate_ordinary_turn_in(db, authority.principal_id, turn, run=run)
        return
    if run.workflow != "chat":
        if ORDINARY_CHECKER_KEY in run.input_manifest:
            _fail()
        return
    # A stripped turn_id must not turn a bound chat into a generic model call.
    turn = db.scalar(
        select(Turn).where(Turn.run_id == run.id).execution_options(populate_existing=True)
    )
    if turn is None:
        if ORDINARY_CHECKER_KEY in run.input_manifest:
            _fail()
        return
    validate_ordinary_turn_in(db, run.principal_id, turn, run=run)


def prepare_ordinary_checker_evidence(
    service, actor, unit, run, *, proposed_text="", question=None
):
    with service.db.read() as db:
        active = db.get(AgentRun, run[0], populate_existing=True)
        if active is None:
            raise DomainError("NOT_FOUND", "独立审核任务不存在。", 404)
        turn = db.scalar(select(Turn).where(Turn.run_id == active.id))
        if turn is None:
            if ORDINARY_CHECKER_KEY in active.input_manifest:
                _fail()
            return None
        snapshot = validate_ordinary_turn_in(db, actor, turn, run=active)
        if snapshot is None:
            return None
        if len(unit["facts"]) != 1:
            raise DomainError("CHECKER_EVIDENCE_SCOPE", "普通审核只能绑定唯一事实。", 409)
        selection_id = turn.manifest_id
        if not selection_id:
            raise DomainError("CHECKER_EVIDENCE_SCOPE", "普通审核缺少本轮检索选择。", 409)
        from .answer_relevance import scoped_continuation_question

        if question not in {
            turn.question,
            scoped_continuation_question(turn.question, turn.focus, [unit]),
        }:
            raise DomainError("CHECKER_EVIDENCE_SCOPE", "普通审核问题与本轮固定问题不一致。", 409)
        selection = require_object(db, actor, selection_id, "selection_manifest")
        task = selection.payload.get("task_spec", {})
        if (
            task.get("turn_id") != turn.id
            or task.get("user_question") != turn.question
            or selection.payload.get("answer_checker_contract") != turn.focus[ORDINARY_CHECKER_KEY]
        ):
            _fail("普通审核选择范围已改变。")
        token = turn.focus[ORDINARY_CHECKER_KEY]
        scope = {
            "scope": "current_question_single_reviewed_fact",
            "turn_id": turn.id,
            "question_hash": snapshot["question_hash"],
            "binding_manifest_id": token["manifest_id"],
            "binding_manifest_hash": token["manifest_hash"],
            "selection_manifest_id": selection_id,
            **{key: unit[key] for key in ("publication_id", "unit_version_id", "source_id")},
            "focus_fact_ids": [unit["facts"][0]["fact_atom_id"]],
        }
    fresh = service._fresh_unit(
        actor, unit, unit["facts"][0]["fact_atom_id"], selection_manifest_id=selection_id
    )
    if unit["facts"] != fresh["facts"] or unit["citations"] != fresh["citations"]:
        raise DomainError("CHECKER_EVIDENCE_CHANGED", "普通审核事实或完整引用已变化。", 409)
    with service.db.read() as db:
        bindings, citations = fact_support(service.knowledge, db, actor, fresh)
        deps = sorted(
            set(service.knowledge.pack_dependencies(fresh) + [selection_id, token["manifest_id"]])
        )
        check_closure(db, actor, deps)
        projection = project_verified_evidence(
            fresh,
            bindings,
            citations,
            scope,
            snapshot["identity"],
            proposed_text=proposed_text,
            scope_kind="question",
        )
    return projection, deps


def bind_ordinary_selection_in(db, actor, turn, manifest_id):
    snapshot = validate_ordinary_turn_in(db, actor, turn)
    if snapshot is None:
        return
    selection = require_object(db, actor, manifest_id, "selection_manifest")
    task = selection.payload.get("task_spec", {})
    if (
        task.get("turn_id") != turn.id
        or task.get("user_question") != turn.question
        or selection.payload.get("answer_checker_contract") != turn.focus[ORDINARY_CHECKER_KEY]
        or turn.manifest_id not in {None, manifest_id}
    ):
        _fail("普通审核检索范围与本轮原问题不一致。")
    check_closure(db, actor, [selection.id])
    turn.manifest_id = manifest_id


def has_ordinary_turn_authority_in(db, actor, turn):
    """Recognize a server admission without trusting a user-controlled marker."""
    return _receipt(db, actor, _TURN_OPERATION, turn.id) is not None
