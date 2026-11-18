"""The model proposes; the server authorizes. Inferred topics never grant scope."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from ..contracts import (
    CoordinatorAction,
    IntentAction,
    IntentDecision,
    OrchestrationDecision,
    TaskType,
)
from ..errors import DomainError
from ..models import ObjectRow, SourceSet
from .dependencies import check_closure
from .explicit_source_paths import SOURCE_PATH_CONTRACT, requested_source_path
from .learning_intent import (
    LEARNING_ROUTING_CONTRACT,
    PAIRED_COMPARISON_CONTRACTS,
    SCOPED_QUESTION_ROUTING_CONTRACT,
    learning_rule_intent,
)
from .retrieval_context import (
    LEGACY_FACT_RETRIEVAL_CONTEXT_CONTRACT,
    LEGACY_RETRIEVAL_CONTEXT_CONTRACT,
    RETRIEVAL_CONTEXT_CONTRACT,
    WHOLE_PACK_RETRIEVAL_CONTEXT_CONTRACT,
    is_topic_continuation,
)
from .security import accessible_namespaces
from .taxonomy import require_topic, resolve_topics, term_matches, topic_object_id, visible_topics

ROUTING_CONTRACT_VERSION = "intent-actions-1.6.3"
LEGACY_ROUTING_CONTRACT_VERSION = "intent-actions-1.6.2"

# Rules deliberately cover only unambiguous requests. Quotes, negation and
# conflicting actions require the funded coordinator (or a clarification).
_INTENT_TERMS: dict[TaskType, tuple[str, ...]] = {
    TaskType.DISPUTE: ("有误", "错了", "质疑", "证据错误"),
    TaskType.IMPORT: ("岗位", "职位要求", "job description", "导入jd"),
    TaskType.DISCOVER: ("帮我找", "调研", "搜索项目", "寻找项目", "find repositories"),
    TaskType.COMPARE: ("对比", "比较", "区别", "其他项目", "各个项目", "compare"),
    TaskType.LOCATE: ("在哪里", "在哪", "定位", "打开函数", "locate", "where is"),
    TaskType.VERIFY: ("什么依据", "证据", "引用", "verify"),
    TaskType.PLAN: ("学习计划", "学习路线", "安排学习", "learning plan"),
    TaskType.ARCHITECTURE: ("架构", "architecture", "数据流"),
    TaskType.PRACTICE: ("练习", "帮我实现", "写代码", "修改代码", "practice"),
    TaskType.PROGRESS: ("任务进度", "运行状态", "查询进度"),
    TaskType.CONCEPT: ("什么是", "概念", "先修", "what is"),
    TaskType.IMPLEMENTATION: ("学习", "实现", "讲解", "解释", "explain"),
}
_QUOTED = re.compile(
    r'```[\s\S]*?```|`[^`\n]*`|"[^"\n]*"|(?<!\w)\'[^\'\n]*\'(?!\w)|'
    r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|^\s*>[^\n]*",
    re.MULTILINE,
)
_NEGATION = re.compile(
    r"\b(?:not|never|without|avoid|except|instead|neither|nor|no)\b|"
    r"\b\w+n[’']t\b|不|勿|别|避免|无需|无须|并非|从未|没有",
    re.IGNORECASE,
)
_COORDINATION = re.compile(
    r"\b(?:and|then|also|before|after|plus|followed by)\b|[&+/]|和|并|同时|然后|再|以及|[；;，,。\n]",
    re.I,
)
_ACTION_CLAUSE = re.compile(
    r"(?:\A|[.;,!?&+\n]\s*|\s/\s*|\b(?:and|then|also|next|afterwards|before|after|followed by)\b\s+)"
    r"\s*(?:(?:please|first|next|then|also)\s+)*"
    r"(?P<english>compare|verify|locate|explain|describe|teach|learn|plan|"
    r"write|run|execute|change|edit|fix|create|build|import|delete|send|publish|"
    r"install|deploy|test|implement|search|find|open|read|show|summarize|"
    r"writing|running|executing|changing|editing|fixing|creating|building|importing|deleting|"
    r"sending|publishing|installing|deploying|testing|implementing|searching|opening|reading)(?![\w.-])|"
    r"(?:\A|[，。；;,.!?&+\n]|\s/\s|和|并且|并|然后|再|同时|接着|以及)\s*(?:请|先|再|然后)*"
    r"(?P<chinese>验证|核验|核实|定位|解释|讲解|比较|对比|编写|写|运行|执行|修复|修改|"
    r"删除|发送|发布|部署|创建|生成|导入|安装|测试|练习|学习|查找|搜索|打开|阅读|总结|分析)",
    re.IGNORECASE,
)

# A bounded editing-action family, not a new positive intent or permission rule.
# Normalize only for clause detection; never alter question/model input, source
# names, quoted data, paths, or identifiers. Keep _ACTION_CLAUSE unchanged so old
# unmarked queued turns retain their exact earlier detection behavior.
_EDIT_ACTION_FORMS = re.compile(
    r"(?<![\w./-])(?:edit(?:s|ed|ing)?|chang(?:e|es|ed|ing)|"
    r"modif(?:y|ies|ied|ying)|alter(?:s|ed|ing)?|revis(?:e|es|ed|ing)|"
    r"adjust(?:s|ed|ing)?|updat(?:e|es|ed|ing)|amend(?:s|ed|ing)?|"
    r"rewrit(?:e|es|ing)|rewrote|rewritten|patch(?:es|ed|ing)?|"
    r"refactor(?:s|ed|ing)?)(?![\w./-])",
    re.I,
)

_QUALIFIED_IDENTIFIER = re.compile(
    r"(?<![\w./-])(?:[A-Za-z0-9_.-]+/)*"
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+(?![\w./-])"
)


def _routing_language(question: str, contract: str | None) -> str:
    # Used only by the deterministic classifier. Keep the original request,
    # exact symbol spelling and all provider receipt bytes untouched. A dot
    # inside a qualified identifier is not a new imperative sentence; masking
    # it does not hide a following semicolon, conjunction or actual action.
    if contract in {
        ROUTING_CONTRACT_VERSION,
        LEARNING_ROUTING_CONTRACT,
        SCOPED_QUESTION_ROUTING_CONTRACT,
    }:
        return _QUALIFIED_IDENTIFIER.sub(lambda match: "_" * len(match.group(0)), question)
    return question


def _editing_action_language(question: str) -> str:
    parts = []
    start = 0
    for quoted in _QUOTED.finditer(question):
        parts.extend(
            [_EDIT_ACTION_FORMS.sub("edit", question[start : quoted.start()]), quoted.group(0)]
        )
        start = quoted.end()
    parts.append(_EDIT_ACTION_FORMS.sub("edit", question[start:]))
    return "".join(parts)


_SCOPE_SWITCH = re.compile(
    r"^\s*(?:(?:please\s+)?(?:switch|change)(?:\s+(?:the\s+)?(?:project|repository|repo|focus))?\s+to\s+|"
    r"(?:请)?(?:切换(?:项目|仓库|焦点)?到|转到(?:项目|仓库)?|将焦点切换到)\s*)"
    r"(?:https://github\.com/)?(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?![\w/.-])",
    re.I,
)


def _explicit_scope_switch(question: str) -> str | None:
    """Only an explicit, unquoted, non-negated full identity can change focus."""
    if _NEGATION.search(question):
        return None
    plain = _QUOTED.sub(" ", question)
    matches = list(_SCOPE_SWITCH.finditer(plain))
    if len(matches) != 1:
        return None
    tail = plain[matches[0].end() :].lstrip()
    remainder = tail.lstrip(" ,;，；.。\t\r\n")
    if remainder and remainder not in {"!", "！"}:
        # A future, conditional or interrogative scope change is not permission
        # to move focus now. Only an immediate teaching request may follow it.
        if not re.match(
            r"(?:(?:and|then)\s+(?:please\s+)?(?:explain|describe|verify|locate|show|compare|teach|summarize|read|analy[sz]e)\b|"
            r"(?:并且|并|然后|接着)?\s*(?:请)?(?:解释|讲解|验证|核实|定位|查看|比较|学习|分析|总结|阅读))",
            remainder,
            re.I,
        ):
            return None
        if re.search(
            r"\b(?:after|before|once|until|if|when|unless)\s+(?:I|you|we|approval|confirmation|permission)\b|"
            r"(?:我|你|我们).{0,6}(?:同意|批准|确认)|(?:批准|同意|确认)(?:后|之前)",
            remainder,
            re.I,
        ):
            return None
    # Multiple repository/path targets are ambiguous. This intentionally favors
    # clarification over guessing, even if one of the targets is not accessible.
    identities = re.findall(
        r"(?<![\w/.-])(?:https://github\.com/)?([\w.-]+/[\w.-]+)(?![\w/.-])", plain
    )
    if len({identity.rstrip(".") for identity in identities}) != 1:
        return None
    return matches[0].group("repo").rstrip(".")


def _intent_matches(question: str, policy: dict[str, Any] | None) -> list[tuple[TaskType, str]]:
    matches = [
        (task, term)
        for task, terms in _INTENT_TERMS.items()
        for term in terms
        if term_matches(question, term)
    ]
    if re.search(r"https://github\.com/[\w.-]+/[\w.-]+", question):
        matches.append((TaskType.DISCOVER, "https://github.com/"))
    # A component of a longer explicit phrase is not a second instruction:
    # 学习 in 学习计划, 实现 in 帮我实现, 证据 in 证据错误.
    matches = [
        (task, term)
        for task, term in matches
        if not any(term != other and term in other for _, other in matches)
    ]
    if matches:
        return matches
    return [
        (TaskType(rule["task_type"]), rule["keyword"])
        for rule in (policy or {}).get("keyword_rules", [])
        if term_matches(question, rule["keyword"])
    ]


def _ambiguous_language(
    question: str,
    policy: dict[str, Any] | None = None,
    *,
    routing_contract_version: str | None = ROUTING_CONTRACT_VERSION,
) -> bool:
    if routing_contract_version not in (
        None,
        LEGACY_ROUTING_CONTRACT_VERSION,
        ROUTING_CONTRACT_VERSION,
        LEARNING_ROUTING_CONTRACT,
        SCOPED_QUESTION_ROUTING_CONTRACT,
    ):
        raise DomainError("ROUTING_CONTRACT_VERSION", "未知的任务动作解析合同版本。", 422)
    if routing_contract_version == SCOPED_QUESTION_ROUTING_CONTRACT and _readonly_explanation(
        question, policy
    ):
        return False
    question = _routing_language(question, routing_contract_version)
    if _NEGATION.search(question) or any(
        _intent_matches(match.group(0), policy) for match in _QUOTED.finditer(question)
    ):
        return True
    # Recognize a second imperative even when the rule vocabulary has no task
    # for it. "Verify ..., then edit ..." must not discard the editing request.
    action_text = (
        _editing_action_language(question)
        if routing_contract_version
        in {
            LEGACY_ROUTING_CONTRACT_VERSION,
            ROUTING_CONTRACT_VERSION,
            LEARNING_ROUTING_CONTRACT,
            SCOPED_QUESTION_ROUTING_CONTRACT,
        }
        else question
    )
    clauses = list(_ACTION_CLAUSE.finditer(action_text))
    if len(clauses) > 1:
        return True
    return any(
        _intent_matches(action_text[: match.start()], policy)
        for match in clauses
        if match.start() > 0
    )


def _readonly_explanation(question, policy=None):
    """A factual negation in an explicit explanation is not a negated command.

    This only selects retrieval; a second operation still requires normal routing.
    """
    if not re.match(
        r"^\s*(?:请)?(?:(?:根据|依据|结合)(?:已发布|已审核)(?:的)?(?:知识|资料|证据)(?:来)?)?(?:解释|讲解|说明)|^\s*(?:please\s+)?explain\b",
        question,
        re.I,
    ):
        return False
    if any(_intent_matches(m.group(0), policy) for m in _QUOTED.finditer(question)):
        return False
    plain = _routing_language(question, SCOPED_QUESTION_ROUTING_CONTRACT)
    clauses = list(_ACTION_CLAUSE.finditer(_editing_action_language(plain)))
    if any(
        (m.group("english") or m.group("chinese") or "").lower()
        not in {"解释", "讲解", "explain", "describe", "teach"}
        for m in clauses
    ):
        return False
    tasks = {task for task, _ in _intent_matches(plain, policy)}
    return bool(tasks) and tasks <= {
        TaskType.IMPLEMENTATION,
        TaskType.ARCHITECTURE,
        TaskType.CONCEPT,
    }


def rule_intent(
    question: str,
    focus: dict[str, Any],
    policy: dict[str, Any] | None = None,
    *,
    routing_contract_version: str | None = ROUTING_CONTRACT_VERSION,
) -> IntentDecision | None:
    if routing_contract_version == SCOPED_QUESTION_ROUTING_CONTRACT and _readonly_explanation(
        question, policy
    ):
        tasks = {task for task, _ in _intent_matches(question, policy)}
        task = (
            TaskType.ARCHITECTURE
            if TaskType.ARCHITECTURE in tasks
            else TaskType.CONCEPT
            if TaskType.CONCEPT in tasks
            else TaskType.IMPLEMENTATION
        )
        return IntentDecision(
            interpreted_intent=task,
            proposed_next_action="retrieve_and_tutor",
            confidence=0.95,
            rationale_codes=["explicit_readonly_explanation"],
        )
    if routing_contract_version in {LEARNING_ROUTING_CONTRACT, SCOPED_QUESTION_ROUTING_CONTRACT}:
        inferred = learning_rule_intent(
            question,
            focus,
            scoped_questions=routing_contract_version == SCOPED_QUESTION_ROUTING_CONTRACT,
        )
        if inferred is not None:
            return inferred
    if _ambiguous_language(question, policy, routing_contract_version=routing_contract_version):
        return None
    matches = _intent_matches(_routing_language(question, routing_contract_version), policy)
    tasks = {task for task, _ in matches}
    # "Explain architecture" is one descriptive request, unlike "compare and
    # explain" or "explain X; then make a learning plan".
    if (
        TaskType.IMPLEMENTATION in tasks
        and len(tasks) == 2
        and tasks & {TaskType.ARCHITECTURE, TaskType.CONCEPT, TaskType.PLAN}
        and not _COORDINATION.search(question)
    ):
        tasks.remove(TaskType.IMPLEMENTATION)
    if len(tasks) > 1:
        return None
    task = next(iter(tasks), None)
    if task is None and (focus.get("topic_keys") or focus.get("project_source_ids")):
        # Focus can resolve a genuine continuation; it cannot turn an unrelated
        # or ambiguous question into an implementation request by itself.
        if re.search(r"这个|那个|它|刚才|上述|继续|\b(?:this|that|it|continue)\b", question, re.I):
            task = TaskType.IMPLEMENTATION
    if task is None:
        return None
    action: IntentAction = (
        "knowledge_build"
        if task in {TaskType.IMPORT, TaskType.DISCOVER}
        else "coding"
        if task == TaskType.PRACTICE
        else "direct_tool"
        if task in {TaskType.LOCATE, TaskType.PROGRESS}
        else "record_gap"
        if task == TaskType.DISPUTE
        else "retrieve_and_tutor"
    )
    return IntentDecision(
        interpreted_intent=task,
        proposed_next_action=action,
        confidence=0.9,
        rationale_codes=["unambiguous_rule_or_explicit_continuation"],
    )


def _intent_model_schema() -> dict[str, Any]:
    """Keep the old model-facing schema; scope is exclusively server supplied."""
    schema = IntentDecision.model_json_schema()
    for field in ("retrieval_topic_keys", "topic_hints", "retrieval_scope_version"):
        schema["properties"].pop(field)
    return schema


def _mentioned_sources(
    question: str, names: list[dict[str, Any]], *, comparison_aliases: bool = False
) -> list[str]:
    # Quoted examples are not requests to switch repository. Full repository
    # identities take precedence over possibly ambiguous short names.
    text = _QUOTED.sub(" ", question)
    full = [
        s["id"]
        for s in names
        if re.search(
            r"(?<![\w/.-])(?:https://github\.com/)?" + re.escape(s["name"]) + r"(?![\w/.-])",
            text,
            re.IGNORECASE,
        )
    ]
    if full and not comparison_aliases:
        return full
    aliases = [s["name"].rsplit("/", 1)[-1].casefold() for s in names]
    short = [
        s["id"]
        for s, alias in zip(names, aliases, strict=True)
        if (
            len(alias) >= 4
            if not comparison_aliases
            else len(alias) >= 2 and aliases.count(alias) == 1
        )
        and re.search(
            r"(?<![\w/.-])" + re.escape(s["name"].rsplit("/", 1)[-1]) + r"(?![\w/.-])",
            text,
            re.IGNORECASE,
        )
    ]
    return list(dict.fromkeys([*full, *short]))


def _coordinator_focus(
    focus: dict[str, Any], source_ids: set[str], topic_keys: set[str]
) -> dict[str, Any]:
    """Reference resolution uses the current question and scoped navigation.

    Previous questions and answers can contain a different private repository
    than this turn's funded hard scope. No authoritative history-lineage contract
    exists, so do not relabel raw history as learning_state or silently expand
    the dependency set just to transmit it to the coordinator.
    """
    result = {
        key: focus[key]
        for key in ("retrieval_scope_version", "task_type", "last_turn_id")
        if key in focus
    }
    for key in ("topic_keys", "retrieval_topic_keys", "topic_hints"):
        values = focus.get(key)
        if isinstance(values, list):
            result[key] = [
                value for value in values if isinstance(value, str) and value in topic_keys
            ]
    values = focus.get("project_source_ids")
    if isinstance(values, list):
        result["project_source_ids"] = [
            value for value in values if isinstance(value, str) and value in source_ids
        ]
    navigation = focus.get("code_focus", {})
    if isinstance(navigation, dict) and navigation.get("source_id") in source_ids:
        result["code_focus"] = {
            key: navigation[key]
            for key in ("source_id", "path", "symbol", "start_line", "end_line", "parser")
            if key in navigation
        }
    return result


class Coordinator:
    @staticmethod
    def dialogue_actions(message):
        from .dialogue_routing import explicit_actions

        return explicit_actions(message)

    def __init__(self, db, gateway):
        self.db, self.gateway = db, gateway

    @staticmethod
    def freeze_path_source_ids(db, actor: str, question: str, focus: dict[str, Any]) -> list[str]:
        """Freeze user-derived snapshot IDs before any coordinator proposal.

        A same-path receipt from another otherwise authorized snapshot is not
        evidence for this turn. Unknown/ambiguous scope stays empty and must
        clarify; it is never used as the unrestricted retrieval sentinel.
        """
        request = requested_source_path(question)
        if not request or request["status"] != "exact":
            return []
        focused = list(dict.fromkeys(focus.get("project_source_ids", [])))
        switch = _explicit_scope_switch(question)
        if focused and switch is None:
            return focused if len(focused) == 1 else []
        names = [switch] if switch else []
        if not switch:
            plain = _QUOTED.sub(" ", question)
            for match in re.finditer(
                r"(?<![\w/.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?![\w/.-])", plain
            ):
                name = match.group(1)
                if name == request["path"]:
                    continue  # A path cannot manufacture repository selection.
                prefix = re.split(r"[，,；;。.!?！？\n]", plain[: match.start()])[-1]
                if not _NEGATION.search(prefix) and not re.search(
                    r"\b(?:compare|versus|example|context)\b|比较|对比|例如|提到", prefix, re.I
                ):
                    names.append(name)
        names = list(dict.fromkeys(names))
        if len(names) != 1:
            return []
        scopes = [namespace.id for namespace in accessible_namespaces(db, actor)]
        selected = list(
            db.scalars(
                select(SourceSet.id)
                .join(ObjectRow, ObjectRow.id == SourceSet.id)
                .where(
                    SourceSet.repository_key == names[0],
                    ObjectRow.namespace_id.in_(scopes),
                    ObjectRow.state == "active",
                )
                .limit(2)
            )
        )
        return selected if len(selected) == 1 else []

    async def decide(
        self,
        actor: str,
        question: str,
        focus: dict[str, Any],
        revision: int,
        policies: dict[str, Any],
        run: tuple[str, int, str] | None,
        *,
        routing_contract_version: str | None = ROUTING_CONTRACT_VERSION,
        retrieval_context_contract: str | None = RETRIEVAL_CONTEXT_CONTRACT,
    ) -> tuple[IntentDecision, OrchestrationDecision]:
        if retrieval_context_contract not in {
            None,
            LEGACY_RETRIEVAL_CONTEXT_CONTRACT,
            LEGACY_FACT_RETRIEVAL_CONTEXT_CONTRACT,
            WHOLE_PACK_RETRIEVAL_CONTEXT_CONTRACT,
            RETRIEVAL_CONTEXT_CONTRACT,
        }:
            raise DomainError("RETRIEVAL_CONTEXT_CONTRACT", "未知的检索上下文合同版本。", 422)
        if routing_contract_version not in (
            None,
            LEGACY_ROUTING_CONTRACT_VERSION,
            ROUTING_CONTRACT_VERSION,
            LEARNING_ROUTING_CONTRACT,
            SCOPED_QUESTION_ROUTING_CONTRACT,
        ):
            raise DomainError("ROUTING_CONTRACT_VERSION", "未知的任务动作解析合同版本。", 422)
        hard_topics = list(
            dict.fromkeys(
                focus["retrieval_topic_keys"]
                if focus.get("retrieval_topic_keys") is not None
                else focus.get("topic_keys", [])
            )
        )
        focused = list(dict.fromkeys(focus.get("project_source_ids", [])))
        path_scoped = (
            focus.get("source_path_contract") == SOURCE_PATH_CONTRACT
            and requested_source_path(question) is not None
        )
        path_scope = focus.get("source_path_source_ids")
        invalid_path_scope = bool(
            path_scoped
            and (
                not isinstance(path_scope, list)
                or len(path_scope) != 1
                or not all(isinstance(identifier, str) for identifier in path_scope)
            )
        )
        if path_scoped:
            focused = (
                list(path_scope) if isinstance(path_scope, list) and not invalid_path_scope else []
            )
        if len(hard_topics) > 10 or len(focused) > 10:
            raise DomainError("FOCUS_SCOPE", "当前检索焦点超过合同范围；请缩小显式选择。", 422)
        invalid_focus = False
        switch_target = _explicit_scope_switch(question)
        switched = False
        with self.db.read() as db:
            scopes = [n.id for n in accessible_namespaces(db, actor)]
            known_sources = db.scalars(
                select(SourceSet)
                .join(ObjectRow, ObjectRow.id == SourceSet.id)
                .where(ObjectRow.namespace_id.in_(scopes), ObjectRow.state == "active")
                .order_by(SourceSet.id)
                .limit(101)
            ).all()
            sources_truncated = len(known_sources) > 100
            known_sources = list(known_sources[:100])
            # A bounded catalog must not erase explicitly selected current focus.
            focused_sources = db.scalars(
                select(SourceSet)
                .join(ObjectRow, ObjectRow.id == SourceSet.id)
                .where(
                    SourceSet.id.in_(focused),
                    ObjectRow.namespace_id.in_(scopes),
                    ObjectRow.state == "active",
                )
            ).all()
            switch_sources = (
                db.scalars(
                    select(SourceSet)
                    .join(ObjectRow, ObjectRow.id == SourceSet.id)
                    .where(
                        SourceSet.repository_key == switch_target,
                        ObjectRow.namespace_id.in_(scopes),
                        ObjectRow.state == "active",
                    )
                ).all()
                if switch_target
                else []
            )
            # More than one snapshot for the same repository is not a unique
            # version choice. Leave focus unchanged and require clarification.
            if len(switch_sources) == 1:
                focused = [switch_sources[0].id]
                focus = {**focus, "project_source_ids": focused}
                switched = True
            known_sources = list(
                {s.id: s for s in [*known_sources, *focused_sources, *switch_sources]}.values()
            )
            source_ids = {s.id for s in known_sources}
            names = [
                {"id": s.id, "name": s.repository_key, "commit": s.commit_sha}
                for s in known_sources
            ]
            topics = resolve_topics(self.db, question, actor=actor)
            valid_topics = {topic.key for topic in visible_topics(db, actor)}
            invalid_focus = (
                invalid_path_scope
                or not set(focused) <= source_ids
                or not set(hard_topics) <= valid_topics
            )
        policy = policies["routing_policy"]
        decision = rule_intent(
            question,
            focus,
            policy["content"],
            routing_contract_version=routing_contract_version,
        )
        fallback_reason = "insufficient_context_and_no_model_budget"
        if decision is None and run and not invalid_focus:
            # Never include another repository merely because it is visible in
            # the account when the user supplied a narrower project scope.
            model_names = [s for s in names if not focused or s["id"] in focused]
            model_topics = list(dict.fromkeys([*hard_topics, *topics]))
            with self.db.read() as db:
                dependencies = sorted(
                    {s["id"] for s in model_names}
                    | {
                        topic_object_id(key, require_topic(db, actor, key).version)
                        for key in model_topics
                    }
                )
                includes_private_sources = check_closure(
                    db, actor, dependencies
                ).private_source_content
            try:
                call_classes, call_dependencies = self.gateway.policy_egress_inputs(
                    run[0],
                    "coordinator",
                    data_classes=[
                        "learning_state",
                        *(["private_source"] if includes_private_sources else []),
                    ],
                    access_dependencies=dependencies,
                )
                reply = await self.gateway.call(
                    *run,
                    "intent",
                    "coordinator",
                    "识别用户学习任务并解析上下文指代。只选择已提供的项目ID与技术Topic；不授予权限或预算。"
                    "用户无需选择按项目或技术模式。信息不足时要求澄清；未知项目应提出knowledge_build。",
                    {
                        "question": question,
                        "focus": _coordinator_focus(
                            focus, {s["id"] for s in model_names}, set(model_topics)
                        ),
                        "authorized_sources": model_names,
                        "authorized_sources_truncated": sources_truncated,
                        "matched_topics": topics,
                        "routing_policy": policy["content"],
                    },
                    IntentDecision,
                    output_schema=_intent_model_schema(),
                    data_classes=call_classes,
                    access_dependencies=call_dependencies,
                )
                decision = IntentDecision.model_validate(reply.structured)
            except DomainError as exc:
                if exc.code not in {
                    "EGRESS_OR_ROLE_DENIED",
                    "FUNDING_SOURCE_SCOPE",
                    "MODEL_BUDGET",
                    "REPLAY_ARM_BUDGET",
                }:
                    raise
                # No permission/extra budget is invented to resolve ambiguity.
                # Replay-changed/uncertain/lease errors still fail closed.
                fallback_reason = "coordinator_unavailable_" + exc.code.lower()
        if decision is None:
            decision = IntentDecision(
                interpreted_intent=TaskType.OTHER,
                confidence=0.0,
                reference_resolution="missing",
                proposed_next_action="ask_clarification",
                rationale_codes=[fallback_reason],
            )
        explicit_sources = (
            []
            if _ambiguous_language(
                question, policy["content"], routing_contract_version=routing_contract_version
            )
            else _mentioned_sources(
                question,
                names,
                comparison_aliases=(
                    focus.get("comparison_gate")
                    in {"evidence-paired-v1", *PAIRED_COMPARISON_CONTRACTS}
                    and decision.interpreted_intent == TaskType.COMPARE
                ),
            )
        )
        # A repository URL names a repository, not every stored snapshot of it.
        # Preserve an already selected version unless the user asks to switch,
        # compare versions or supplies an explicit version identifier.
        if (len(focused) == 1 and focused[0] in explicit_sources
                and not switch_target and decision.interpreted_intent != TaskType.COMPARE
                and not re.search(r"版本|快照|切换|commit|snapshot|version|branch|tag|[0-9a-f]{7,40}", question, re.I)):
            repositories = {s["name"].casefold() for s in names if s["id"] in explicit_sources}
            if len(repositories) == 1:
                explicit_sources = list(focused)
        proposed = list(dict.fromkeys(decision.project_source_ids))
        selected = list(focused)
        if focused:
            # An incompatible proposal cannot turn a restrictive list into []
            # (which means all authorized sources to retrieval).
            requested = explicit_sources or proposed
            if requested and not set(requested) <= set(focused):
                decision = decision.model_copy(update={"reference_resolution": "ambiguous"})
            elif requested:
                selected = requested
        else:
            selected = explicit_sources or proposed
        if not selected and decision.interpreted_intent == TaskType.LOCATE:
            last_code = focus.get("code_focus", {})
            if last_code.get("source_id") in source_ids:
                selected = [last_code["source_id"]]
        if len(selected) > 1 and decision.interpreted_intent != TaskType.COMPARE:
            if explicit_sources or any(x in question for x in ["这个函数", "它的", "这个项目"]):
                decision = decision.model_copy(update={"reference_resolution": "ambiguous"})
        if (switch_target and not switched) or invalid_focus or not set(selected) <= source_ids:
            decision = decision.model_copy(update={"reference_resolution": "missing"})
            # Preserve the rejected hard constraint for audit, never retrieve on it.
            selected = focused or selected
        proposed_topics = list(dict.fromkeys([*decision.topic_keys, *decision.topic_hints]))
        if not set(proposed_topics) <= valid_topics:
            decision = decision.model_copy(update={"reference_resolution": "missing"})
        # Keep the user's source and hard topic constraints, but a newly stated
        # question is not a request to append all previous inferred subjects.
        # Unmarked durable turns retain their original receipt inputs.
        inherited_hints = (
            focus.get("topic_hints", [])
            if retrieval_context_contract is None
            or (not switched and is_topic_continuation(question))
            else []
        )
        hints = list(
            dict.fromkeys(
                key for key in [*proposed_topics, *topics, *inherited_hints] if key in valid_topics
            )
        )[:10]
        keys = hard_topics or hints
        decision = decision.model_copy(
            update={
                "topic_keys": keys,
                "retrieval_topic_keys": hard_topics,
                "topic_hints": hints,
                "retrieval_scope_version": "explicit_scope_v2",
                "project_source_ids": selected,
                "rationale_codes": (
                    [*decision.rationale_codes[:4], "explicit_source_focus_change"]
                    if switched
                    else decision.rationale_codes
                ),
            }
        )
        task = decision.interpreted_intent
        action: CoordinatorAction
        if (
            decision.reference_resolution != "resolved"
            or decision.confidence < policy["content"]["ambiguity_threshold"]
            or decision.proposed_next_action == "ask_clarification"
            or task == TaskType.OTHER
        ):
            action, code = "ask_clarification", "reference_or_intent_ambiguous"
        elif task in {TaskType.IMPORT, TaskType.DISCOVER}:
            action, code = "start_background_job", "new_knowledge_requires_authorized_build"
        elif task == TaskType.PRACTICE:
            action, code = "invoke_coding", "practice_requires_workspace_approval"
        elif task == TaskType.DISPUTE:
            action, code = "return_evidence_gap", "dispute_not_memory_rewrite"
        elif task in {TaskType.LOCATE, TaskType.PROGRESS}:
            action, code = "direct_tool", "deterministic_operation"
        else:
            action, code = "invoke_tutor", "teaching_requires_evidence"
        return decision, OrchestrationDecision(
            action=action,
            trigger_code=code,
            principal_id=actor,
            authorized_namespace_refs=scopes,
            focus_revision=revision,
            task_type=task,
            source_version_refs=selected,
            topic_keys=keys,
            policy_version=policy["policy_id"],
            task_contract_ref=run[0] if run else None,
        )
