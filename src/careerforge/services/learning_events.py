"""Private append-only preference decisions and separately reported client ACKs.

The event log lives in the existing dependency-object store. Its namespace and
content hashes inherit privacy erasure and fail-closed integrity checks. Frozen
turn projections, rather than mutable current state, determine resumed answers.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from sqlalchemy import select

from ..contracts_learning_events_v166 import (
    NavigationAckInput,
    PreferenceEventInput,
    PreferenceScope,
)
from ..db import locked
from ..errors import DomainError
from ..models import AnswerBlock, Conversation, Idempotency, ObjectRow, Principal, Turn
from ..util import digest, fingerprint
from .dependencies import check_closure, register, require_object
from .preference_control_v166 import (
    control_audit,
    parse_preference_controls,
    style_reference_matches,
)
from .preference_scope_v166 import (
    dialogue_preference_scope,
    protected_preference_topics,
)
from .security import private_namespace
from .taxonomy import require_topic, visible_topics

PREFERENCE_CONTRACT = "learning-preferences-1.6.6"
ACK_CONTRACT = "source-navigation-ack-1.6.6"
_STYLES = {
    "concept_first",
    "code_first",
    "comparison_first",
    "architecture_first",
    "evidence_first",
}
_STYLE_INSTRUCTIONS = {
    "concept_first": "先说明通用概念，再在已审资料范围内解释代码。",
    "code_first": "先展示当前已审证据中的关键代码，再解释机制与条件。",
    "comparison_first": "先对照已审证据的共同点与差异，未知部分保持未知。",
    "architecture_first": "先说明已审证据能支持的整体结构，再讲具体代码；没有架构证据时明确缺口。",
    "evidence_first": "先展示已审证据与版本，再给出它能支持的结论。",
}
_PREFERENCE_QUOTES = re.compile(
    r"```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)|(?P<ticks>`+)[\s\S]*?(?:(?P=ticks)|\Z)|"
    r"“[^”]*(?:”|\Z)|‘[^’]*(?:’|\Z)|「[^」]*(?:」|\Z)|"
    r"『[^』]*(?:』|\Z)|《[^》]*(?:》|\Z)|"
    r'"(?:\\.|[^"\\])*(?:"|\Z)|(?<![A-Za-z0-9_])\'(?:\\.|[^\\])*?(?:\'(?![A-Za-z0-9_])|\Z)|^\s*>[^\n]*',
    re.M,
)
_PREFERENCE_NEGATION = re.compile(
    r"不要|不用|不想|不愿|不必|不许|不能|不应|不该|不再|不喜欢|不希望|不允许|"
    r"无需|无须|禁止|请勿|别|不是|避免|\bnot\b|\bnever\b|\bdon['’]t\b|"
    r"\bmustn['’]t\b|\bshouldn['’]t\b|\bno\b|\bwithout\b",
    re.I,
)
_PREFERENCE_CONTROL = re.compile(
    r"撤销|取消|忘掉|记住|保存|设置|更正|修改|\b(?:revoke|forget|remove|remember|save|set|settings?|change)\b",
    re.I,
)


def _preference_text(question):
    """Quoted data cannot grant preference or persistence authorization."""
    return _PREFERENCE_QUOTES.sub(" __quoted_preference_data__ ", question)


def _preference_clauses(text):
    return [
        clause.strip()
        for clause in re.split(
            r"[，,。；;!?！？\n]|\.(?=\s|$)|而是|但是|\b(?:instead|but|however)\b", text, flags=re.I
        )
        if clause.strip()
    ]


def _affirmative_preference_clauses(text):
    return [
        clause for clause in _preference_clauses(text) if not _PREFERENCE_NEGATION.search(clause)
    ]


def _negated_preference_control(clause):
    negative = _PREFERENCE_NEGATION.search(clause)
    return bool(negative and _PREFERENCE_CONTROL.search(clause, negative.end()))


def _preference_lifetime(current, *, persistence_authorized=False, lifetime_hints=()):
    """Resolve only this control's lifetime, never time words in other values."""
    save_denied = any(
        _negated_preference_control(clause) and re.search(r"记住|保存|remember|save", clause, re.I)
        for clause in _preference_clauses(current)
    )
    if "conversation" in lifetime_hints:
        return "conversation"
    if "turn" in lifetime_hints:
        return "turn"
    if persistence_authorized and not save_denied:
        return "persistent"
    return "turn"


def _preference_conflict(clarifications, message):
    if clarifications is not None:
        clarifications.append(message)
    return []


_LIFETIME_CONFLICT = "这条请求包含相互冲突的偏好值或作用期限。请明确是本次例外、会话设置还是长期偏好；当前设置未改变。"
_PROTECTION_CONFLICT = (
    "这项修改会改变你明确要求保留的偏好期限。请确认要保留原设置还是修改它；当前设置未改变。"
)


def _profile(db, actor, *, lock=False):
    from .learning_ext import _profile as profile

    return profile(db, actor, lock=lock)


