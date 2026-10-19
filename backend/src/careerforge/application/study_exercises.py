"""Versioned, intent-aware material requirements; no mastery inference."""

from careerforge.domain.json_types import JsonObject as Json


def profile(intent: str, module: Json, kind: str) -> Json:
    execution = intent == "build_project" or (intent == "function" and module["depth"] != "了解")
    labels = {
        "build_project": "项目构建",
        "learn_project": "架构与执行链路说明",
        "technical_design": "设计取舍报告",
        "function": "函数实现解析",
    }
    materials = (
        ["code_artifact", "execution_record"]
        if execution
        else ["learning_report", "source_evidence"]
    )
    criteria: list[Json] = [
        {"dimension": "artifact", "scenario": labels.get(intent, intent), "materials": materials},
        {
            "dimension": "explanation",
            "scenario": "本人解释关键机制、依据与未验证边界",
            "materials": ["explanation_answer"],
        },
    ]
    if kind == "transfer":
        criteria.append(
            {
                "dimension": "transfer",
                "scenario": "本人迁移与差异说明",
                "materials": ["transfer_note"],
            }
        )
    return {
        "version": "intent-exercise-1",
        "intent": intent,
        "execution_required": execution,
        "artifact_kind": "code" if execution else "report",
        "criteria": criteria,
        "competence_verified": False,
    }
