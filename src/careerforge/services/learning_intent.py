"""Conservative, versioned interpretation of learning requests.

This is a bounded language router, not a model-quality evaluator. Ambiguous or
unsupported actions still use the existing coordinator/clarification path.
"""

from __future__ import annotations

import re
from typing import Any

from ..contracts import IntentDecision, TaskType

LEARNING_ROUTING_CONTRACT = "intent-actions-1.6.6"
SCOPED_QUESTION_ROUTING_CONTRACT = "intent-scoped-learning-questions-v1"
SCOPED_COMPARISON_CONTRACT = "evidence-paired-with-diagnostics-v3"
MULTIVALUE_COMPARISON_CONTRACT = "evidence-paired-with-diagnostics-v4"
PAIRED_COMPARISON_CONTRACTS = {SCOPED_COMPARISON_CONTRACT, MULTIVALUE_COMPARISON_CONTRACT}
_QUOTED = re.compile(
    r'```[\s\S]*?```|`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|^\s*>[^\n]*', re.M
)
_NEGATED = re.compile(
    r"(?:不要|不用|无需|无须|别|不是要|并非|不想|不需要|not\b|never\b|don't\b|do not\b)", re.I
)
_ACTIONS = {
    "practice": r"练习|出题|测验|测试|quiz|practice|test",
    "plan": r"计划|路线|plan",
    "verify": r"验证|核验|verify",
    "project_overview": r"完整项目|整个项目|完整仓库|whole project|entire repository",
    "source_code": r"源码|代码|source code",
}


def inaccessible_knowledge_request(question: str) -> bool:
    plain = _QUOTED.sub(" ", question)
    return bool(
        re.search(r"另一个用户|其他用户|别人的|他人|another user|someone else's", plain, re.I)
        and re.search(r"私有|私人|无权|没有权限|private|without (?:access|permission)", plain, re.I)
        and re.search(r"访问|给我|内容|读取|查看|access|show|read|retrieve", plain, re.I)
    )


def module_guidance_request(question: str) -> bool:
    """Recognize a continuation command without changing an explicit question's scope."""
    text = question.strip().rstrip("。！!？?")
    return bool(
        re.fullmatch(
            r"(?:请|麻烦)?(?:开始学习|开始本模块|带我学习当前模块|"
            r"(?:继续|接着)(?:讲解?|学习)?(?:一下)?(?:当前|本)(?:学习)?模块|"
            r"(?:换一种|换个)(?:方式|讲法)(?:继续讲解?|接着讲解?))|"
            r"(?:please )?(?:start|continue)(?: with)? (?:this|the current) (?:learning )?module",
            text,
            re.IGNORECASE,
        )
    )