def _events(db, actor):
    ns = private_namespace(db, actor)
    rows = db.scalars(
        select(ObjectRow).where(
            ObjectRow.namespace_id == ns.id, ObjectRow.kind == "learning_preference_event"
        )
    ).all()
    values = []
    for row in rows:
        require_object(db, actor, row.id, "learning_preference_event")
        if row.payload.get("contract_version") != PREFERENCE_CONTRACT:
            raise DomainError("PREFERENCE_CONTRACT", "未知学习偏好事件版本。", 422)
        values.append({"event_id": row.id, **row.payload})
    return sorted(values, key=lambda value: value["revision"])


def _slot(event):
    return (
        event["lifetime"],
        event.get("conversation_id") if event["lifetime"] != "persistent" else None,
        event["scope"]["kind"],
        event["scope"].get("identifier"),
        event["key"],
    )


def _active(events):
    state = {}
    for event in events:
        target = event.get("target_event_id")
        if target:
            for key, old in list(state.items()):
                if old["event_id"] == target:
                    del state[key]
        if event["operation"] != "revoke":
            state[_slot(event)] = event
    return list(state.values())


def append_preference_in(
    db, actor, value, *, turn_id=None, source="explicit_form", next_turn=False, feedback_id=None
):
    profile = _profile(db, actor, lock=True)
    request_hash = fingerprint(value.model_dump(mode="json"))
    old = db.scalar(
        select(Idempotency).where(
            Idempotency.principal_id == actor,
            Idempotency.operation == "learning.preference",
            Idempotency.key == value.idempotency_key,
        )
    )
    if old:
        if old.request_hash != request_hash:
            raise DomainError("IDEMPOTENCY_CONFLICT", "偏好事件键对应不同内容。")
        # Privacy erasure/revocation is checked before replay, not just on first write.
        require_object(db, actor, old.result["event_id"], "learning_preference_event")
        return {**old.result, "replayed": True}
    if profile.revision != value.expected_revision:
        raise DomainError("REVISION_CONFLICT", "学习偏好已变化。")
    if value.conversation_id:
        conversation = db.get(Conversation, value.conversation_id)
        if (
            not conversation
            or conversation.principal_id != actor
            or conversation.namespace_id != profile.namespace_id
        ):
            raise DomainError("NOT_FOUND", "偏好事件会话不存在。", 404)
    if value.scope.kind == "topic":
        require_topic(db, actor, value.scope.identifier)
    elif value.scope.kind == "source":
        require_object(db, actor, value.scope.identifier, "source_set")
        check_closure(db, actor, [value.scope.identifier])
    if value.target_event_id:
        target = next(
            (
                item
                for item in _active(_events(db, actor))
                if item["event_id"] == value.target_event_id
            ),
            None,
        )
        if not target or target["key"] != value.key:
            raise DomainError("PREFERENCE_TARGET", "只能更正或撤销本人当前生效的同项偏好。", 422)
        if value.operation == "correct" and target["lifetime"] != value.lifetime:
            raise DomainError("PREFERENCE_LIFETIME", "作用期限变化需要独立的新偏好事件。", 422)
    profile.revision += 1
    payload = {
        "contract_version": PREFERENCE_CONTRACT,
        "revision": profile.revision,
        "operation": value.operation,
        "key": value.key,
        "value": value.value,
        "scope": value.scope.model_dump(mode="json"),
        "lifetime": value.lifetime,
        "conversation_id": value.conversation_id,
        "target_event_id": value.target_event_id,
        "durable_consent": value.durable_consent,
        "source": source,
        "source_turn_id": turn_id,
        "source_feedback_id": feedback_id,
        "applies_to_turn_id": turn_id if value.lifetime == "turn" and not next_turn else None,
        "after_turn_id": turn_id if next_turn and value.lifetime == "turn" else None,
        "competence_verified": False,
    }
    event = register(db, actor, profile.namespace_id, "learning_preference_event", payload)
    result = {"event_id": event.id, **payload}
    db.add(
        Idempotency(
            principal_id=actor,
            operation="learning.preference",
            key=value.idempotency_key,
            request_hash=request_hash,
            result=result,
        )
    )
    return result


def _project_preferences(base, focus, events, *, conversation_id=None, turn_id=None, consumed=()):
    """Pure projection shared by reads and pre-write correction validation."""
    preferences = {
        "requested_style": base.get("preferred_style", "concept_first"),
        "practice": base.get("exercises_enabled", True),
        "check_understanding": base.get("checks_enabled", True),
        **focus.get("preferences", {}),
    }
    topics = set(focus.get("topic_keys", [])) | set(focus.get("topic_hints", []))
    sources = set(focus.get("project_source_ids", []))
    relevant = []
    for event in _active(events):
        scope = event["scope"]
        if event["lifetime"] != "persistent" and event.get("conversation_id") != conversation_id:
            continue
        if event.get("after_turn_id") == turn_id and turn_id is not None:
            continue
        if event["lifetime"] == "turn" and (
            event["event_id"] in consumed
            or (event.get("applies_to_turn_id") and event["applies_to_turn_id"] != turn_id)
        ):
            continue
        if scope["kind"] == "topic" and scope["identifier"] not in topics:
            continue
        if scope["kind"] == "source" and scope["identifier"] not in sources:
            continue
        if scope["kind"] == "project_facts" and not sources:
            continue
        relevant.append(event)
    relevant.sort(
        key=lambda e: (
            {"persistent": 0, "conversation": 1, "turn": 2}[e["lifetime"]],
            e["scope"]["kind"] != "global",
            e["revision"],
        )
    )
    for event in relevant:
        preferences[event["key"]] = event["value"]
    return preferences, [event["event_id"] for event in relevant]


