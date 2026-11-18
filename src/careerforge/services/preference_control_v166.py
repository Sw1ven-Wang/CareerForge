"""Bounded positive grammar for personal preference control, never mention mining.

Each admitted control consumes a complete imperative/personal-preference clause.
Durable controls also require declarative/directive sentence mood and understood
surrounding clauses. An unknown matrix clause, question, quotation, or unparsed
suffix cannot grant consent. Unsupported language is deliberately fail-closed.
The separate ACL-aware resolver must still validate every requested scope.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from typing import Any

CONTROL_CONTRACT = "personal-preference-control-1.6.6"


@dataclass(frozen=True)
class PreferenceControl:
    clause: str
    operation: str
    actor: str
    explicit_personal_frame: bool
    persistence_requested: bool
    scope_request: str | None = None
    target_reference: str | None = None
    style_value: str | None = None
    lifetime_hints: tuple[str, ...] = ()
    protected_lifetimes: tuple[str, ...] = ()
    next_turn_requested: bool = False
    context_conflict: bool = False


def clauses(text: str) -> list[str]:
    return [
        value.strip()
        for value in re.split(
            r"[，,。；;!?！？\n]|\.(?=\s|$)|而是|但是|\b(?:instead|but|however)\b",
            text,
            flags=re.I,
        )
        if value.strip()
    ]


_PERSONAL = re.compile(
    r"^(?:请(?:你)?|麻烦(?:你)?|帮我|我(?:更)?(?:想|希望|要求|习惯|喜欢|偏好)|"
    r"本人(?:希望|要求)|我的偏好(?:是|为)|"
    r"please\s+|I\s+(?:want|prefer|would like|request)\s+(?:you\s+to\s+)?|"
    r"my preference is\s+)",
    re.I,
)
_TIME = re.compile(
    r"^(?:从今以后|从现在起|以后|今后|之后|每次|后面几轮|接下来几轮|这次会话|本次会话|"
    r"(?:仅|只)?(?:今天这一次|这一次|这次|本次)|下一次|下次|下一轮|"
    r"from now on\b\s*|always\b\s*|this time\b\s*|just this time\b\s*|"
    r"only this time\b\s*|next time\b\s*|this conversation\b\s*|next few turns\b\s*)",
    re.I,
)

_SCOPE_PREFIX = re.compile(
    r"^(?:(?:学习|讲解|解释|学)\s*[^，,。；;:：!?！？]{0,70}?(?:时|都)(?![A-Za-z])|"
    r"(?:只在|在)\s*[^，,。；;:：!?！？]{1,70}?(?:里|中|时|(?=先))|"
    r"[^，,。；;:：!?！？]{1,70}?相关(?:问题|主题|技术)|"
    r"[^，,。；;:：!?！？]{1,70}?(?:这个|这一)(?:主题|话题)|"
    r"所有(?:技术|主题|项目)|每次(?:讲解|解释|学习)|"
    r"for\s+[^,;:!?]{1,70}?\s+(?=from now on\b|always\b|please\b|use\b)|"
    r"when\s+(?:learning|studying)\s+[^,;:!?]{1,70}?\s+(?=always\b|please\b|use\b))",
    re.I,
)
_CONTEXT = re.compile(
    r"^(?:(?:这个|当前|本次)(?:机制|函数|主题|项目|技术)(?:也|还是)?(?:是)?|都|也|仍然|继续|例外|但是|但|不过)"
)
_DURABLE = re.compile(r"从今以后|从现在起|以后|今后|每次|from now on|always", re.I)
_SAVE = re.compile(
    r"^(?:(?:请(?:你)?|帮我)?(?:记住|保存)(?:我的|这个)?偏好|"
    r"(?:please\s+)?(?:remember|save)\s+my\s+preference)\s*[:：]?\s*",
    re.I,
)
_STYLE_ACTION = re.compile(
    r"(?:"
    r"先(?:看|给出|给我|展示|讲|看到|讲一个|展示给我|看一下)\s*(?:关键|核心|一个)?\s*(?:代码|源码|函数|架构图?|整体结构|图|通用概念|概念|证据)"
    r"(?:再(?:讲|看|解释|讲解)(?:代码|结论|原理|机制|概念|条件))?|"
    r"先把整体结构(?:说清楚|说明白|讲清楚)|直接看源码|只看核心函数|"
    r"(?:代码|架构|整体结构|概念|证据|对比)优先|"
    r"(?:use\s+|show\s+(?:me\s+)?)?(?:code|architecture|concept|evidence|comparison)\s+first"
    r")",
    re.I,
)
_ACTIVITY_ACTION = re.compile(
    r"(?:(?:不要|不用|不想|不必|别)(?:给我|再)?(?:出题|测验|测试|练习)|"
    r"不(?:测验|出题|测试)|可以给(?:我|一个|我一个)?小?练习|给(?:我|一个|我一个)小?练习|"
    r"no\s+(?:quiz|practice)|don['’]t\s+(?:quiz|test)(?:\s+me)?|one\s+(?:small\s+)?exercise)",
    re.I,
)
_PROMOTE_ACTION = re.compile(r"(?:按(?:这个方式|这个顺序)|(?:use|keep)\s+this\s+way)", re.I)
_ACTION_TAIL = re.compile(
    r"\s*(?:即可|就好|吧|please)?\s*"
    r"(?:\s+(?:for|about|only for)\s+[^,;!?]{1,100})?\s*",
    re.I,
)
_MOVE = re.compile(
    r"把(?P<target>[^，,。；;:：!?！？]{1,100}?(?:偏好|设置))"
    r"(?:改成|改为|调整为|限定为|限定到)(?P<scope>[^，,。；;:：!?！？]+)",
    re.I,
)
_CORRECT = re.compile(
    r"(?:我说的只针对|我刚才说的只是|(?:这个偏好)?(?:仅限于?|只用于)|只是|"
    r"(?:I meant|apply it|limit (?:it|this))\s+(?:only\s+)?(?:for\s+|to\s+)?)"
    r"(?P<scope>[^，,。；;:：!?！？]+)",
    re.I,
)
_REVOKE = re.compile(r"(?:撤销|取消|忘掉|revoke\s+|forget\s+|remove\s+)", re.I)
_TARGET_END = re.compile(
    r"(?:偏好|设置|setting|settings|preference|preferences|[a-f0-9]{8}-[a-f0-9-]{27,})\s*(?:即可|吧|please)?$",
    re.I,
)
# These positive adjunct grammars may accompany a control. They do not create
# controls or persistence by themselves; unknown discourse remains untrusted.
_ADJUNCT = re.compile(
    r"(?:但)?(?:再(?:讲|看|解释|讲解)(?:代码|结论|原理|机制|概念|条件)|"
    r"再理解(?:它的)?(?:条件|原理|机制|概念)|"
    r"我只想(?:阅读|看)(?:源码|代码)|"
    r"通用概念不用强制|"
    r"(?:这个解释|讲得|这)?太(?:抽象|复杂|简单)(?:了)?|"
    r"(?:For|About)\s+[\w .-]+|"
    r"(?:不是|而非|不要用于|不适用于|不用于|not(?:\s+for)?|rather than)\s*[^:：!?！？]+|"
    r"[^:：!?！？]+(?:不适用|除外|不要改|保持原状|保持不变|不变|不用这个偏好|unchanged)|"
    r"(?:请)?(?:不要改|保留|保持|do not change|leave)\s*[^:：!?！？]+|"
    r"(?:do not|don['’]t)\s+(?:use|apply)(?:\s+(?:it|this|that|the preference))?\s+(?:for|to|on)\s+[^:：!?！？]+|"
    r"(?:不要|请勿|别)(?:保存|记住|撤销|取消|修改|改变|设置)[^:：!?！？]+|"
    r"(?:我刚才说的不是)[^:：!?！？]+|"
    r"之后维持原设置|按默认解释即可|我想自己选|先证据再结论的那个)",
    re.I,
)


_EPHEMERAL_ADJUNCT = re.compile(
    r"(?:但)?(?:(?:命令)?名称|命令名)(?:是|为)\s*__quoted_preference_data__|"
    r"(?:以后|今后)再(?:研究|学习|解释)(?:这个|当前)(?:函数|机制|主题)|"
    r"(?:这一次|今天这一次|这次|本次|下一次|下次|下一轮)(?:例外)?|"
    r"(?:这次|本次)?(?:不要|不用)(?:任何)?(?:源码|代码)",
    re.I,
)


_PROTECTION_TARGET = re.compile(
    r"(?:(?:我的|我原来的|原来的|之前的|现有的|这个)\s*)?"
    r"(?P<zh_lifetime>长期|永久|持久|已保存的)?(?:偏好|设置)|"
    r"(?:(?:my|the|existing|original)\s+)?"
    r"(?:(?P<en_lifetime>long[- ]term|persistent|permanent|saved)\s+)?"
    r"(?:preferences?|settings?)",
    re.I,
)
_PROTECTION_FRAMES = (
    r"(?:请(?:你)?|麻烦(?:你)?)?(?:暂时)?(?:不要|请勿|别)(?:修改|改变|更改|调整|覆盖|更新|改动|改|动)(?P<target>.+)",
    r"(?:请(?:你)?|麻烦(?:你)?)?(?:保留|保持)(?P<target>.+?)(?:不变|原样|即可)?",
    r"(?P<target>.+?)(?:保持不变|保持原状|不要改|别动|不变)",
    r"(?:please\s+)?(?:do not|don't)\s+(?:change|modify|update|overwrite|alter)\s+(?P<target>.+)",
    r"(?:please\s+)?(?:keep|preserve|leave)\s+(?P<target>.+?)(?:\s+(?:unchanged|intact))?",
)


def _protection(clause: str) -> PreferenceControl | None:
    """An existing-state constraint is neither a value nor a new scope."""
    for frame in _PROTECTION_FRAMES:
        match = re.fullmatch(r"(?:但是|但|不过)?" + frame, clause, re.I)
        target = _PROTECTION_TARGET.fullmatch(match["target"].strip()) if match else None
        if target:
            lifetime = "persistent" if any(target.groups()) else "all"
            return PreferenceControl(
                clause,
                "protect",
                "self_or_addressee",
                True,
                False,
                protected_lifetimes=(lifetime,),
            )
    return None


def _time_lifetime(token: str, following: str = "") -> str | None:
    token = token.strip()
    if _DURABLE.fullmatch(token) or token == "之后" and re.match(r"学", following):
        return "persistent"
    if (
        re.fullmatch(
            r"后面几轮|接下来几轮|这次会话|本次会话|this conversation|next few turns", token, re.I
        )
        or token == "之后"
        and following.startswith("继续")
    ):
        return "conversation"
    return "turn" if token != "之后" else None


def _temporary_adjunct(part: str) -> bool:
    if _EPHEMERAL_ADJUNCT.fullmatch(part):
        return True
    negative = re.fullmatch(r"(?:以后|今后)?(?:不要|不用|不想|别)(.+)", part)
    return bool(negative and _STYLE_ACTION.fullmatch(negative[1]))


def _scope_operand(value: str) -> str | None:
    """Require a scope-control complement; the resolver consumes its names."""
    match = re.fullmatch(
        r"(?:只对|仅对)\s*(.+?)\s*生效|"
        r"(?:只针对|仅限于?|只用于|仅用于|only\s+for)\s*(.+)",
        value.strip(),
        re.I,
    )
    if match:
        return "只针对 " + next(group for group in match.groups() if group is not None)
    return None


def _style_value(action: str) -> str | None:
    for value, words in [
        ("architecture_first", r"architecture|架构|整体结构|先看图"),
        ("evidence_first", r"evidence|证据"),
        ("concept_first", r"concept|概念"),
        ("comparison_first", r"comparison|对比"),
        ("code_first", r"code|代码|源码|函数"),
    ]:
        if re.search(words, action, re.I):
            return value
    return None


def style_reference_matches(text: str) -> list[tuple[str, int, int]]:
    """Recognized selector spans only; callers must reject every other word.

    Reuse the positive action grammar rather than matching arbitrary language
    between style keywords inside a quoted/unquoted target description.
    """
    matches = []
    for match in _STYLE_ACTION.finditer(text):
        value = _style_value(match.group())
        if value is not None:
            matches.append((value, match.start(), match.end()))
    for value in (
        "architecture_first",
        "evidence_first",
        "concept_first",
        "comparison_first",
        "code_first",
    ):
        for match in re.finditer(r"(?<![A-Za-z0-9_])" + value + r"(?![A-Za-z0-9_])", text, re.I):
            matches.append((value, match.start(), match.end()))
    return matches


def _one(clause: str) -> PreferenceControl | None:
    remainder = clause.strip()
    protection = _protection(remainder)
    if protection:
        return protection
    explicit, persistence = False, False
    lifetimes: set[str] = set()
    next_turn = False
    save = _SAVE.match(remainder)
    if save:
        explicit, persistence = True, True
        lifetimes.add("persistent")
        remainder = remainder[save.end() :].strip()
    if re.search(r"[:：]", remainder):
        return None
    for _ in range(12):
        personal = _PERSONAL.match(remainder)
        time = _TIME.match(remainder)
        scope = None if re.fullmatch(r"只在.+这样", remainder) else _SCOPE_PREFIX.match(remainder)
        context = _CONTEXT.match(remainder)
        match = personal or time or scope or context
        if not match:
            break
        if personal:
            explicit = True
        if time:
            hint = _time_lifetime(time.group(), remainder[time.end() :].strip())
            if hint:
                lifetimes.add(hint)
            persistence = persistence or hint == "persistent"
            next_turn = next_turn or bool(
                re.fullmatch(r"下一次|下次|下一轮|next time\s*", time.group(), re.I)
            )
        remainder = remainder[match.end() :].strip()
    if not remainder and lifetimes and "persistent" not in lifetimes:
        return PreferenceControl(
            clause,
            "context",
            "self_or_addressee",
            explicit,
            False,
            lifetime_hints=tuple(sorted(lifetimes)),
            next_turn_requested=next_turn,
        )
    move = _MOVE.fullmatch(remainder)
    if move:
        scope_request = _scope_operand(move["scope"])
        if scope_request:
            return PreferenceControl(
                clause,
                "correct",
                "self_or_addressee",
                True,
                False,
                scope_request,
                move["target"],
                lifetime_hints=tuple(sorted(lifetimes)),
                next_turn_requested=next_turn,
            )
        return None
    correction = _CORRECT.fullmatch(remainder)
    contextual = re.fullmatch(r"只在(.+?)这样", remainder)
    if correction or contextual:
        if correction:
            operand = correction["scope"]
        else:
            assert contextual is not None
            operand = contextual[1]
        return PreferenceControl(
            clause,
            "correct",
            "self_or_addressee",
            True,
            False,
            "只针对 " + operand,
            lifetime_hints=tuple(sorted(lifetimes)),
            next_turn_requested=next_turn,
        )
    revoke = _REVOKE.match(remainder)
    if revoke and _TARGET_END.search(remainder[revoke.end() :]):
        return PreferenceControl(
            clause,
            "revoke",
            "self_or_addressee",
            True,
            False,
            target_reference=remainder[revoke.end() :],
            lifetime_hints=tuple(sorted(lifetimes)),
            next_turn_requested=next_turn,
        )
    for operation, pattern in [
        ("set", _STYLE_ACTION),
        ("set", _ACTIVITY_ACTION),
        ("promote", _PROMOTE_ACTION),
    ]:
        action = pattern.match(remainder)
        if not action:
            continue
        tail = remainder[action.end() :]
        suffix_time = re.search(r"\s+from now on\s*$", tail, re.I)
        if suffix_time:
            persistence = True
            lifetimes.add("persistent")
            tail = tail[: suffix_time.start()]
        if _ACTION_TAIL.fullmatch(tail):
            style_value = _style_value(action.group()) if pattern is _STYLE_ACTION else None
            return PreferenceControl(
                clause,
                operation,
                "self_or_addressee",
                explicit,
                persistence,
                style_value=style_value,
                lifetime_hints=tuple(sorted(lifetimes)),
                next_turn_requested=next_turn,
            )
    return None


def parse_preference_controls(text: str) -> list[PreferenceControl]:
    """Parse sanitized user text without turning a mention into permission.

    Punctuation is retained until sentence mood has been checked. A complete
    direct command is necessary but insufficient if it sits inside unexplained
    surrounding discourse. This also covers multi-sentence reports/examples
    without maintaining lists of reporting verbs or hypothetical keywords.
    """
    parts = clauses(text)
    parsed = [_one(part) for part in parts]
    if re.search(r"[?？]", text):
        return []
    controls = []
    context = None
    for control in parsed:
        if control is None:
            continue
        lasting = control.persistence_requested or control.operation in {"correct", "revoke"}
        if any(
            other is None
            and not _ADJUNCT.fullmatch(part)
            and (lasting or not _temporary_adjunct(part))
            for part, other in zip(parts, parsed, strict=True)
        ):
            continue
        if (
            control.operation in {"context", "set", "promote", "correct", "revoke"}
            and context is not None
        ):
            control = replace(
                control,
                lifetime_hints=tuple(
                    sorted(set(control.lifetime_hints) | set(context.lifetime_hints))
                ),
                context_conflict=control.context_conflict
                or context.context_conflict
                or bool(
                    "turn" in control.lifetime_hints
                    and "turn" in context.lifetime_hints
                    and control.next_turn_requested != context.next_turn_requested
                ),
                next_turn_requested=control.next_turn_requested or context.next_turn_requested,
            )
        if control.operation == "context":
            context = control
        elif control.operation in {"set", "promote", "correct", "revoke"}:
            context = None
        controls.append(control)
    return controls


def control_audit(controls: list[PreferenceControl]) -> dict[str, Any]:
    return {"contract_version": CONTROL_CONTRACT, "controls": [asdict(item) for item in controls]}
