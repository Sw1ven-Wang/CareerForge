"""Bounded learning presentation built from authorized text, never model prose.

The planner selects exact excerpts and closed operations. Excerpts remain quoted
source/request data; hypothetical scenarios and acceptance checks never claim a
runtime result, source capability, or learner competence. Historical free-form
missing-goal strings can only select matching *user goal* excerpts, not become
user-visible instructions or facts.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
import unicodedata
from copy import deepcopy
from typing import Any
from urllib.parse import unquote

from ..contracts_jd_v166 import REQUIREMENT_VERSION
from ..contracts_learning_ext import (
    PersonalizedPlanDraft,
    PlanAcceptanceDraft,
    PlanGoalGapDraft,
    PlanModuleDraft,
    PlanTaskDraft,
    PlanTextReference,
)
from ..contracts_learning_plan_v162 import (
    PLAN_CONTRACT_VERSION,
    PersonalizedPlanDraftV162,
    PlanModuleDraftV162,
    PlanTaskV162,
)
from ..contracts_learning_scope_v166 import (
    QUESTION_PLAN_CONTRACT,
    QuestionModuleDraft,
    QuestionPlanDraft,
)
from ..errors import DomainError
from ..util import fingerprint
from .consumer_context import (
    pack_context_contract,
    require_frozen_standalone,
    standalone_fact_ids,
    validate_context_contract,
)
from .research_coverage import iter_requirement_items, requirement_identity

PLAN_SCHEMA = "learning-plan-projection-1.6.1"
MODULE_SCHEMA = "learning-module-1.6.1"
ACTIVITY_SCHEMA = "learning-activity-1.6.1"
TASK_SCHEMA = "learning-task-1.6.1"
GOAL_KINDS = {"plan_title", "self_reported_context", "goal_text", "goal_requirement"}
EVIDENCE_KINDS = {"fact_text", "fact_condition", "unit_limitation"}
TEXT_CATALOG_VERSION = "learning-text-catalog-1.6.3"
GOAL_CATALOG_VERSION = "learning-text-catalog-goal-amendment-v1"
QUOTE_PROJECTION_VERSION = "verified-evidence-quote-1.6.5"
JD_SCOPE_FIELDS = (
    "requirement_contract_version",
    "modality",
    "group_operator",
    "group_id",
    "group_path",
    "paragraph_id",
    "span_start",
    "span_end",
    "jd_revision_id",
    "raw_sha256",
    "paragraph_sha256",
    "user_mastery_unchanged",
)

# Exact quotation is the authority boundary. These additional presentation checks
# prevent control characters, active markup, and common instruction spoofing in
# even source-anchored excerpts. They are not a semantic correctness classifier.
_UNSAFE = re.compile(
    r"(?:https?://|javascript:|data:text/|<\s*/?\s*(?:script|iframe|system|developer)|"
    r"\[\s*(?:system|developer|assistant)\s*\]|"
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|"
    r"treat\s+.{0,60}\s+as\s+(?:a\s+)?system\s+message|"
    r"忽略.{0,12}(?:指令|要求)|(?:绕过|跳过|关闭).{0,12}(?:权限|审批|安全))",
    re.I,
)


def _display_safe(value: str) -> bool:
    return (
        bool(value.strip())
        and not _UNSAFE.search(value)
        and all(c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"} for c in value)
    )


def quote_projection_version(frozen: dict[str, Any]) -> str | None:
    """Only the server's frozen input selects policy; old receipts keep old bytes."""
    version = frozen.get("quote_projection_version")
    if version is not None and version != QUOTE_PROJECTION_VERSION:
        raise DomainError("PLAN_QUOTE_POLICY", "未知的学习引文显示合同版本。", 422)
    return version


