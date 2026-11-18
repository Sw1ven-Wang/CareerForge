"""Goal-bound language confirmation and linked, language-neutral learning outlines."""

import re

from ..contracts_learning_language import normalize_language
from ..errors import DomainError
from ..models import GoalVersion
from ..util import fingerprint
from .dependencies import check_closure, depend, register, require_object
from .research_coverage import read_goal_version

LANGUAGE_CONTRACT = "learning-language-v1"
PROFILE_KEY = "learning_language_profile_id"
LANGUAGE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:Python3?|JavaScript|TypeScript|Java|Golang|Go|Rust|C\+\+|C#|Kotlin|Scala|Ruby|PHP|Swift)(?![A-Za-z0-9_])",
    re.I,
)


def language_prompt(profile):
    if profile["ai_agent_track"]:
        return "JD 未限定实现语言。AI 应用与 Agent 开发必学 Python；后端也使用 Python，还是增加 Java、Go 或其他语言？请回复 /language Python 或 /language Java,Go。架构知识始终跨语言。"
    return "JD 未限定实现语言。具体实现你想学习哪种语言？请回复 /language Java、/language Python 或其他语言。架构知识始终跨语言。"


def bare_language_choice(text):
    values = [s for s in re.split(r"[,，、\s]+", text.strip()) if s]
    return bool(values) and len(values) <= 8 and all(LANGUAGE_PATTERN.fullmatch(v) for v in values)


def domain_for(topic):
    return (
        "backend"
        if (topic or "").startswith(("storage.", "ops.", "code.", "runtime."))
        else "ai_agent"
    )


def implementation_languages(profile, domain):
    if domain == "ai_agent" and profile["ai_agent_track"]:
        return ["python"]
    return list(profile["backend_languages"])


def _store_profile(db, actor, workflow, value):
    obj = register(db, actor, workflow.namespace_id, "learning_language_profile", value)
    depend(db, actor, obj.id, workflow.goal_version_id, "binds_goal_version")
    workflow.target = {**workflow.target, PROFILE_KEY: obj.id}
    return value


def read_profile_in(db, actor, workflow):
    identifier = workflow.target.get(PROFILE_KEY)
    if not identifier:
        return None
    obj = require_object(db, actor, identifier, "learning_language_profile")
    check_closure(db, actor, [identifier])
    if (
        obj.namespace_id != workflow.namespace_id
        or obj.payload.get("workflow_id") != workflow.id
        or obj.payload.get("goal_version_id") != workflow.goal_version_id
    ):
        raise DomainError("LANGUAGE_SCOPE", "语言配置与当前目标不一致。", 409)
    return obj.payload


def initialize_language_in(db, actor, workflow):
    if workflow.target.get("learning_language_contract") != LANGUAGE_CONTRACT:
        return None
    if workflow.target.get(PROFILE_KEY):
        return read_profile_in(db, actor, workflow)
    read_goal_version(db, actor, workflow.goal_version_id)
    goal = db.get(GoalVersion, workflow.goal_version_id)
    rows = goal.requirements.get("requirements", [])
    ai = any(
        (r.get("topic_key") or "").startswith(("agent.", "rag.", "interface.mcp"))
        for r in rows
        if r.get("modality") != "not_required"
    )
    ai = ai or bool(re.search(r"AI\s*(?:应用|Agent)|Agent\s*开发", goal.source_text, re.I))
    # Only extracted, positively required language labels establish JD constraints.
    # Quotes and all original AND/OR/NOT qualifiers remain in the GoalVersion.
    jd_rows = [
        r
        for r in rows
        if r.get("modality") == "required" and LANGUAGE_PATTERN.search(r["technology"])
    ]
    jd_languages = list(
        dict.fromkeys(
            normalize_language(m.group())
            for r in jd_rows
            for m in LANGUAGE_PATTERN.finditer(r["technology"])
        )
    )
    value = {
        "contract": LANGUAGE_CONTRACT,
        "workflow_id": workflow.id,
        "goal_version_id": workflow.goal_version_id,
        "architecture_language_constraint": None,
        "ai_agent_track": ai,
        "required_core_languages": ["python"] if ai else [],
        "core_language_origin": "product_rule_ai_agent_python" if ai else None,
        "jd_languages": jd_languages,
        "jd_language_requirement_ids": [r["requirement_id"] for r in jd_rows],
        "jd_language_requirements": [
            {
                key: r.get(key)
                for key in (
                    "requirement_id",
                    "technology",
                    "modality",
                    "group_operator",
                    "group_id",
                    "group_path",
                )
            }
            for r in jd_rows
        ],
        "backend_languages": jd_languages,
        "status": "configured_from_jd" if jd_languages else "awaiting_user_choice",
        "selection_origin": "jd" if jd_languages else None,
        "user_mastery_unchanged": True,
    }
    return _store_profile(db, actor, workflow, value)


def require_language_in(db, actor, workflow):
    profile = initialize_language_in(db, actor, workflow)
    if profile and profile["status"] == "awaiting_user_choice":
        raise DomainError("LANGUAGE_CHOICE_REQUIRED", language_prompt(profile), 409)
    return profile


