"""Single-session drafts, total budgets and acceptance-linked private schedules."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..contracts_learning_ext import PersonalizedPlanInput
from ..contracts_session_plan_v166 import SessionPlanInput
from ..db import locked
from ..errors import DomainError
from ..models import Idempotency, Principal
from ..util import fingerprint
from .dependencies import register, require_object
from .security import private_namespace

SESSION_PLAN_CONTRACT = "learning-session-plan-1.6.6"
SESSION_PLAN_RULES = """
本请求是单次学习计划。所有模块 estimated_minutes 总和不得超过 session_budget_minutes。
每个模块仍不得超过用户单模块时间上限。不要为凑齐资料而超时；不能安排的资料明确留作未覆盖。
session_request.preferences 只是本次计划覆盖，不是永久设置。无练习时不安排练习/迁移任务。
learning_preferences.preferences.requested_style 决定组织顺序；architecture_first先整体结构，
evidence_first先来源再结论，均只能使用现有已审材料。草案不能自行采纳、排期到日历或声称已学会。
"""


def module_schedule(modules):
    result, cursor = [], 0
    for module_id, content in modules:
        duration = content.get("estimated_minutes")
        if type(duration) is not int or not 5 <= duration <= 240:
            raise DomainError("PLAN_SESSION_BUDGET", "单次计划模块缺少有效时长。", 422)
        result.append(
            {
                "module_id": module_id,
                "publication_id": content["publication_id"],
                "prerequisite_module_ids": content.get("prerequisite_module_ids", []),
                "activity_kind": content.get("task", {}).get("kind", "reading")
                if content.get("task")
                else "reading",
                "start_offset_minutes": cursor,
                "end_offset_minutes": cursor + duration,
                "estimated_minutes": duration,
            }
        )
        cursor += duration
    return result


def session_projection(frozen, schedule):
    version = frozen.get("session_contract")
    if version is None:
        return {}
    if version != SESSION_PLAN_CONTRACT:
        raise DomainError("PLAN_SESSION_CONTRACT", "未知单次计划合同版本。", 422)
    budget = frozen.get("session_budget_minutes")
    if (
        type(budget) is not int
        or not 5 <= budget <= 240
        or not isinstance(schedule, list)
        or not schedule
    ):
        raise DomainError("PLAN_SESSION_BUDGET", "单次计划预算或模块安排无效。", 422)
    cursor = 0
    for item in schedule:
        minutes = item.get("estimated_minutes")
        if (
            type(minutes) is not int
            or minutes < 5
            or item.get("start_offset_minutes") != cursor
            or item.get("end_offset_minutes") != cursor + minutes
        ):
            raise DomainError("PLAN_SESSION_BUDGET", "单次学习安排不是连续有效的时间顺序。", 422)
        cursor += minutes
    if cursor > budget:
        raise DomainError("PLAN_SESSION_BUDGET", "所有学习模块的总时长超出本次预算。", 422)
    selected = {item["publication_id"] for item in schedule}
    return {
        "session_contract": version,
        "total_minutes": budget,
        "planned_minutes": cursor,
        "schedule": schedule,
        "schedule_basis": "relative_order_not_external_calendar",
        "unplanned_publication_ids": [
            identifier for identifier in frozen["publication_ids"] if identifier not in selected
        ],
    }


class SessionPlanMixin:
    app: Any
    db: Any

    def start_session_plan(
        self, actor: str, value: SessionPlanInput, *, db=None, dialogue_scope=None
    ):
        if value.publication_ids and value.total_minutes >= 5:
            if not value.funding_id:
                raise DomainError("FUNDING_REQUIRED", "可生成的单次计划需要明确经费授权。", 422)
            base = PersonalizedPlanInput(
                namespace_id=value.namespace_id,
                funding_id=value.funding_id,
                publication_ids=value.publication_ids,
                learning_intent=value.learning_intent,
                title=value.title,
                expected_profile_revision=value.expected_profile_revision,
                idempotency_key=value.idempotency_key,
            )
            return self.app.learning.start_personalized_plan(
                actor, base, session_request=value, db=db, dialogue_scope=dialogue_scope
            )
        from .learning_ext import _no_independent_assistance, _profile

        request_hash = fingerprint(value.model_dump(mode="json"))
        with self.db.transaction() as db:
            locked(db, Principal, actor)
            ns = private_namespace(db, actor)
            if value.namespace_id != ns.id:
                raise DomainError("LEARNING_PRIVATE", "单次学习计划只能保存在本人私人域。", 403)
            old = db.scalar(
                select(Idempotency).where(
                    Idempotency.principal_id == actor,
                    Idempotency.operation == "learning.session_plan",
                    Idempotency.key == value.idempotency_key,
                )
            )
            if old:
                if old.request_hash != request_hash:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "单次计划请求键对应不同内容。")
                require_object(db, actor, old.result["draft_id"], "selection_manifest")
                return {**old.result, "replayed": True}
            row = _profile(db, actor)
            if row.revision != value.expected_profile_revision:
                raise DomainError("REVISION_CONFLICT", "学习偏好已变化。")
            _no_independent_assistance(db, actor)
            # Invalid/unpublished IDs must not become an apparently valid draft.
            for publication in value.publication_ids:
                self.app.knowledge.publication_pack(db, actor, publication)
            reason = (
                "no_reviewed_knowledge"
                if not value.publication_ids
                else "time_budget_below_minimum_module"
            )
            payload = {
                "purpose": "unbuildable_session_plan_draft",
                "session_contract": SESSION_PLAN_CONTRACT,
                "status": "unbuildable",
                "total_minutes": value.total_minutes,
                "planned_minutes": 0,
                "schedule": [],
                "module_ids": [],
                "uncovered_requirements": [{"request_title": value.title, "reason": reason}],
                "unplanned_publication_ids": value.publication_ids,
                "accepted": False,
                "can_accept": False,
                "model_calls": 0,
                "competence_verified": False,
            }
            draft = register(db, actor, ns.id, "selection_manifest", payload)
            result = {"draft_id": draft.id, **payload}
            db.add(
                Idempotency(
                    principal_id=actor,
                    operation="learning.session_plan",
                    key=value.idempotency_key,
                    request_hash=request_hash,
                    result=result,
                )
            )
            return result

    def session_plan_gap(self, actor, identifier):
        with self.db.read() as db:
            ns = private_namespace(db, actor)
            row = require_object(db, actor, identifier, "selection_manifest")
            if (
                row.namespace_id != ns.id
                or row.payload.get("purpose") != "unbuildable_session_plan_draft"
            ):
                raise DomainError("NOT_FOUND", "单次计划缺口草稿不存在。", 404)
            return {"draft_id": row.id, **row.payload}