_ENCODED_CHARACTER = re.compile(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2}))")
_BASE64_TEXT = re.compile(r"(?<![\w+/=-])[A-Za-z0-9+/_-]{24,2048}={0,2}(?![\w+/=-])")
_SECRET_LIKE = re.compile(
    r"(?:-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|AKIA[A-Z0-9]{16})\b|"
    r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}|"
    r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*|"
    r"(?:password|passwd|api[_ -]?(?:key|token)|client[_ -]?secret|access[_ -]?token|"
    r"refresh[_ -]?token|session[_ -]?token|secret(?:[_ -]?key)?|authorization)"
    r"\s*[:=]\s*[\"']?[^\s,;\"'<>]{6,})",
    re.I,
)
_COMPACT_INSTRUCTION = re.compile(
    r"(?:ignore|disregard|override)(?:all)?(?:previous|prior|above|system|developer)"
    r"(?:instructions|messages|rules)|"
    r"treat.{0,60}as(?:a)?(?:system|developer)message|"
    r"(?:reveal|print|send|upload|exfiltrate|disclose).{0,24}"
    r"(?:secret|password|apikey|accesstoken|systemprompt)|"
    r"(?:泄露|透露|输出|发送|上传).{0,12}(?:密码|密钥|令牌|系统提示)|"
    r"(?:https?://|javascript:|data:text/)|"
    r"<[/!]?(?:script|iframe|system|developer|img|svg)|"
    r"<(?:imstart|imend|startheaderid|endheaderid|eotid)>|<<sys>>|"
    r"\[/?(?:system|developer|assistant|inst)\]|"
    r"(?:system|developer|assistant)(?:message|instructions?)?:|"
    r"(?:忽略|覆盖).{0,12}(?:指令|要求)",
    re.I,
)
_ROLE_LINE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:system|developer|assistant)(?:\s+(?:message|instructions?))?\s*:?\s*$",
    re.I | re.M,
)
_SAFETY_ACTION = re.compile(
    r"(?=(?:绕过|跳过|关闭).{0,12}(?:权限|审批|安全)|"
    r"(?:bypass|disable|skip).{0,24}(?:permissions?|approvals?|safety|security))"
)
_WARNING_PREFIX = re.compile(
    r"(?:被|不可|不能|不得|不应|不允许|不要|禁止|切勿|\bnot|\bnever|\bcannot|\bbe)$"
)


def _inspection_forms(value: str) -> list[str] | None:
    """Bounded detection only. Never decode or normalize the displayed quotation."""
    if len(value) > 16000:
        return None
    forms: list[str] = []
    pending = [(value, 0)]
    seen = {value}
    while pending:
        value, depth = pending.pop(0)
        forms.append(value)
        decoded = unicodedata.normalize("NFKC", html.unescape(unquote(value)))
        candidates = [
            _ENCODED_CHARACTER.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), decoded)
        ]
        # Inspect common base64 wrappers too; opaque/binary material is never executed.
        for token in _BASE64_TEXT.findall(value):
            try:
                candidates.append(
                    base64.b64decode(
                        token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
                    ).decode("utf-8")
                )
            except (ValueError, binascii.Error, UnicodeError):
                continue
        for candidate in candidates:
            if candidate and candidate not in seen:
                if depth >= 4 or len(seen) >= 32:
                    return None  # Do not accept an unbounded encoding tunnel.
                seen.add(candidate)
                pending.append((candidate, depth + 1))
    return forms


def _quote_safe(value: str, *, evidence: bool) -> bool:
    """Defense in depth for *verified data*, not a semantic trust classifier.

    The old blanket safety-action regex conflates passive/negative warnings with
    commands. Only source evidence may retain those warnings. Every other old
    denial remains; new plans also check common encoded/obfuscated instructions
    and secret-like values. Authority still comes from current source/goal checks.
    """
    forms = _inspection_forms(value)
    if not value.strip() or forms is None:
        return False
    for form in forms:
        if any(c not in "\n\t" and unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in form):
            return False
        if _SECRET_LIKE.search(form) or _ROLE_LINE.search(form):
            return False
        # Remove punctuation between words/letters without removing role/URL
        # delimiters. This view is used only for rejecting text, never matching it.
        folded = form.casefold()
        offsets = [i for i, c in enumerate(folded) if c.isalnum() or c in "<>[]/!:"]
        compact = "".join(folded[i] for i in offsets)
        if _COMPACT_INSTRUCTION.search(compact):
            return False
        for match in _UNSAFE.finditer(form):
            if not evidence or not match.group().startswith(("绕过", "跳过", "关闭")):
                return False
        # Lookahead checks EVERY action, including one hidden inside an earlier
        # broad match. A safe warning prefix cannot bless the rest of a sentence.
        for match in _SAFETY_ACTION.finditer(compact):
            prefix = folded[: offsets[match.start()]].rstrip()
            if not evidence or not _WARNING_PREFIX.search(prefix):
                return False
    return True