def preference_projection_in(db, actor, focus, *, conversation_id=None, turn_id=None):
    profile = _profile(db, actor)
    consumed = set()
    if conversation_id:
        for old in db.scalars(select(Turn).where(Turn.conversation_id == conversation_id)):
            if old.id != turn_id:
                consumed.update(
                    old.focus.get("learning_preferences", {}).get("applied_event_ids", [])
                )
    preferences, applied = _project_preferences(
        profile.preferences,
        focus,
        _events(db, actor),
        conversation_id=conversation_id,
        turn_id=turn_id,
        consumed=consumed,
    )
    return {
        "contract_version": PREFERENCE_CONTRACT,
        "revision": profile.revision,
        "preferences": preferences,
        "applied_event_ids": applied,
        "competence_verified": False,
    }


def _dialogue_scope(focus, *, scoped=False):
    topics = list(dict.fromkeys([*focus.get("topic_keys", []), *focus.get("topic_hints", [])]))
    if scoped and len(topics) == 1:
        return PreferenceScope(kind="topic", identifier=topics[0])
    return PreferenceScope()


def _target_style_values(reference):
    return {value for value, _, _ in style_reference_matches(reference)}


def _selector_term(term):
    left = r"(?<![A-Za-z0-9_])" if term[0].isascii() and term[0].isalnum() else ""
    right = r"(?![A-Za-z0-9_])" if term[-1].isascii() and term[-1].isalnum() else ""
    return left + re.escape(term) + right


def _preference_targets(db, actor, active, keys, reference, focus, *, protected=()):
    """Resolve a fully consumed selector, never a newest-record guess.

    A named topic outside the old conversational focus is valid. An unknown
    name must not degrade to an unqualified selector. Quoted names can select
    existing records only after an independently authorized control frame.
    """
    named_ids = {event["event_id"] for event in active if event["event_id"] in reference}
    styles = _target_style_values(reference)
    scopes = set()
    residue = reference
    scope_terms = {
        ("global", None): [
            "全局",
            "所有技术",
            "所有主题",
            "所有项目",
            "global",
            "globally",
            "all topics",
        ],
        ("project_facts", None): ["项目事实", "project facts"],
    }
    for topic in visible_topics(db, actor):
        scope_terms[("topic", topic.key)] = [
            name
            for name in [topic.key, topic.title, *topic.aliases]
            if isinstance(name, str) and len(name) >= 2
        ]
    for event in active:
        if event["scope"]["kind"] == "source":
            identifier = event["scope"]["identifier"]
            scope_terms[("source", identifier)] = [identifier]
    for scope, names in scope_terms.items():
        for name in sorted(names, key=len, reverse=True):
            if re.search(_selector_term(name), reference, re.I):
                scopes.add(scope)
                residue = re.sub(_selector_term(name), " ", residue, flags=re.I)
    if re.search(r"当前项目|这个项目|本项目|this project|current project", reference, re.I):
        sources = list(dict.fromkeys(focus.get("project_source_ids", [])))
        if len(sources) != 1:
            return None
        scopes.add(("source", sources[0]))
        residue = re.sub(
            r"当前项目|这个项目|本项目|this project|current project", " ", residue, flags=re.I
        )
    for identifier in named_ids:
        residue = residue.replace(identifier, " ")
    for _value, start, end in sorted(
        style_reference_matches(residue), key=lambda match: match[1], reverse=True
    ):
        residue = residue[:start] + " " * (end - start) + residue[end:]
    residue = re.sub(
        r"禁测|测验|出题|quiz|check|check_understanding|practice|"
        r"撤销|取消|忘掉|原来的|原先的|之前的|现有的|以前的|长期|永久|"
        r"这个|当前|之前|原来|原先|现有|以前|本人|我的|我|偏好|设置|事件|对|的|针对|用于|"
        r"(?:\b(?:revoke|forget|remove|persistent|permanent|my|the|this|old|original|"
        r"for|on|about|topic|source|setting|settings|preference|preferences|please)\b)",
        " ",
        residue,
        flags=re.I,
    )
    # Strip delimiters, not quoted contents. Unknown quoted labels remain
    # unresolved just like unknown unquoted topic/style names.
    if re.search(r"[\w\u4e00-\u9fff]", residue):
        return None
    if len(styles) > 1 or len(scopes) > 1 or len(named_ids) > 1:
        return None
    current_topics = set(focus.get("topic_keys", [])) | set(focus.get("topic_hints", []))
    current_sources = set(focus.get("project_source_ids", []))
    selected = []
    for key in keys:
        candidates = [event for event in active if event["key"] == key]
        if named_ids:
            candidates = [event for event in candidates if event["event_id"] in named_ids]
        if key == "requested_style" and styles:
            candidates = [event for event in candidates if event["value"] in styles]
        if scopes:
            candidates = [
                event
                for event in candidates
                if (event["scope"]["kind"], event["scope"].get("identifier")) in scopes
            ]
        elif not named_ids and not styles:
            candidates = [
                event
                for event in candidates
                if (
                    event["scope"]["kind"] == "global"
                    or event["scope"]["kind"] == "topic"
                    and event["scope"]["identifier"] in current_topics
                    or event["scope"]["kind"] == "source"
                    and event["scope"]["identifier"] in current_sources
                    or event["scope"]["kind"] == "project_facts"
                    and bool(current_sources)
                )
            ]
        if re.search(r"长期|永久|persistent|permanent", reference, re.I):
            candidates = [event for event in candidates if event["lifetime"] == "persistent"]
        candidates = [
            event
            for event in candidates
            if not (event["scope"]["kind"] == "topic" and event["scope"]["identifier"] in protected)
        ]
        if len(candidates) > 1:
            return None
        if candidates:
            selected.append((key, candidates[0]))
    return selected or None


