"""Lossless, versioned model-only de-duplication of immutable planner metadata.

The authorization snapshot and authoritative text catalog remain in the database.
This projection changes only new model requests, never existing paid receipts.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from itertools import zip_longest
from typing import Any

from ..errors import DomainError
from ..util import canonical, fingerprint
from .consumer_context import validate_context_contract
from .learning_projection import text_catalog

PLANNER_CONTEXT_VERSION = "learning-planner-context-1.6.6"
BOUNDED_PLANNER_CONTEXT_VERSION = "learning-planner-context-bounded-v1"
CONTEXT_ONCE_PLANNER_CONTEXT_VERSION = "learning-planner-context-bounded-once-v2"
BOUNDED_PLANNER_CONTEXT_VERSIONS = {
    BOUNDED_PLANNER_CONTEXT_VERSION,
    CONTEXT_ONCE_PLANNER_CONTEXT_VERSION,
}
# Preserve bounded-v1's historical doubled-context allowance. The new once-v2
# budgets only its escaped message, retaining the same reserve for rules, schema
# and dependencies. The gateway still checks the complete 250,000-byte envelope.
PLANNER_CONTEXT_WIRE_BUDGET = 210_000
PLANNER_CONTEXT_RULES = """
模型输入使用无损去重目录。text_catalog的text_id和text仍是原始逐字引用，不要改变它们。
条目的scope_ref指向scope_catalog中的完整来源/目标元数据；group_path_refs按原顺序引用
 group_catalog中的组。组的operator、member_ids、quote和来源位置必须保留原义：
