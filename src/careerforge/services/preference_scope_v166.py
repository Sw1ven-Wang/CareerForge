"""Conservative personal-intent and authorized topic-scope parsing.

Named user scopes outrank conversational context. The current focus is only a
fallback for an unqualified request, never evidence that a different named topic
means the current one. Unsupported unions/exclusions are clarified, not widened.
"""

from __future__ import annotations

import re

from ..contracts_learning_events_v166 import PreferenceScope
from .taxonomy import visible_topics

_SCOPE_ERROR = "请明确这项偏好适用于哪个已授权主题或来源；当前设置未改变，也不会用当前焦点替代你指定或排除的范围。"
_NEGATIVE_SCOPE_PREFIX = re.compile(
    r"(?:不是|而非|不(?:要)?(?:用于|适用于|应用于|用在|针对|包括)|排除|除了|"
    r"not(?:\s+(?:for|to|about|on|in|including))?|except(?:\s+for)?|excluding|exclude|rather\s+than|"
    r"(?:do\s+not|don['’]t)\s+(?:use|apply)(?:\s+(?:it|this|that|the preference))?\s+(?:for|to|on))\s*(?:整个|这个|当前|全部)?[（(]?\s*$",
    re.I,
)
_NEGATIVE_SCOPE_SUFFIX = re.compile(
    r"^\s*(?:除外|不适用|不包括|不用这个偏好|不要改|保持原状|保持不变|不变|excluded\b|is excluded\b|unchanged\b)",
    re.I,
)
_PROTECTED_SUFFIX = re.compile(r"^\s*(?:不要改|保持原状|保持不变|不变|unchanged\b)", re.I)
_PROTECTED_PREFIX = re.compile(r"(?:不要改|保留|保持|do not change|leave)\s*$", re.I)


def _term_pattern(term):
    left = r"(?<![A-Za-z0-9_])" if term[0].isascii() and term[0].isalnum() else ""
    right = r"(?![A-Za-z0-9_])" if term[-1].isascii() and term[-1].isalnum() else ""
    return left + re.escape(term) + right


def _clauses(text):
    return re.split(r"[，,。；;!?！？\n]|\.(?=\s|$)|但是|\b(?:but|however)\b", text, flags=re.I)


def _mentions(text, named_terms):
    included, excluded = set(), set()
    for clause in _clauses(text):
        for key, terms in named_terms.items():
            for term in terms:
                for match in re.finditer(_term_pattern(term), clause, re.I):
                    negative = (
                        _NEGATIVE_SCOPE_PREFIX.search(clause[: match.start()])
                        or _NEGATIVE_SCOPE_SUFFIX.search(clause[match.end() :])
                        or _PROTECTED_PREFIX.search(clause[: match.start()])
                    )
                    (excluded if negative else included).add(key)
    return included, excluded


def _scope_fragments(text):
    named_title = r"([^，,。；;!?！？]{1,70}?)(?:这个|这一)(?:主题|话题)"
    patterns = [
        r"^(?:不是|而非|不要用于|不适用于|不用于|not(?:\s+for)?|rather than)\s*(.+)$",
        r"^(.+?)(?:不适用|除外|不要改|保持原状|保持不变|不变|不用这个偏好|unchanged)\s*$",
        r"^(?:请)?(?:不要改|保留|保持|do not change|leave)\s*(.+?)(?:不变|原样|unchanged)?$",
        r"^(?:do not|don['’]t)\s+(?:use|apply)(?:\s+(?:it|this|that|the preference))?\s+(?:for|to|on)\s+(.+)$",
        r"(?:只针对|针对|只对|仅对|仅限于?|限定在|适用于|用于|关于|对于|只是|只在)\s*(.+?)(?=生效|[，,。；;!?！？]|$)",
        r"(?:学习|讲解|解释|学)\s*(.*?)(?=时|都)",
        r"([^，,。；;!?！？]{1,70}?)相关(?:问题|主题|技术)",
        named_title,
        r"\b(?:only\s+for|only\s+when\s+(?:learning|studying)|when\s+(?:learning|studying)|for|about|limit\s+(?:it|this)\s+to)\s+(.+?)(?=\s+(?:from now on|always|use|please|but|not)\b|[,;!?]|\.(?:\s|$)|$)",
    ]
    fragments = []
    for pattern in patterns:
        for clause in _clauses(text):
            for match in re.finditer(pattern, clause, re.I):
                fragment = match.group(1).strip()
                if not fragment:
                    continue
                # In "学习这个主题", the phrase before "这个主题" is a
                # grammatical introducer, not an omitted or unknown topic name.
                if pattern == named_title and re.fullmatch(
                    r"(?:(?:以后|今后|之后|请|我希望|我想)\s*)*(?:学习|讲解|解释|学)",
                    fragment,
                ):
                    continue
                fragments.append(fragment)
    return fragments