def _correction_conflict(
    db,
    actor,
    events,
    targets,
    scope,
    protected,
    focus,
    conversation_id,
    *,
    turn_id,
    next_turn,
    consumed,
):
    """Validate the exact proposed ledger and effective projections before writes.

    Narrowing replaces the target event. It never silently leaves a global
    setting active or invents compensating overrides for a protected topic.
    Check lasting state separately so a temporary exception cannot conceal a
    lasting change. The normal projector is reused, not reimplemented.
    """
    active = _active(events)
    simulated = list(events)
    proposed_ids = set()
    target_ids = {target["event_id"] for _, target in targets}
    for index, (_key, target) in enumerate(targets, start=1):
        proposed = {
            **target,
            "event_id": f"proposed-correction-{index}",
            "revision": max(event["revision"] for event in events) + index,
            "operation": "correct",
            "target_event_id": target["event_id"],
            "scope": scope.model_dump(mode="json"),
            "applies_to_turn_id": turn_id
            if target["lifetime"] == "turn" and not next_turn
            else None,
            "after_turn_id": turn_id if target["lifetime"] == "turn" and next_turn else None,
        }
        if any(
            event["event_id"] not in target_ids and _slot(event) == _slot(proposed)
            for event in active
        ):
            return "目标范围已有另一项偏好；请确认要替换哪一项。当前设置未改变。"
        simulated.append(proposed)
        proposed_ids.add(proposed["event_id"])
    after_active = _active(simulated)
    if target_ids & {event["event_id"] for event in after_active}:
        return "无法验证原偏好已限定到新范围，当前设置未改变。"
    base = _profile(db, actor).preferences
    sources = {
        event["scope"]["identifier"] for event in active if event["scope"]["kind"] == "source"
    }
    sources.update(focus.get("project_source_ids", []))
    topics = [topic.key for topic in visible_topics(db, actor)]
    contexts = [(None, None)] + [(topic, None) for topic in topics]
    contexts += [(None, source) for source in sources]
    contexts += [(topic, source) for topic in topics for source in sources]
    for topic, source in contexts:
        projection_focus = {
            "topic_keys": [topic] if topic else [],
            "project_source_ids": [source] if source else [],
        }
        for horizon, projected_turn in (
            (None, None),
            (conversation_id, None),
            (conversation_id, turn_id),
            (conversation_id, "proposed-next-turn"),
        ):
            before, _ = _project_preferences(
                base,
                projection_focus,
                events,
                conversation_id=horizon,
                turn_id=projected_turn,
                consumed=consumed,
            )
            after, applied = _project_preferences(
                base,
                projection_focus,
                simulated,
                conversation_id=horizon,
                turn_id=projected_turn,
                consumed=consumed,
            )
            if topic in protected and before != after:
                return (
                    "存在范围冲突：把原偏好限定为只对新主题生效，会改变你要求保持不变的主题。"
                    "请确认是缩小原偏好的范围，还是保留原范围；当前设置未改变。"
                )
            scope_applies = (
                scope.kind == "global"
                or scope.kind == "topic"
                and topic == scope.identifier
                or scope.kind == "source"
                and source == scope.identifier
                or scope.kind == "project_facts"
                and source is not None
            )
            if not scope_applies and proposed_ids & set(applied):
                return "无法验证新偏好仅在指定范围生效，当前设置未改变。"
    return None


def preference_feedback_only(question):
    """A settings-only message is acknowledged without an unrelated tutor call."""
    plain = _preference_text(question)
    return bool(
        re.search(
            r"^\s*(?:撤销|取消|忘掉|下一次|下次|下一轮|后面几轮|接下来几轮|之后学|之后继续|"
            r"我说的只针对|我刚才说的不是|只在.*这样|我更习惯|以后.*都|"
            r"revoke\b|forget\b|next time\b|from now on\b)",
            plain,
            re.I,
        )
    )