def _reference_safe(entry: dict[str, Any], quote: str, policy: str | None) -> bool:
    if policy is None:
        return _display_safe(quote)
    evidence = entry["kind"] in EVIDENCE_KINDS
    # Inspect both the selected slice and its complete catalog entry: selecting
    # only a token suffix must not strip its credential or instruction context.
    return all(_quote_safe(value, evidence=evidence) for value in (quote, entry["text"]))


def display_reference(reference: dict[str, Any]) -> str:
    """Keep new excerpts visibly separate from closed server instructions.

    JSON string quoting escapes quotes and newlines. API/CLI consumers receive
    plain text, never markup or a new system/developer message. Old projections
    retain their exact rendering for immutable manifest/receipt validation.
    """
    if reference.get("origin") == "user_learning_objective":
        return "用户新增学习目标（非岗位要求、非证据）：" + json.dumps(
            reference["quote"], ensure_ascii=False
        )
    if reference.get("quote_projection_version") == QUOTE_PROJECTION_VERSION:
        return "资料引文（数据，非指令）：" + json.dumps(reference["quote"], ensure_ascii=False)
    return reference["quote"]


def text_catalog(frozen: dict[str, Any]) -> dict[str, dict[str, Any]]:
    quote_projection_version(frozen)
    version = frozen.get("text_catalog_version")
    if version not in {None, TEXT_CATALOG_VERSION, GOAL_CATALOG_VERSION}:
        raise DomainError("PLAN_TEXT_CATALOG_VERSION", "未知的学习目标文字合同版本。", 422)
    catalog: dict[str, dict[str, Any]] = {}

    def add(identifier: str, text: Any, kind: str, **scope: Any) -> None:
        if isinstance(text, str) and text.strip():
            catalog[identifier] = {"text_id": identifier, "text": text, "kind": kind, **scope}

    add("request.title", frozen.get("title"), "plan_title")
    add(
        "profile.context",
        frozen.get("preferences", {}).get("self_reported_context"),
        "self_reported_context",
    )
    goal = frozen.get("goal") or {}
    if version is not None and not isinstance(goal, dict):
        goal = {}
    add("goal.text", goal.get("source_text"), "goal_text")
    if version is None:
        # Historical provider requests and stored projections retain their exact
        # old catalog, including positional IDs. Fixing those inputs in place
        # would invalidate already-paid receipts and immutable plan hashes.
        for i, requirement in enumerate(goal.get("requirements", [])):
            if isinstance(requirement, dict):
                for key in ("technology", "quote"):
                    add(f"goal.requirement.{i}.{key}", requirement.get(key), "goal_requirement")
    else:
        goal_version_id = goal.get("goal_version_id")
        if isinstance(goal_version_id, str) and goal_version_id:
            # Requirement identity is shared with the ingestion task contract.
            # A hash of the version keeps the reference syntax/length bounded;
            # its complete original identity remains explicit in the scope.
            version_key = fingerprint(goal_version_id)[:24]
            requirements: dict[str, dict[str, Any]] = {}
            ambiguous: set[str] = set()
            for requirement in iter_requirement_items(goal.get("requirements")):
                identifier = requirement_identity(requirement)
                if identifier in requirements and requirements[identifier] != requirement:
                    ambiguous.add(identifier)
                requirements[identifier] = requirement
            for identifier, requirement in sorted(requirements.items()):
                if identifier in ambiguous:
                    continue  # Conflicting IDs cannot silently select a quote.
                scope = {
                    "goal_version_id": goal_version_id,
                    "requirement_id": identifier,
                    "requirement_key": requirement.get("key"),
                    "required_depth": requirement.get("depth"),
                }
                if requirement.get("requirement_contract_version") == REQUIREMENT_VERSION:
                    # New provenance is never projected as legacy all-required
                    # text. Old catalog and paid-receipt shapes remain intact.
                    scope.update({key: deepcopy(requirement[key]) for key in JD_SCOPE_FIELDS})
                for key in ("technology", "quote"):
                    add(
                        f"goal.requirement.{version_key}.{identifier}.{key}",
                        requirement.get(key),
                        "goal_requirement",
                        **scope,
                    )
    if version == GOAL_CATALOG_VERSION:
        if goal.get("origin_contract") != "command-goal-v1" or not goal.get("version_hash"):
            raise DomainError("PLAN_GOAL_SCOPE", "用户目标目录缺少新增版本来源。", 422)
        version_key = fingerprint(goal["goal_version_id"])[:24]
        for objective in goal.get("user_learning_objectives", []):
            scope = {
                key: deepcopy(objective[key])
                for key in (
                    "objective_id",
                    "target_depth",
                    "learning_priority",
                    "user_mastery_unchanged",
                    "source",
                )
                if key in objective
            }
            scope.update(
                goal_version_id=goal["goal_version_id"],
                version_hash=goal["version_hash"],
                catalog_contract=GOAL_CATALOG_VERSION,
            )
            for field in ("technology", "target_depth"):
                add(
                    f"goal.user_objective.{version_key}.{objective['objective_id']}.{field}",
                    objective.get(field),
                    "user_learning_objective",
                    **scope,
                )
    marker = frozen.get("consumer_context_contract")
    validate_context_contract(marker)
    for unit in frozen.get("units", []):
        if pack_context_contract(unit) != marker:
            raise DomainError("CONSUMER_CONTEXT_CONTRACT", "文本目录与冻结事实使用合同不一致。")
        eligible = standalone_fact_ids(unit) if marker else None
        pid = unit["publication_id"]
        add(f"unit.{pid}.title", unit.get("title"), "unit_title", publication_id=pid)
        for i, limitation in enumerate(unit.get("limitations", [])):
            add(f"unit.{pid}.limitation.{i}", limitation, "unit_limitation", publication_id=pid)
        for fact in unit.get("facts", []):
            if eligible is not None and fact["fact_atom_id"] not in eligible:
                continue
            scope = {"publication_id": pid, "fact_atom_id": fact["fact_atom_id"]}
            add(f"fact.{fact['fact_atom_id']}.text", fact["text"], "fact_text", **scope)
            for i, condition in enumerate(fact.get("conditions", [])):
                add(
                    f"fact.{fact['fact_atom_id']}.condition.{i}",
                    condition,
                    "fact_condition",
                    **scope,
                )
    return catalog