def protected_preference_topics(db, actor, text):
    """Explicit preservation constraints are not implicit new preferences."""
    protected = set()
    for topic in visible_topics(db, actor):
        for clause in _clauses(text):
            for name in [topic.key, topic.title, *topic.aliases]:
                if not isinstance(name, str) or len(name) < 2:
                    continue
                for match in re.finditer(_term_pattern(name), clause, re.I):
                    if _PROTECTED_PREFIX.search(
                        clause[: match.start()]
                    ) or _PROTECTED_SUFFIX.search(clause[match.end() :]):
                        protected.add(topic.key)
    return protected


def dialogue_preference_scope(db, actor, text, focus, *, scoped=False, require_named=False):
    """Return one exact representable scope or a clarification, without writes."""
    topics = visible_topics(db, actor)
    named = {
        topic.key: list(
            dict.fromkeys(
                [
                    topic.key,
                    topic.title,
                    *[
                        alias
                        for alias in topic.aliases
                        if isinstance(alias, str) and len(alias) >= 3
                    ],
                ]
            )
        )
        for topic in topics
    }
    included, excluded = _mentions(text, named)
    special = {
        "global": [
            "全局",
            "所有技术",
            "所有主题",
            "所有项目",
            "global",
            "globally",
            "all topics",
            "all projects",
            "每次讲解",
            "每次解释",
            "每次学习",
            "every explanation",
            "every lesson",
            "every time you explain",
        ],
        "project_facts": ["项目事实", "project facts"],
        "current_topic": [
            "这个主题",
            "当前主题",
            "本主题",
            "这个技术",
            "当前技术",
            "this topic",
            "current topic",
        ],
        "current_source": ["这个项目", "当前项目", "本项目", "this project", "current project"],
    }
    special_in, special_out = _mentions(text, special)
    terms = sorted(
        {term for values in [*named.values(), *special.values()] for term in values},
        key=len,
        reverse=True,
    )
    # An unknown explicitly named scope cannot disappear merely because a known
    # current topic or another recognized name also exists in the sentence.
    for fragment in _scope_fragments(text):
        if not any(
            re.search(_term_pattern(term), fragment, re.I) for term in terms
        ) and not re.fullmatch(r"\s*(?:me|myself)\s*", fragment, re.I):
            return None, _SCOPE_ERROR
        residue = fragment
        for term in terms:
            residue = re.sub(_term_pattern(term), " ", residue, flags=re.I)
        residue = re.sub(
            r"相关|问题|技术|主题|内容|语言|整个|这个|当前|本次|这类|学习|的|和|与|或|以及|以后|今后|之后|先|都|也|有|时|这样|这个方式|这个顺序|我|\b(?:rather than|except|excluding|and|or|only|topics?|technology|language|learning|studying|me|myself|next time|this way)\b",
            " ",
            residue,
            flags=re.I,
        )
        if re.search(r"[\w\u4e00-\u9fff]", residue):
            return None, _SCOPE_ERROR
    if included & excluded or special_in & special_out or len(included) > 1:
        return None, _SCOPE_ERROR
    if "global" in special_in:
        if included or excluded or special_out or special_in - {"global"}:
            return None, _SCOPE_ERROR
        return PreferenceScope(), None
    if included:
        if special_in - {"current_topic"}:
            return None, _SCOPE_ERROR
        return PreferenceScope(kind="topic", identifier=next(iter(included))), None
    if "project_facts" in special_in:
        if special_in - {"project_facts"}:
            return None, _SCOPE_ERROR
        return PreferenceScope(kind="project_facts"), None
    if "current_source" in special_in:
        sources = list(dict.fromkeys(focus.get("project_source_ids", [])))
        if len(sources) == 1 and special_in == {"current_source"}:
            return PreferenceScope(kind="source", identifier=sources[0]), None
        return None, _SCOPE_ERROR
    if excluded or "global" in special_out:
        return None, _SCOPE_ERROR
    if require_named and "current_topic" not in special_in:
        return None, _SCOPE_ERROR
    if scoped or "current_topic" in special_in:
        current = list(dict.fromkeys([*focus.get("topic_keys", []), *focus.get("topic_hints", [])]))
        if len(current) == 1 and current[0] in named:
            return PreferenceScope(kind="topic", identifier=current[0]), None
        if current or special_in:
            return None, _SCOPE_ERROR
    return PreferenceScope(), None