def select_language_in(app, db, actor, conv, workflow, languages, receipt, action_index):
    if workflow is None or workflow.goal_version_id is None:
        raise DomainError("GOAL_REQUIRED", "请先完成 JD 提取。", 409)
    if workflow.control_state != "active":
        raise DomainError("WORKFLOW_NOT_ACTIVE", "请先恢复工作流。", 409)
    if app.session_workflows.current(db, workflow) is not None:
        raise DomainError("LANGUAGE_SCOPE_FROZEN", "执行已开始，请新建学习目标后调整语言。", 409)
    if workflow.target.get("learning_language_contract") != LANGUAGE_CONTRACT:
        workflow.target = {**workflow.target, "learning_language_contract": LANGUAGE_CONTRACT}
    profile = initialize_language_in(db, actor, workflow)
    languages = list(dict.fromkeys(normalize_language(v) for v in languages))
    if not 1 <= len(languages) <= 8:
        raise DomainError("LANGUAGE_INPUT", "请选择 1 到 8 种实现语言。", 422)
    chosen = set(languages) | set(profile["required_core_languages"])
    alternatives = {}
    for row in profile.get("jd_language_requirements", []):
        terms = {
            normalize_language(m.group()) for m in LANGUAGE_PATTERN.finditer(row["technology"])
        }
        or_group = next(
            (g["group_id"] for g in row.get("group_path", []) if g["operator"] == "OR"), None
        )
        if row.get("group_operator") == "OR":
            or_group = or_group or row.get("group_id")
        if or_group:
            alternatives.setdefault(or_group, set()).update(terms)
        elif not terms <= chosen:
            raise DomainError(
                "JD_LANGUAGE_REQUIRED",
                "请选择保留 JD 必需语言的组合，可在此基础上增加后端语言。",
                409,
            )
    if any(not terms & chosen for terms in alternatives.values()):
        raise DomainError("JD_LANGUAGE_REQUIRED", "JD 的替代语言组至少需要选择一种。", 409)
    profile = {
        **profile,
        "backend_languages": languages,
        "status": "confirmed",
        "selection_origin": "user",
        "action_receipt_id": receipt.id,
        "action_index": action_index,
        "request_hash": receipt.request_hash,
    }
    _store_profile(db, actor, workflow, profile)
    store_outline_in(db, actor, workflow, profile)
    workflow.milestone = "awaiting_goal_confirmation"
    workflow.target = {**workflow.target, "waiting_user_action": "confirm_goal"}
    workflow.revision += 1
    app.workflows.focus_in(db, conv, workflow)
    return {
        "language_profile": profile,
        "reply": ("AI/Agent 实现必学 Python；" if profile["ai_agent_track"] else "")
        + "后端实现语言已设为 "
        + "、".join(languages)
        + "。已保存跨语言架构目标及关联实现目标，等待确认学习目标。",
        "run_refs": [],
    }


def store_outline_in(db, actor, workflow, profile):
    goal = db.get(GoalVersion, workflow.goal_version_id)
    entries = []
    for row in goal.requirements.get("requirements", []):
        if row.get("modality") == "not_required":
            continue
        rid = row["requirement_id"]
        base = fingerprint([workflow.goal_version_id, rid])[:24]
        domain = domain_for(row.get("topic_key"))
        entries.append(
            {
                "requirement_id": rid,
                "technology": row["technology"],
                "architecture": {
                    "entry_id": "architecture-" + base,
                    "language": None,
                    "knowledge_status": "awaiting_reviewed_material",
                },
                "implementation": {
                    "entry_id": "implementation-" + base,
                    "domain": domain,
                    "languages": implementation_languages(profile, domain),
                    "knowledge_status": "awaiting_reviewed_material",
                },
                "relation": "implementation_example_for",
                "semantic_quality_verified": False,
            }
        )
    value = {
        "contract": "linked-learning-outline-v1",
        "goal_version_id": workflow.goal_version_id,
        "language_profile_id": workflow.target[PROFILE_KEY],
        "entries": entries,
    }
    obj = register(db, actor, workflow.namespace_id, "learning_knowledge_outline", value)
    depend(db, actor, obj.id, workflow.target[PROFILE_KEY], "uses_language_profile")
    depend(db, actor, obj.id, workflow.goal_version_id, "binds_goal_version")
    workflow.target = {**workflow.target, "learning_knowledge_outline_id": obj.id}
    return value


def outline_in(db, actor, workflow):
    identifier = workflow.target.get("learning_knowledge_outline_id")
    if not identifier:
        return None
    obj = require_object(db, actor, identifier, "learning_knowledge_outline")
    check_closure(db, actor, [identifier])
    if obj.payload.get("language_profile_id") != workflow.target.get(PROFILE_KEY):
        raise DomainError("LANGUAGE_SCOPE", "学习目录与语言配置不一致。", 409)
    return obj.payload


def inherit_language_in(db, actor, workflow):
    """Run only after the new goal's exact successful command receipt is flushed."""
    from ..models_dialogue import WorkflowSession

    parent = (
        db.get(WorkflowSession, workflow.parent_workflow_id)
        if workflow.parent_workflow_id
        else None
    )
    if parent is None or parent.target.get("learning_language_contract") != LANGUAGE_CONTRACT:
        return
    previous = read_profile_in(db, actor, parent)
    workflow.target = {**workflow.target, "learning_language_contract": LANGUAGE_CONTRACT}
    current = initialize_language_in(db, actor, workflow)
    if previous:
        current = {
            **current,
            "backend_languages": previous["backend_languages"],
            "status": previous["status"],
            "selection_origin": "inherited_goal",
            "parent_language_profile_id": parent.target[PROFILE_KEY],
        }
        _store_profile(db, actor, workflow, current)
        depend(
            db,
            actor,
            workflow.target[PROFILE_KEY],
            parent.target[PROFILE_KEY],
            "inherits_language_choice",
        )
    if current["status"] == "awaiting_user_choice":
        workflow.milestone = "awaiting_language_choice"
        workflow.target = {
            **workflow.target,
            "waiting_user_action": "select_implementation_language",
        }
    else:
        store_outline_in(db, actor, workflow, current)