def resolve_reference(
    ref: PlanTextReference,
    catalog: dict[str, dict[str, Any]],
    *,
    kinds: set[str] | None = None,
    publication_id: str | None = None,
    focus_fact_ids: list[str] | None = None,
    quote_policy: str | None = None,
) -> dict[str, Any]:
    if quote_policy is not None and quote_policy != QUOTE_PROJECTION_VERSION:
        raise DomainError("PLAN_QUOTE_POLICY", "未知的学习引文显示合同版本。", 422)
    entry = catalog.get(ref.text_id)
    if (
        not entry
        or ref.quote not in entry["text"]
        or (
            kinds is not None
            and entry["kind"] not in kinds
            and not (
                entry["kind"] == "user_learning_objective"
                and entry.get("catalog_contract") == GOAL_CATALOG_VERSION
                and "goal_requirement" in kinds
            )
        )
        or (publication_id and entry.get("publication_id", publication_id) != publication_id)
        or (
            focus_fact_ids is not None
            and entry.get("fact_atom_id")
            and entry["fact_atom_id"] not in focus_fact_ids
        )
        or not _reference_safe(entry, ref.quote, quote_policy)
    ):
        raise DomainError(
            "PLAN_TEXT_SCOPE", "计划文字必须是授权目标或所选审核事实中的安全原文摘录。", 422
        )
    return {
        "text_id": ref.text_id,
        "quote": ref.quote,
        "origin": entry["kind"],
        **{
            k: deepcopy(entry[k])
            for k in (
                "publication_id",
                "fact_atom_id",
                "goal_version_id",
                "requirement_id",
                "requirement_key",
                "required_depth",
                "objective_id",
                "target_depth",
                "learning_priority",
                "version_hash",
                "source",
                *JD_SCOPE_FIELDS,
            )
            if k in entry
        },
        "quoted_data_not_instruction": True,
        "excerpt_only": ref.quote != entry["text"],
        "reviewed_source": entry["kind"] in {"fact_text", "fact_condition"},
        "standalone_factual_claim": False,
        **({"quote_projection_version": quote_policy} if quote_policy is not None else {}),
    }


OBSERVABLES = {
    "cite_version": ("evidence_and_version", "指出固定来源、版本和支持片段"),
    "explain_conditions": ("conditions", "列出成立条件、边界与仍未知的结论"),
    "trace_input_output": (
        "execution_path",
        "逐步说明输入、调用次数、状态变化、返回值或异常；未执行须标明",
    ),
    "contrast_boundary": ("conditions", "对照边界两侧及恰好等于边界时的路径、输出与前提"),
    "identify_counterexample": ("counterexample", "给出不满足条件的反例并指出不能套用的结论"),
    "explain_variant": ("transfer", "说明变体改变的前提及必须重新核验的结论"),
    "verify_runtime_receipt": (
        "runtime",
        "仅引用绑定当前提交及独立审批的真实执行回执；否则标为未观察",
    ),
}