def learning_intent(
    question: str, focus: dict[str, Any], *, scoped_questions: bool = False
) -> dict[str, Any]:
    plain = _QUOTED.sub(" ", question)
    plain = re.sub(
        r"(?<![\w./-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+(?![\w./-])",
        " ",
        plain,
    )
    # Inline identifiers are data but may legitimately name the current symbol.
    clauses = [
        part.strip()
        for part in re.split(r"[，,。；;!?！？\n]|\bthen\b|然后", plain)
        if part.strip()
    ]
    positives: list[str] = []
    negated: list[str] = []
    deferred: list[str] = []
    for clause in clauses:
        if _NEGATED.search(clause):
            negated.extend(
                key for key, pattern in _ACTIONS.items() if re.search(pattern, clause, re.I)
            )
            # A negative clause is never inverted into permission to act.
            continue
        if re.search(r"下次|下一次|下一轮|以后|之后|再说|\bnext time\b|\blater\b", clause, re.I):
            deferred.append(clause)
        else:
            positives.append(clause)
    active = " ".join(positives)
    selected = focus.get("project_source_ids") or focus.get("active_source_ids") or []
    topics = focus.get("topic_hints") or focus.get("topic_keys") or []
    code = focus.get("code_focus")
    has_context = bool(selected or topics or code)
    result: dict[str, Any] = {
        "contract_version": LEARNING_ROUTING_CONTRACT,
        "intent": None,
        "task_type": None,
        "retrieval_route": None,
        "focus_resolved": False,
        "negated_actions": sorted(set(negated)),
        "deferred_actions": ["compare_projects"]
        if any(re.search(r"其他项目|比较|对比|compare", x, re.I) for x in deferred)
        else [],
        "requires_clarification": True,
        "confidence_or_uncertainty": "unsupported_or_missing_referent",
        "user_mastery_unchanged": True,
        "forces_learning_mode": False,
    }
    if not active:
        return result
    if scoped_questions and re.search(
        r"另一个用户|其他用户|他人私有|没有权限访问|another user", plain, re.I
    ):
        return result
    if re.search(
        r"学习计划|学习路线|导入|调研|搜索项目|任务进度|证据错误|帮我实现|写代码|修改代码|编写|删除|发布|部署|运行测试|执行命令|\b(?:verify|practice|edit|delete|deploy|publish|execute)\b",
        active,
        re.I,
    ):
        return result
    # Do not discard an executable command hidden behind a teaching clause.
    if re.search(
        r"(?:然后|再|并且|\band\b).*(?:修改|删除|发布|部署|执行|运行测试|edit\b|delete\b|deploy\b|run tests)",
        plain,
        re.I,
    ):
        return result
    if re.search(r"(?:在哪|在哪里|定位|where\s+is|locate\b)", active, re.I):
        intent, task, route = "function", TaskType.LOCATE, "function_context"
    elif re.search(
        r"环境准备|安装并搭建|安装|搭建|跑起来|setup|set up|install", active, re.I
    ) and re.search(r"带我|学|教|how|teach|learn|guide", active, re.I):
        intent, task, route = "build_project", TaskType.ARCHITECTURE, "project_context"
    elif re.search(
        r"比较|对比|不同.*框架|不同.*项目|跨项目|compare\b|across.*framework", active, re.I
    ):
        intent, task, route = "technical_design", TaskType.COMPARE, "technology_cross_project"
    elif re.search(
        r"架构|模块|仓库组织|整体结构|先看图|architecture|repository structure", active, re.I
    ):
        intent, task, route = "learn_project", TaskType.ARCHITECTURE, "project_context"
    elif re.search(
        r"函数|代码|源码|返回|异常处理|核心函数|逐行|function|source code|return", active, re.I
    ):
        intent, task, route = "function", TaskType.IMPLEMENTATION, "function_context"
    elif re.search(r"通用概念|什么是|概念|concept|what is", active, re.I):
        intent, task, route = "technical_design", TaskType.CONCEPT, "technology_cross_project"
    elif (
        scoped_questions
        and len(selected) == 1
        and bool(topics)
        and len(positives) == 1
        and not negated
        and not deferred
        and not re.search(
            r"请帮|帮我|给我|告诉我密码|执行以下|\b(?:please run|please execute)\b", active, re.I
        )
        and re.search(
            r"如何|怎样|怎么|什么时候|为何|为什么|是否|会被|会不会|哪些|还会.*吗|\b(?:how|why|when|which)\b",
            active,
            re.I,
        )
    ):
        intent, task, route = "function", TaskType.IMPLEMENTATION, "function_context"
    elif has_context and re.search(r"这个|刚才|继续|原来顺序|\bthis\b|continue", active, re.I):
        intent, task, route = "function", TaskType.IMPLEMENTATION, "function_context"
    else:
        return result
    if route != "technology_cross_project" and not has_context:
        return result
    result.update(
        intent=intent,
        task_type=task.value,
        retrieval_route=route,
        focus_resolved=True,
        requires_clarification=False,
        confidence_or_uncertainty="bounded_learning_request",
    )
    return result


def learning_rule_intent(
    question: str, focus: dict[str, Any], *, scoped_questions: bool = False
) -> IntentDecision | None:
    result = learning_intent(question, focus, scoped_questions=scoped_questions)
    if result["requires_clarification"]:
        return None
    return IntentDecision(
        interpreted_intent=TaskType(result["task_type"]),
        proposed_next_action="direct_tool"
        if result["task_type"] == TaskType.LOCATE.value
        else "retrieve_and_tutor",
        confidence=0.9,
        rationale_codes=["bounded_learning_request", result["intent"], result["retrieval_route"]],
    )