def capture_dialogue_preferences_in(
    db,
    actor,
    conversation,
    turn,
    *,
    content=None,
    feedback_id=None,
    clarifications=None,
    prepare_only=False,
    target_answer_scope=False,
):
    """Only bounded explicit requests; repetition alone never persists a preference."""
    if target_answer_scope and (not prepare_only or feedback_id is None):
        raise DomainError(
            "FEEDBACK_TARGET_BINDING", "目标答案范围只允许准备新反馈，不能写入通用偏好。", 409
        )
    question = turn.question if content is None else content
    focus = conversation.focus if feedback_id is None else turn.focus
    source = "explicit_dialogue" if feedback_id is None else "explicit_feedback"
    event_key = f"dialogue:{turn.id}" if feedback_id is None else f"feedback:{feedback_id}"
    text = _preference_text(question)
    controls = parse_preference_controls(text)
    if feedback_id is None:
        turn.focus = {**turn.focus, "preference_control_parse": control_audit(controls)}
    if not controls:
        return []
    mutations = [
        control
        for control in controls
        if control.operation in {"set", "promote", "correct", "revoke"}
    ]
    if not mutations:
        return []
    if prepare_only and any(control.operation != "set" for control in mutations):
        return _preference_conflict(
            clarifications, "目标答案模式不更正、撤销或提升已有偏好；请明确使用原偏好操作。"
        )
    if any(control.context_conflict or len(control.lifetime_hints) > 1 for control in mutations):
        return _preference_conflict(clarifications, _LIFETIME_CONFLICT)
    if any(control.operation in {"correct", "revoke"} for control in mutations) and any(
        control.operation in {"set", "promote"} for control in mutations
    ):
        return _preference_conflict(clarifications, _LIFETIME_CONFLICT)
    protected_lifetimes = {
        lifetime
        for control in controls
        if control.operation == "protect"
        for lifetime in control.protected_lifetimes
    }
    nonscope_clauses = {
        control.clause for control in controls if control.operation in {"protect", "context"}
    }
    scope_text = "，".join(
        clause for clause in _preference_clauses(text) if clause not in nonscope_clauses
    )
    authorized_settings = {control.clause for control in controls if control.operation == "set"}
    settings_only = feedback_id is not None or preference_feedback_only(question)
    events = _events(db, actor)
    active = [
        e
        for e in _active(events)
        if e["lifetime"] == "persistent" or e.get("conversation_id") == conversation.id
    ]
    # A quoted preference name may identify an already-authorized setting for
    # revocation; quote contents never supply a new value or durable consent.
    affirmative_clauses = _affirmative_preference_clauses(text)
    revoke_clauses = [
        c.clause for c in controls if c.operation == "revoke" and c.clause in affirmative_clauses
    ]
    correction_clauses = [
        c.clause for c in controls if c.operation == "correct" and c.clause in affirmative_clauses
    ]
    revoke, correction = bool(revoke_clauses), bool(correction_clauses)
    if revoke and correction:
        return []  # Conflicting settings operations require explicit resolution.
    if revoke or correction:
        controlling = [
            control
            for control in controls
            if control.clause in (revoke_clauses or correction_clauses)
        ]
        if len(controlling) != 1:
            if clarifications is not None:
                clarifications.append("请一次明确更正或撤销一项偏好；当前设置未改变。")
            return []
        control = controlling[0]
        corrected_scope = None
        protected = protected_preference_topics(db, actor, scope_text) if correction else set()
        if correction:
            scope_text = scope_text.replace(control.clause, control.scope_request or "")
            corrected_scope, scope_error = dialogue_preference_scope(
                db, actor, scope_text, focus, scoped=True, require_named=True
            )
            if scope_error:
                if clarifications is not None:
                    clarifications.append(scope_error)
                return []
        original_clause = next(
            (
                clause
                for clause in _preference_clauses(question)
                if _preference_text(clause).strip() == control.clause
            ),
            control.clause,
        )
        if revoke:
            references = re.split(
                r"撤销|取消|忘掉|revoke\s+|forget\s+|remove\s+",
                original_clause,
                maxsplit=1,
                flags=re.I,
            )[-1]
        elif control.target_reference is not None:
            references = re.split(r"改成|改为|调整为|限定为|限定到", original_clause, maxsplit=1)[0]
            references = references.split("把", 1)[-1]
        else:
            references = ""
        keys = ["requested_style"]
        if re.search(r"禁测|测验|出题|quiz|check", references, re.I):
            keys = ["check_understanding", "practice"]
        consumed = set()
        for previous_turn in db.scalars(
            select(Turn).where(Turn.conversation_id == conversation.id)
        ):
            consumed.update(
                previous_turn.focus.get("learning_preferences", {}).get("applied_event_ids", [])
            )
        active = [
            event
            for event in active
            if event["lifetime"] != "turn" or event["event_id"] not in consumed
        ]
        targets = _preference_targets(
            db, actor, active, keys, references, focus, protected=protected
        )
        if targets is None:
            if clarifications is not None:
                clarifications.append(
                    "你要更正或撤销哪一项偏好？请说明全局、具体主题或来源，以及偏好值，或提供事件 ID；当前设置未改变。"
                )
            return []
        if "all" in protected_lifetimes or any(
            target["lifetime"] in protected_lifetimes for _, target in targets
        ):
            return _preference_conflict(clarifications, _PROTECTION_CONFLICT)
        if control.lifetime_hints and any(
            target["lifetime"] not in control.lifetime_hints for _, target in targets
        ):
            return _preference_conflict(clarifications, _LIFETIME_CONFLICT)
        if correction:
            conflict = _correction_conflict(
                db,
                actor,
                events,
                targets,
                corrected_scope,
                protected,
                focus,
                conversation.id,
                turn_id=turn.id,
                next_turn=settings_only,
                consumed=consumed,
            )
            if conflict:
                if clarifications is not None:
                    clarifications.append(conflict)
                return []
        result = []
        for key, target in targets:
            scope = corrected_scope
            value = PreferenceEventInput(
                expected_revision=_profile(db, actor).revision,
                idempotency_key=f"{event_key}:{key}",
                operation="revoke" if revoke else "correct",
                key=key,
                value=None if revoke else target["value"],
                scope=PreferenceScope.model_validate(target["scope"]) if revoke else scope,
                lifetime=target["lifetime"],
                conversation_id=target.get("conversation_id"),
                target_event_id=target["event_id"],
                durable_consent=bool(target.get("durable_consent")),
            )
            result.append(
                append_preference_in(
                    db,
                    actor,
                    value,
                    turn_id=turn.id,
                    source=source,
                    feedback_id=feedback_id,
                    next_turn=settings_only and value.lifetime == "turn",
                )
            )
        return result
    # Current exception and future baseline are distinct. The latter must not
    # overwrite the exception or manufacture a lasting setting.
    current = text
    # A negative clause cannot provide the positive style it mentions. Keep a
    # later affirmative alternative, rather than dropping the entire request.
    affirmative_clauses = _affirmative_preference_clauses(current)
    temporary = any("turn" in control.lifetime_hints for control in mutations)
    changes: dict[str, Any] = {}
    supports: dict[str, list[str]] = {}
    style_requests = [
        (control.style_value, control.clause)
        for control in controls
        if control.operation == "set"
        and control.style_value
        and control.clause in affirmative_clauses
    ]
    if len({style for style, _ in style_requests}) > 1:
        return _preference_conflict(clarifications, _LIFETIME_CONFLICT)
    if len({style for style, _ in style_requests}) == 1:
        changes["requested_style"] = style_requests[0][0]
        supports["requested_style"] = [clause for _, clause in style_requests]
    optout_clauses = [
        clause
        for clause in _preference_clauses(current)
        if clause in authorized_settings
        and not _negated_preference_control(clause)
        and re.search(
            r"不(?:要|用|想|必).*?(?:出题|测验|测试|练习)|不测验|不出题|不测试|别(?:测验|出题)|no (?:quiz|practice)|don't (?:quiz|test)",
            clause,
            re.I,
        )
    ]
    if optout_clauses:
        changes["check_understanding"] = False
        supports["check_understanding"] = optout_clauses
        if any(
            re.search(r"练习|出题|只想阅读|practice", clause, re.I) for clause in optout_clauses
        ):
            changes["practice"] = False
            supports["practice"] = optout_clauses
    elif temporary and any(
        re.search(r"可以.*练习|给.*练习|one.*exercise", clause, re.I)
        for clause in affirmative_clauses
        if clause in authorized_settings
    ):
        changes["practice"] = True
        supports["practice"] = [
            clause
            for clause in affirmative_clauses
            if clause in authorized_settings
            and re.search(r"可以.*练习|给.*练习|one.*exercise", clause, re.I)
        ]
    # "Use this way in future" explicitly promotes the last observed choice in
    # this conversation only; no similarity/repeated-feedback inference.
    promotion_clauses = [
        control.clause
        for control in controls
        if control.operation == "promote"
        and _preference_lifetime(
            current,
            persistence_authorized=control.persistence_requested,
            lifetime_hints=control.lifetime_hints,
        )
        == "persistent"
    ]
    if promotion_clauses and not changes:
        previous = [
            e
            for e in _active(events)
            if e.get("conversation_id") == conversation.id
            and e["operation"] != "revoke"
            and e["key"] == "requested_style"
        ]
        if previous:
            changes["requested_style"] = max(previous, key=lambda event: event["revision"])["value"]
            supports["requested_style"] = promotion_clauses
    pending = []
    revision = _profile(db, actor).revision
    for key, choice in changes.items():
        supporting_controls = [control for control in controls if control.clause in supports[key]]
        lifetimes = {hint for control in supporting_controls for hint in control.lifetime_hints}
        schedules = {control.next_turn_requested for control in supporting_controls}
        if len(lifetimes) > 1 or len(schedules) > 1:
            return _preference_conflict(clarifications, _LIFETIME_CONFLICT)
        lifetime = _preference_lifetime(
            current,
            persistence_authorized=any(
                control.persistence_requested for control in supporting_controls
            ),
            lifetime_hints=lifetimes,
        )
        if "all" in protected_lifetimes or lifetime in protected_lifetimes:
            return _preference_conflict(clarifications, _PROTECTION_CONFLICT)
        durable = lifetime == "persistent"
        scope, scope_error = dialogue_preference_scope(
            db,
            actor,
            scope_text,
            focus,
            # Only the new target's preparation skips an implicit topic
            # fallback. Named/deictic scopes and exclusions are still parsed.
            scoped=durable or (feedback_id is not None and not target_answer_scope),
        )
        if scope_error:
            if clarifications is not None:
                clarifications.append(scope_error)
            return []
        value = PreferenceEventInput(
            expected_revision=revision + len(pending),
            idempotency_key=f"{event_key}:{key}",
            key=key,
            value=choice,
            scope=scope,
            lifetime=lifetime,
            conversation_id=conversation.id,
            durable_consent=durable,
        )
        explicit_current = any(
            "turn" in control.lifetime_hints and not control.next_turn_requested
            for control in supporting_controls
        )
        pending.append(
            (
                value,
                any(schedules) or feedback_id is not None or settings_only and not explicit_current,
            )
        )
    if prepare_only:
        return [{"value": value, "scope_text": scope_text} for value, _ in pending]
    # Validate all scopes and lifetimes before any append. A later ambiguous
    # activity scope must not leave an earlier style event partially committed.
    return [
        append_preference_in(
            db,
            actor,
            value,
            turn_id=turn.id,
            source=source,
            feedback_id=feedback_id,
            next_turn=apply_next,
        )
        for value, apply_next in pending
    ]