def project_task(
    module: PlanModuleDraft | PlanModuleDraftV162 | QuestionModuleDraft, frozen: dict[str, Any]
) -> dict[str, Any]:
    from .learning_scope_v166 import project_question_task

    if frozen.get("consumer_context_contract") is not None:
        pack = next(
            (p for p in frozen.get("units", []) if p["publication_id"] == module.publication_id),
            None,
        )
        if pack is None:
            raise DomainError("PLAN_EVIDENCE", "计划引用未知发布。", 422)
        require_frozen_standalone(frozen, pack, set(module.focus_fact_ids))

    if frozen.get("planner_contract_version") == QUESTION_PLAN_CONTRACT:
        if not isinstance(module, QuestionModuleDraft):
            raise DomainError("PLAN_QUESTION_SCOPE", "原题合同不能使用旧草稿。", 422)
        return project_question_task(module, frozen)
    if isinstance(module, QuestionModuleDraft):
        raise DomainError("PLAN_QUESTION_SCOPE", "原题草稿不能由旧合同读取。", 422)
    task: PlanTaskDraft | PlanTaskV162 | None = module.task
    prefs = frozen["preferences"]
    catalog = text_catalog(frozen)
    quote_policy = quote_projection_version(frozen)
    if task is None:
        # Providers using the original schema still get concrete, source-specific
        # targets. This fallback adds no facts or free-form generated instructions.
        def excerpt(entry: dict[str, Any]) -> PlanTextReference | None:
            text = entry["text"]
            if len(text) > 600:
                text = text[:600].rsplit(" ", 1)[0]
            return (
                PlanTextReference(text_id=entry["text_id"], quote=text)
                if _reference_safe(entry, text, quote_policy)
                else None
            )

        default_targets = [
            ref
            for entry in catalog.values()
            if entry.get("fact_atom_id") in module.focus_fact_ids and entry["kind"] == "fact_text"
            if (ref := excerpt(entry))
        ][:4]
        default_inputs = [
            ref
            for entry in catalog.values()
            if entry["kind"] == "self_reported_context"
            if (ref := excerpt(entry))
        ][:1]
        default_boundaries = [
            ref
            for entry in catalog.values()
            if entry.get("fact_atom_id") in module.focus_fact_ids
            and entry["kind"] == "fact_condition"
            if (ref := excerpt(entry))
        ][:8]
        default_checks = []
        if prefs["checks_enabled"] and default_targets:
            for observable, (key, _) in OBSERVABLES.items():
                if (
                    key in module.rubric_keys
                    and key != "runtime"
                    and observable != "contrast_boundary"
                ):
                    default_checks.append(
                        PlanAcceptanceDraft.model_validate(
                            {
                                "rubric_key": key,
                                "observable": observable,
                                "targets": default_targets,
                            }
                        )
                    )
        default_kind = (
            "transfer"
            if any(c.rubric_key == "transfer" for c in default_checks)
            else "understanding_check"
            if default_checks
            else "reading"
        )
        task = PlanTaskDraft.model_validate(
            {
                "kind": default_kind,
                "inputs": default_inputs,
                "boundaries": default_boundaries,
                "acceptance_checks": default_checks,
            }
        )
    enabled = {
        "reading": True,
        "diagnostic": prefs["diagnostics_enabled"],
        "understanding_check": prefs["checks_enabled"],
        "exercise": prefs["exercises_enabled"],
        "transfer": prefs["exercises_enabled"],
    }
    if not enabled[task.kind]:
        if frozen.get("planner_contract_version") == PLAN_CONTRACT_VERSION:
            raise DomainError(
                "LEARNING_OPT_OUT", f"用户已关闭 {task.kind} 活动；不可通过其他任务标签绕过。", 422
            )
        raise DomainError("LEARNING_OPT_OUT", "任务不可通过阅读标签绕过已关闭的检查或练习。", 422)
    if task.kind == "reading" and (task.scenarios or task.acceptance_checks):
        if frozen.get("planner_contract_version") == PLAN_CONTRACT_VERSION:
            raise DomainError(
                "PLAN_TASK_KIND", "阅读任务不可包含场景或验收；模型须遵守实际任务种类。", 422
            )
        # Preserve the legacy error for old queued plans and historical parsing.
        raise DomainError("LEARNING_OPT_OUT", "任务不可通过阅读标签绕过已关闭的检查或练习。", 422)

    def resolve(ref: PlanTextReference, kinds: set[str] | None = None) -> dict[str, Any]:
        return resolve_reference(
            ref,
            catalog,
            kinds=kinds,
            publication_id=module.publication_id,
            focus_fact_ids=module.focus_fact_ids,
            quote_policy=quote_policy,
        )

    title = resolve(task.title) if task.title else None
    if title and len(title["quote"]) > 120:
        raise DomainError("PLAN_TEXT_SCOPE", "模块显示标题摘录不得超过120字符。", 422)
    inputs = [resolve(ref) for ref in task.inputs]
    boundaries = [resolve(ref, EVIDENCE_KINDS) for ref in task.boundaries]
    scenarios = []
    for index, scenario in enumerate(task.scenarios):
        anchor = resolve(scenario.target)
        quote = anchor["quote"]
        quoted_data = display_reference(anchor)
        if scenario.kind.startswith("repeat_then_"):
            # A sequence's exception/symbol is data, never an arbitrary instruction.
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,79}", quote):
                raise DomainError("PLAN_SCENARIO", "重复场景须引用已给出的单个异常或符号名。", 422)
            ending = "正常返回" if scenario.kind == "repeat_then_return" else "再次抛出该异常"
            text = f"假设第1至{scenario.repeat_count}次调用抛出 {quote}，第{scenario.repeat_count + 1}次{ending}。"
        elif scenario.kind.startswith("boundary_"):
            relation = {
                "boundary_equal": "恰好等于边界",
                "boundary_below": "低于边界",
                "boundary_above": "高于边界",
            }[scenario.kind]
            text = f"构造{relation}的假设输入；对照资料摘录：{quoted_data}"
        else:
            text = f"按所给输入或场景分析：{quoted_data}"
        scenarios.append(
            {
                "index": index,
                "kind": scenario.kind,
                "repeat_count": scenario.repeat_count,
                "target": anchor,
                "text": text,
                "hypothetical": True,
                "runtime_observed": False,
            }
        )
    checks = []
    for index, criterion in enumerate(task.acceptance_checks):
        required_key, instruction = OBSERVABLES[criterion.observable]
        if criterion.rubric_key != required_key or required_key not in module.rubric_keys:
            raise DomainError("PLAN_ACCEPTANCE", "具体验收项必须对应模块内冻结的Rubric维度。", 422)
        if required_key == "runtime" and task.kind not in {"exercise", "transfer"}:
            raise DomainError("PLAN_ACCEPTANCE", "运行验收只适用于单独请求和审批的练习。", 422)
        if required_key == "transfer" and not prefs["exercises_enabled"]:
            raise DomainError("LEARNING_OPT_OUT", "具体验收项不得绕过关闭的迁移练习偏好。", 422)
        if required_key == "transfer" and task.kind not in {"exercise", "transfer"}:
            raise DomainError("PLAN_ACCEPTANCE", "迁移验收须明确标为练习或迁移活动。", 422)
        if len(set(criterion.scenario_indexes)) != len(criterion.scenario_indexes) or any(
            i < 0 or i >= len(scenarios) for i in criterion.scenario_indexes
        ):
            raise DomainError("PLAN_ACCEPTANCE", "验收项引用了不存在或重复的输入场景。", 422)
        targets = [resolve(ref) for ref in criterion.targets]
        checks.append(
            {
                "key": f"criterion-{index + 1}",
                "rubric_key": required_key,
                "observable": criterion.observable,
                "instruction": instruction,
                "targets": targets,
                "scenario_indexes": criterion.scenario_indexes,
                "criterion": instruction
                + "；对照："
                + "；".join(display_reference(x) for x in targets),
                "assessment_state": "not_observed",
                "mandatory": False,
            }
        )
    return {
        "schema_version": TASK_SCHEMA,
        "kind": task.kind,
        "title": (display_reference(title) if title["origin"] in EVIDENCE_KINDS else title["quote"])
        if title
        else None,
        "title_reference": title,
        "inputs": inputs,
        "scenarios": scenarios,
        "boundaries": boundaries,
        "acceptance_checks": checks,
        "proposal_only": True,
        "runtime_observed": False,
        "competence_verified": False,
        "text_scope": "source_and_request_excerpts_not_new_facts",
        "boundary_policy": "all_focused_fact_conditions_apply_even_if_excerpted",
    }