OR/任选不是全部必需，preferred/bonus不是required。没有深度要求不能补造深度。
这些目录只是相同不可变数据的去重表示，不新增事实、权限、运行结果或用户能力证据。
"""


def projection_rules(frozen: dict[str, Any]) -> str:
    version = frozen.get("planner_model_context_version")
    if version is None:
        return ""
    if version not in {PLANNER_CONTEXT_VERSION, *BOUNDED_PLANNER_CONTEXT_VERSIONS}:
        raise DomainError("PLAN_MODEL_CONTEXT_VERSION", "未知学习规划模型输入投影版本。", 422)
    marker = frozen.get("consumer_context_contract")
    validate_context_contract(marker)
    rules = PLANNER_CONTEXT_RULES + (
        "\n本请求使用显式独立事实合同。focus_fact_ids及事实引用只能选择各发布consumer_context中"
        "standalone=true的事实；false或缺少独立审核的事实只能保留原整单元语境，不可拆成学习目标。"
        "text_catalog已排除这些事实；不要补写指代、借另一单元作前提或推测依赖，应保留缺口。\n"
        if marker
        else ""
    )
    if version in BOUNDED_PLANNER_CONTEXT_VERSIONS:
        rules += (
            "\n发布的facts只列出本次可选事实；text_refs指向目录中的逐字陈述与全部条件。"
            "完整JD及限定继续保留。omitted_standalone_facts仅表示本次未提供的已审核事实数，"
            "不能据此推断内容或证明目标。只选择本次可见的发布、事实和文本引用。\n"
        )
    return rules


def planner_model_context(frozen, step_catalog, rubric_catalog):
    """Keep every text, fact and scope value, sharing only duplicate metadata."""
    projection_rules(frozen)
    catalog = list(text_catalog(frozen).values())
    if frozen.get("planner_model_context_version") is None:
        # Exact historical request shape/bytes, including original full metadata.
        return {
            "frozen_input": frozen,
            "step_catalog": step_catalog,
            "rubric_catalog": rubric_catalog,
            "text_catalog": catalog,
        }

    groups = {}
    scopes = {}

    def compact(value):
        if isinstance(value, list):
            return [compact(item) for item in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {}
        for key, item in value.items():
            if key == "group_path" and isinstance(item, list):
                references = []
                for group in item:
                    identifier = "group." + fingerprint(group)
                    groups[identifier] = deepcopy(group)
                    references.append(identifier)
                result["group_path_refs"] = references
            else:
                result[key] = compact(item)
        return result

    bounded = frozen.get("planner_model_context_version") in BOUNDED_PLANNER_CONTEXT_VERSIONS
    model_input = compact(
        {key: value for key, value in frozen.items() if key not in {"units", "base_preferences"}}
        if bounded
        else frozen
    )
    projected = []
    # Small authority/selection fields stay beside their text. Bulky repeated
    # provenance is shared. Nothing is stripped from the scope, only moved.
    local = {
        "text_id",
        "text",
        "kind",
        "publication_id",
        "fact_atom_id",
        "goal_version_id",
        "requirement_id",
        "requirement_key",
        "required_depth",
        "modality",
        "group_operator",
        "group_id",
    }
    for original in catalog:
        entry = compact(original)
        scope = {key: item for key, item in entry.items() if key not in local}
        item = {key: value for key, value in entry.items() if key in local}
        if scope:
            identifier = "scope." + fingerprint(scope)
            scopes[identifier] = scope
            item["scope_ref"] = identifier
        projected.append(item)
    context = {
        "planner_model_context_version": frozen["planner_model_context_version"],
        "frozen_input": model_input,
        "step_catalog": step_catalog,
        "rubric_catalog": rubric_catalog,
        "text_catalog": projected,
        "scope_catalog": {key: scopes[key] for key in sorted(scopes)},
        "group_catalog": {key: groups[key] for key in sorted(groups)},
    }

    if bounded:
        _budget_evidence(context, frozen)
    return context


def _wire_size(context):
    serialized = canonical(context)
    escaped_size = len(canonical(serialized).encode())
    if context.get("planner_model_context_version") == CONTEXT_ONCE_PLANNER_CONTEXT_VERSION:
        return escaped_size
    return len(serialized.encode()) + escaped_size


def _budget_evidence(context, frozen):
    text_by_fact = defaultdict(list)
    catalog = context["text_catalog"]
    context["text_catalog"] = []
    for entry in catalog:
        if "fact_atom_id" in entry:
            text_by_fact[(entry["publication_id"], entry["fact_atom_id"])].append(entry)
        else:
            context["text_catalog"].append(entry)

    goal_requirements = (frozen.get("goal") or {}).get("requirements")
    goal_requirements = goal_requirements if isinstance(goal_requirements, dict) else {}
    requirement_topics = {
        link["topic_key"]
        for row in goal_requirements.get("topic_links", [])
        if row.get("status") == "mapped"
        for link in row["topics"]
    }
    units = context["frozen_input"]["units"] = []
    by_source = defaultdict(list)
    for pack in sorted(
        frozen["units"],
        key=lambda row: (
            -len(requirement_topics & {link["topic_key"] for link in row["topic_links"]}),
            row["publication_id"],
        ),
    ):
        unit = {
            key: pack[key]
            for key in ("publication_id", "source_id", "repository", "commit", "title")
        }
        unit["topic_keys"] = [link["topic_key"] for link in pack["topic_links"]]
        unit["facts"] = []
        facts = [
            fact["fact_atom_id"]
            for fact in pack["facts"]
            if (unit["publication_id"], fact["fact_atom_id"]) in text_by_fact
        ]
        unit["omitted_standalone_facts"] = len(facts)
        units.append(unit)
        by_source[pack["source_id"]].append([(unit, fact) for fact in facts])

    # Preserve the original goal, all qualifiers and every selected publication's
    # identity before spending any remaining bytes on whole facts and conditions.
    if _wire_size(context) > PLANNER_CONTEXT_WIRE_BUDGET:
        raise DomainError(
            "PLAN_GOAL_CONTEXT_LIMIT", "完整学习目标与来源清单超过规划输入预算。", 413
        )

    per_source = [
        (item for row in zip_longest(*queues) for item in row if item is not None)
        for queues in by_source.values()
    ]
    for turn in zip_longest(*per_source):
        for item in turn:
            if item is None:
                continue
            unit, fact_id = item
            entries = text_by_fact[(unit["publication_id"], fact_id)]
            unit["facts"].append(
                {"fact_atom_id": fact_id, "text_refs": [entry["text_id"] for entry in entries]}
            )
            context["text_catalog"].extend(entries)
            unit["omitted_standalone_facts"] -= 1
            if _wire_size(context) > PLANNER_CONTEXT_WIRE_BUDGET:
                unit["facts"].pop()
                del context["text_catalog"][-len(entries) :]
                unit["omitted_standalone_facts"] += 1

    if not any(unit["facts"] for unit in units):
        raise DomainError(
            "PLAN_EVIDENCE_CONTEXT_LIMIT", "规划输入预算无法提供可独立使用的完整已审核事实。", 413
        )


def require_visible_choices(draft, context):
    if context.get("planner_model_context_version") not in BOUNDED_PLANNER_CONTEXT_VERSIONS:
        return
    visible = {
        unit["publication_id"]: {row["fact_atom_id"] for row in unit["facts"]}
        for unit in context["frozen_input"]["units"]
    }
    text_ids = {entry["text_id"] for entry in context["text_catalog"]}
    for module in draft.modules:
        if not set(module.focus_fact_ids) <= visible.get(module.publication_id, set()):
            raise DomainError("PLAN_EVIDENCE", "计划选择了本次未提供的已审核事实。", 422)

    def check_references(value):
        if isinstance(value, dict):
            if "text_id" in value and value["text_id"] not in text_ids:
                raise DomainError("PLAN_TEXT_REFERENCE", "计划引用了本次未提供的文本。", 422)
            for child in value.values():
                check_references(child)
        elif isinstance(value, list):
            for child in value:
                check_references(child)

    check_references(draft.model_dump(mode="json"))