def capture_feedback_preferences_in(
    db, actor, conversation, turn, content, feedback_id, *, clarifications=None
):
    """Explicit feedback uses its original focus and applies only to a later turn.

    The caller owns the principal lock and persists the original Feedback row.
    Training consent is deliberately absent from this interface.
    """
    return capture_dialogue_preferences_in(
        db,
        actor,
        conversation,
        turn,
        content=content,
        feedback_id=feedback_id,
        clarifications=clarifications,
    )


def teaching_preferences(skill, preferences, contract):
    result = deepcopy(skill)
    if contract != PREFERENCE_CONTRACT:
        if preferences.get("requested_style") == "code_first":
            result["explanation_order"] = "code_first"
        return result
    style = preferences.get("requested_style")
    if style in {"concept_first", "code_first", "comparison_first"}:
        result["explanation_order"] = style
    if preferences.get("check_understanding") is False:
        result["check_understanding"] = False
        result["teaching_steps"] = [
            step
            for step in result.get("teaching_steps", [])
            if step["kind"] != "check_understanding"
        ]
    if preferences.get("practice") is False:
        result["exercise_prompts"] = []
        result["teaching_steps"] = [
            step
            for step in result.get("teaching_steps", [])
            if step["kind"] != "check_understanding"
            or preferences.get("check_understanding") is not False
        ]
    return result