_STOP_WORDS = {
    "these",
    "those",
    "that",
    "this",
    "with",
    "from",
    "have",
    "only",
    "goal",
    "evidence",
    "source",
    "learning",
    "learn",
    "supplied",
    "supports",
    "would",
    "could",
    "cannot",
    "record",
    "what",
    "about",
    "requirements",
    "explicitly",
    "teach",
    "material",
    "materials",
    "provided",
}


def _terms(text: str) -> set[str]:
    tokens = {x.casefold() for x in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text)} - _STOP_WORDS
    # Matching is only used to select already-authorized goal text. It never
    # proves coverage, translates concepts, or promotes arbitrary model prose.
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def project_gaps(
    draft: PersonalizedPlanDraft | PersonalizedPlanDraftV162 | QuestionPlanDraft,
    frozen: dict[str, Any],
) -> dict[str, Any]:
    catalog = text_catalog(frozen)
    quote_policy = quote_projection_version(frozen)
    gaps: list[dict[str, Any]] = []
    skipped = 0
    legacy = 0
    duplicates = 0
    reasons = {
        "insufficient_evidence": "现有材料未充分覆盖该目标；需补充审核证据。",
        "outside_source_scope": "该目标超出现有材料的已审核范围；不推断项目具有该能力。",
        "runtime_evidence_missing": "该目标尚缺绑定版本与环境的真实运行证据。",
    }
    seen: set[str] = set()
    for item in draft.uncovered_goal_requirements:
        if isinstance(item, PlanGoalGapDraft):
            requirement = resolve_reference(
                item.requirement, catalog, kinds=GOAL_KINDS, quote_policy=quote_policy
            )
            evidence = [
                resolve_reference(ref, catalog, kinds=EVIDENCE_KINDS, quote_policy=quote_policy)
                for ref in item.evidence
            ]
            reason = item.reason
            mode = "structured_goal_reference"
        else:
            legacy += 1
            # Old prose is not trusted or displayed. Recover a bounded exact goal
            # fragment using lexical overlap; otherwise report that it was omitted.
            candidates = []
            for entry in catalog.values():
                if entry["kind"] not in GOAL_KINDS:
                    continue
                for fragment in re.split(r"(?<=[.!?。！？;；])\s+|[\n\r]+", entry["text"]):
                    fragment = fragment.strip()
                    overlap = len(_terms(item) & _terms(fragment))
                    if (
                        overlap >= 2
                        and 1 <= len(fragment) <= 600
                        and _reference_safe(entry, fragment, quote_policy)
                    ):
                        candidates.append((overlap, len(fragment), entry["text_id"], fragment))
            if not candidates:
                skipped += 1
                continue
            _, _, text_id, quote = max(candidates, key=lambda x: (x[0], x[1], x[2]))
            requirement = resolve_reference(
                PlanTextReference(text_id=text_id, quote=quote),
                catalog,
                kinds=GOAL_KINDS,
                quote_policy=quote_policy,
            )
            evidence, reason, mode = [], "insufficient_evidence", "legacy_goal_reference"
        key = fingerprint({"requirement": requirement, "reason": reason, "evidence": evidence})
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        gaps.append(
            {
                "requirement": requirement,
                "reason": reason,
                "text": reasons[reason] + " 目标摘录：" + display_reference(requirement),
                "evidence": evidence,
                "projection_mode": mode,
                "coverage_verified": False,
            }
        )
    result = {
        "uncovered_goal_requirements": gaps,
        "source_gap_count": len(draft.uncovered_goal_requirements),
        "projected_gap_count": len(gaps),
        "deduplicated_gap_count": duplicates,
        "unprojected_gap_count": skipped,
        "legacy_gap_count": legacy,
        "raw_draft_exposed": False,
    }
    if frozen.get("text_catalog_version") == GOAL_CATALOG_VERSION:
        result["user_objective_coverage"] = deepcopy(frozen.get("user_objective_coverage", []))
        result["uncovered_user_objectives"] = [
            {
                **deepcopy(item),
                "label": "用户新增学习目标",
                "text": "目标深度与能力尚未验证，未覆盖范围保留。",
            }
            for item in frozen.get("user_objective_coverage", [])
        ]
    return result