def teaching_instruction(preferences, contract):
    if contract == PREFERENCE_CONTRACT:
        return _STYLE_INSTRUCTIONS.get(preferences.get("requested_style"))
    return None


class LearningEventsMixin:
    app: Any
    db: Any

    def interpret_learning_intent(self, actor, value):
        from .learning_intent import learning_intent

        with self.db.read() as db:
            private_namespace(db, actor)
            focus = {}
            if value.conversation_id:
                conversation = self.app.conversations._owned(db, actor, value.conversation_id)
                focus = conversation.focus
                sources = set(focus.get("project_source_ids", []))
                code_focus = focus.get("code_focus", {})
                if isinstance(code_focus, dict) and code_focus.get("source_id"):
                    sources.add(code_focus["source_id"])
                for source in sources:
                    require_object(db, actor, source, "source_set")
                    check_closure(db, actor, [source])
            return learning_intent(value.question, focus)

    def preference_event(self, actor, value: PreferenceEventInput):
        with self.db.transaction() as db:
            locked(db, Principal, actor)
            return append_preference_in(db, actor, value)

    def preference_events(self, actor):
        with self.db.transaction() as db:
            locked(db, Principal, actor)
            profile = _profile(db, actor)
            events = _events(db, actor)
            consumed = set()
            for turn in db.scalars(
                select(Turn)
                .join(Conversation, Turn.conversation_id == Conversation.id)
                .where(
                    Conversation.principal_id == actor,
                    Conversation.namespace_id == profile.namespace_id,
                )
            ):
                consumed.update(
                    turn.focus.get("learning_preferences", {}).get("applied_event_ids", [])
                )
            future = [
                event
                for event in _active(events)
                if event["lifetime"] != "turn" or event["event_id"] not in consumed
            ]
            return {
                "contract_version": PREFERENCE_CONTRACT,
                "revision": profile.revision,
                "events": events,
                "active_event_ids": [e["event_id"] for e in future],
                "competence_verified": False,
            }

    def effective_preferences(
        self, actor, *, conversation_id=None, topic_keys=None, source_ids=None
    ):
        with self.db.transaction() as db:
            locked(db, Principal, actor)
            focus = {"topic_keys": topic_keys or [], "project_source_ids": source_ids or []}
            if conversation_id:
                conv = self.app.conversations._owned(db, actor, conversation_id)
                focus = {
                    **conv.focus,
                    "preferences": conv.preferences,
                    **({"topic_keys": topic_keys} if topic_keys is not None else {}),
                    **({"project_source_ids": source_ids} if source_ids is not None else {}),
                }
            for topic in focus.get("topic_keys", []):
                require_topic(db, actor, topic)
            for source in focus.get("project_source_ids", []):
                require_object(db, actor, source, "source_set")
                check_closure(db, actor, [source])
            return preference_projection_in(db, actor, focus, conversation_id=conversation_id)

    def _navigation_target(self, db, actor, block_id, index):
        block = db.get(AnswerBlock, block_id)
        turn = db.get(Turn, block.turn_id) if block else None
        if not block or not turn:
            raise DomainError("NOT_FOUND", "源码导航回答不存在。", 404)
        self.app.conversations._owned(db, actor, turn.conversation_id)
        from ..models import AgentRun
        from .session_tutor import validate_session_tutor_in

        validate_session_tutor_in(
            db,
            actor,
            turn,
            run=db.get(AgentRun, turn.run_id) if turn.run_id else None,
            history=True,
        )
        obj = require_object(db, actor, block.id, "answer_block")
        check_closure(db, actor, [block.id])
        if block.content_hash != fingerprint(block.content) or obj.payload != block.content:
            raise DomainError("ARTIFACT_HASH", "回答完整性校验失败。")
        self.app.answers._validate_navigation_read(db, actor, turn, block.content)
        citations = block.content.get("citations", [])
        if not 0 <= index < len(citations):
            raise DomainError("NAVIGATION_CITATION", "导航需要已提交回答中的精确引用。", 422)
        citation = citations[index]
        if citation.get("citation_id"):
            verified = self.app.knowledge.evidence_dict(db, actor, citation["citation_id"])
            if any(
                citation.get(key) != verified[key]
                for key in (
                    "source_id",
                    "commit",
                    "path",
                    "start_line",
                    "end_line",
                    "file_hash",
                    "quote_hash",
                    "read_id",
                )
            ):
                raise DomainError("NAVIGATION_CITATION", "引用不匹配原始审核证据。", 422)
        target = {
            key: citation.get(key)
            for key in ("source_id", "commit", "path", "start_line", "end_line")
        }
        if any(value is None for value in target.values()):
            raise DomainError("NAVIGATION_CITATION", "此引用不提供可确认的源码位置。", 422)
        source = self.app.sources.get(db, actor, target["source_id"])
        if source.commit_sha != target["commit"] or target["path"] not in source.files:
            raise DomainError("NAVIGATION_CITATION", "引用不匹配当前固定快照。", 422)
        line_count = len(source.files[target["path"]].splitlines(keepends=True))
        if (
            type(target["start_line"]) is not int
            or type(target["end_line"]) is not int
            or not 1 <= target["start_line"] <= target["end_line"] <= line_count
        ):
            raise DomainError("NAVIGATION_CITATION", "引用行区间不匹配当前固定快照。", 422)
        return turn, target

    def acknowledge_navigation(self, actor, value: NavigationAckInput):
        with self.db.transaction() as db:
            locked(db, Principal, actor)
            ns = private_namespace(db, actor)
            turn, target = self._navigation_target(
                db, actor, value.answer_block_id, value.citation_index
            )
            expected = {key: getattr(value, key) for key in target}
            if target != expected:
                raise DomainError(
                    "NAVIGATION_ACK_MISMATCH",
                    "客户端确认必须匹配原始引用的快照、路径和完整行区间。",
                    422,
                )
            request_hash = fingerprint(value.model_dump(mode="json"))
            old = db.scalar(
                select(Idempotency).where(
                    Idempotency.principal_id == actor,
                    Idempotency.operation == "navigation.ack",
                    Idempotency.key == value.idempotency_key,
                )
            )
            if old:
                if old.request_hash != request_hash:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "导航确认键对应不同事件。")
                require_object(db, actor, old.result["ack_id"], "source_navigation_ack")
                return {**old.result, "replayed": True}
            payload = {
                "contract_version": ACK_CONTRACT,
                "turn_id": turn.id,
                "answer_block_id": value.answer_block_id,
                "citation_index": value.citation_index,
                **target,
                "server_location_valid": True,
                "client_highlight_ack": True,
                "ack_source": "client_reported_not_server_observed",
            }
            receipt = register(db, actor, ns.id, "source_navigation_ack", payload)
            result = {"ack_id": receipt.id, **payload}
            db.add(
                Idempotency(
                    principal_id=actor,
                    operation="navigation.ack",
                    key=value.idempotency_key,
                    request_hash=request_hash,
                    result=result,
                )
            )
            return result

    def navigation_status(self, actor, block_id, citation_index=0):
        with self.db.read() as db:
            ns = private_namespace(db, actor)
            turn, target = self._navigation_target(db, actor, block_id, citation_index)
            source = self.app.sources.get(db, actor, target["source_id"])
            body = source.files[target["path"]]
            displayed = "".join(
                body.splitlines(keepends=True)[target["start_line"] - 1 : target["end_line"]]
            )
            ack_ids = []
            for row in db.scalars(
                select(ObjectRow).where(
                    ObjectRow.namespace_id == ns.id, ObjectRow.kind == "source_navigation_ack"
                )
            ):
                require_object(db, actor, row.id, "source_navigation_ack")
                if (
                    row.payload.get("answer_block_id") == block_id
                    and row.payload.get("citation_index") == citation_index
                ):
                    if any(row.payload.get(key) != choice for key, choice in target.items()):
                        raise DomainError("NAVIGATION_ACK_MISMATCH", "历史客户端确认与引用不一致。")
                    ack_ids.append(row.id)
            return {
                "contract_version": ACK_CONTRACT,
                "turn_id": turn.id,
                "answer_block_id": block_id,
                "citation_index": citation_index,
                **target,
                "server_location_valid": True,
                "client_highlight_ack": bool(ack_ids),
                "ack_ids": ack_ids,
                "ack_source": "client_reported_not_server_observed" if ack_ids else None,
                "display": {
                    "text": displayed,
                    "start_line": target["start_line"],
                    "end_line": target["end_line"],
                    "text_sha256": digest(displayed),
                    "file_sha256": digest(body),
                    "content_complete": True,
                },
            }
