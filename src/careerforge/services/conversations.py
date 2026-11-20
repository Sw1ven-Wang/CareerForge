from __future__ import annotations

import re
import time
from contextlib import nullcontext
from typing import Any

from sqlalchemy import select

from ..contracts import (
    IntentDecision,
    KnowledgeBuildInput,
    OrchestrationDecision,
    RetrievalTaskSpec,
    TaskType,
    TurnInput,
)
from ..contracts_answer_completion_v166 import (
    ANSWER_ASPECT_CONTRACT,
    ANSWER_COMPLETION_CONTRACT,
    ANSWER_COMPLETION_STAGE,
    EVIDENCE_ANSWER_STATE,
    EVIDENCE_CONTINUITY_CONTRACT,
    TUTOR_PROGRESS_CONTRACT,
)
from ..contracts_feedback_target_v1 import (
    FEEDBACK_TARGET_CHOICE,
    FEEDBACK_TARGET_KEY,
    AnswerFeedbackConversationInput,
)
from ..contracts_ordinary_checker_v166 import ORDINARY_CHECKER_KEY
from ..contracts_session_tutor_v166 import GuidedTutorTurnInput, SessionTutorTurnInput
from ..db import locked
from ..errors import DomainError
from ..models import (
    AgentRun,
    AnswerBlock,
    Conversation,
    KnowledgeGap,
    LearningPlan,
    Principal,
    Turn,
    Workspace,
)
from ..models_learning_ext import LearningProfile
from ..util import fingerprint
from .answer_continuity import continuation_projection, continued_focus
from .answers import stream_event
from .explicit_source_paths import (
    LOCATE_PATH_GUARD,
    SOURCE_PATH_CONTRACT,
    navigation_path_request,
    requested_source_path,
    resolve_source_path,
    source_path_gap,
)
from .feedback_target_v1 import (
    conversation_choice_in,
    freeze_preferences_in,
)
from .feedback_target_v1 import (
    creation_request as feedback_creation_request,
)
from .feedback_target_v1 import (
    seal_run_in as seal_feedback_run_in,
)
from .feedback_target_v1 import (
    validate_turn_in as validate_feedback_turn_in,
)
from .learning import COMPARISON_SCOPE_CONTRACT
from .learning_events import (
    capture_dialogue_preferences_in,
    preference_feedback_only,
    preference_projection_in,
)
from .learning_intent import (
    LEARNING_ROUTING_CONTRACT,
    MULTIVALUE_COMPARISON_CONTRACT,
    PAIRED_COMPARISON_CONTRACTS,
    SCOPED_QUESTION_ROUTING_CONTRACT,
    inaccessible_knowledge_request,
    learning_intent,
    module_guidance_request,
)
from .orchestration import ROUTING_CONTRACT_VERSION
from .ordinary_checker import (
    adopt_conversation_contract_in,
    bind_ordinary_selection_in,
    conversation_contract_in,
    create_conversation_contract_in,
    freeze_ordinary_turn_in,
    seal_ordinary_run_in,
    validate_ordinary_turn_in,
)
from .policies import ensure_policies, frozen_policies
from .privacy import assert_personal_access
from .question_coverage import QUESTION_COVERAGE_CONTRACT, needs_coverage_selection
from .retrieval import SOURCE_NAVIGATION_CONTRACT
from .retrieval_context import RETRIEVAL_CONTEXT_CONTRACT
from .runtime import interrupt_turn, leased
from .security import namespace_access, private_namespace
from .session_tutor import (
    SESSION_TUTOR_KEY,
    freeze_session_tutor_in,
    seal_session_funding_run_in,
    validate_session_funding_in,
    validate_session_tutor_in,
)

_REFERENCE_LOCATION = re.compile(
    r"\s*(?:(?:(?:刚才|之前|上述)(?:的)?)?(?:这个|那个|该|它)(?:函数|方法|符号|实现)?"
    r"(?:在哪|在哪里|的定义在哪|的定义在哪里)|(?:定位|查找)(?:刚才|之前)?(?:这个|那个|该|它)"
    r"(?:函数|方法|符号|实现)?|where\s+(?:is|was)\s+(?:it|this|that|the previous)"
    r"(?:\s+(?:function|method|symbol|implementation))?(?:\s+(?:defined|located))?|"
    r"(?:locate|find|show)\s+(?:it|this|that|the previous)(?:\s+(?:function|method|symbol))?)"
    r"\s*[?？.!。]*\s*",
    re.I,
)


class ConversationService:
    def __init__(self, app):
        self.app, self.db = app, app.db

    def create_answer_feedback(self, actor: str, value: AnswerFeedbackConversationInput) -> str:
        return self.create(actor, value.auto_build, value.build_call_cap, feedback_choice=value)

    def create(
        self,
        actor: str,
        auto_build: bool = False,
        build_call_cap: int = 24,
        *,
        feedback_choice: AnswerFeedbackConversationInput | None = None,
    ) -> str:
        if not 8 <= build_call_cap <= 100:
            raise DomainError("BUDGET", "知识构建额度必须在8–100之间。", 422)
        with self.db.transaction() as db:
            ns = private_namespace(db, actor)
            ensure_policies(db, actor, ns.id)
            row = Conversation(
                principal_id=actor,
                namespace_id=ns.id,
                preferences={"auto_build": auto_build, "build_call_cap": build_call_cap},
            )
            db.add(row)
            db.flush()
            if feedback_choice is not None:
                feedback_choice = AnswerFeedbackConversationInput.model_validate(feedback_choice)
                row.preferences = {
                    **row.preferences,
                    FEEDBACK_TARGET_CHOICE: feedback_choice.contract,
                }
            create_conversation_contract_in(
                db,
                actor,
                row,
                admission="new_conversation",
                request=feedback_creation_request(feedback_choice) if feedback_choice else None,
            )
            return row.id

    def adopt_checker_contract(self, actor, identifier, value):
        with self.db.transaction() as db:
            locked(db, Principal, actor)
            conv = self._owned(db, actor, identifier, lock=True)
            return adopt_conversation_contract_in(db, actor, conv, value)

    def detail(self, actor, identifier):
        with self.db.read() as db:
            row = self._owned(db, actor, identifier)
            conversation_contract_in(db, actor, row)
            conversation_choice_in(db, actor, row)
            return {
                "conversation_id": row.id,
                "revision": row.revision,
                "focus": row.focus,
                "preferences": row.preferences,
            }

    def submit(self, actor: str, identifier: str, value: TurnInput) -> dict[str, Any]:
        return self._submit(actor, identifier, value, ordinary_entry=True)

    def submit_gap_reanswer(self, actor, identifier, value, binding):
        return self._submit(actor, identifier, value, gap_reanswer=binding)

    def submit_session(self, actor: str, identifier: str, value: SessionTutorTurnInput):
        return self._submit(actor, identifier, value, session_value=value)

    def submit_guided(self, actor: str, identifier: str, value: GuidedTutorTurnInput):
        return self._submit(actor, identifier, value, guided=True)

    @staticmethod
    def _active_session_request(db, actor, conv, value):
        profile = db.scalar(select(LearningProfile).where(LearningProfile.principal_id == actor))
        plan = (
            db.get(LearningPlan, profile.active_plan_id)
            if profile and profile.active_plan_id
            else None
        )
        if (
            profile is None
            or profile.namespace_id != conv.namespace_id
            or plan is None
            or plan.principal_id != actor
            or plan.namespace_id != conv.namespace_id
            or not plan.active_version_id
            or not profile.active_module_id
        ):
            raise DomainError(
                "GUIDED_MODULE_MISSING", "请先接受一份有已审知识的单次学习计划。", 409
            )
        return SessionTutorTurnInput(
            **value.model_dump(exclude={"question"}),
            question=value.question
            or (
                "请开始讲解当前已接受的学习模块。按模块的教学步骤，先说明学习焦点，"
                "再使用已审核的项目事实和可定位源码，讲清关键条件与适用边界。"
                "直接提供讲解，学习者无需先回答问题。"
            ),
            namespace_id=conv.namespace_id,
            plan_id=plan.id,
            version_id=plan.active_version_id,
            module_id=profile.active_module_id,
            expected_plan_revision=plan.revision,
        )

    def session_binding(self, actor: str, turn_id: str):
        with self.db.read() as db:
            turn = db.get(Turn, turn_id)
            if turn is None:
                raise DomainError("NOT_FOUND", "会话不存在。", 404)
            self._owned(db, actor, turn.conversation_id)
            active = db.get(AgentRun, turn.run_id) if turn.run_id else None
            snapshot = validate_session_tutor_in(db, actor, turn, run=active, history=True)
            if snapshot is None:
                raise DomainError("NOT_FOUND", "本轮未显式绑定单次学习计划。", 404)
            return {
                "turn_id": turn.id,
                "run_id": turn.run_id,
                **turn.focus[SESSION_TUTOR_KEY],
                "learning_session_id": snapshot["learning_session_id"],
                "module": snapshot["module"],
                **({"module_guidance": True} if snapshot.get("module_guidance") is True else {}),
                "budget": snapshot["budget"],
                "effective_preferences": snapshot["effective_preferences"],
                "preference_sources": snapshot["preference_sources"],
                "module_completed": False,
                "competence_verified": False,
            }

    def teaching_session(self, actor, session_id):
        from .session_progress import session_detail

        return session_detail(self.app, actor, session_id)

    def acknowledge_module(self, actor, value):
        from .session_progress import acknowledge_module

        return acknowledge_module(self.app, actor, value)

    def _submit(
        self,
        actor,
        identifier,
        value,
        *,
        session_value=None,
        guided=False,
        ordinary_entry=False,
        gap_reanswer=None,
        learning_request_id=None,
        db=None,
    ):
        guided_hash = fingerprint(value.model_dump(mode="json")) if guided else None
        module_guidance = guided and value.question is None
        with self.db.transaction() if db is None else nullcontext(db) as db:
            locked(db, Principal, actor)  # Same lock as independent-attempt creation.
            conv = self._owned(db, actor, identifier, lock=True)
            old = db.scalar(
                select(Turn).where(
                    Turn.conversation_id == identifier,
                    Turn.idempotency_key == value.idempotency_key,
                )
            )
            auto_guided = ordinary_entry and (
                (old is not None and old.focus.get("guided_from_chat") is True)
                or (old is None and module_guidance_request(value.question))
            )
            if auto_guided:
                if old is None:
                    profile = db.scalar(
                        select(LearningProfile).where(LearningProfile.principal_id == actor)
                    )
                    auto_guided = bool(
                        profile and profile.active_plan_id and profile.active_module_id
                    )
                if auto_guided:
                    guided = module_guidance = True
                    guided_hash = fingerprint(
                        {"entry": "chat", "request": value.model_dump(mode="json")}
                    )
                    value = GuidedTutorTurnInput.model_validate(
                        {**value.model_dump(mode="json"), "question": None}
                    )
            if old:
                if (
                    learning_request_id is not None
                    and old.focus.get("learning_tutor_request", {}).get("request_id")
                    != learning_request_id
                ):
                    raise DomainError("IDEMPOTENCY_CONFLICT", "已有回答不属于该固定学习请求。", 409)
                if old.focus.get("guided_request_hash") != guided_hash:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "相同消息键对应不同的指导请求。")
                if guided:
                    snapshot = validate_session_tutor_in(
                        db,
                        actor,
                        old,
                        run=db.get(AgentRun, old.run_id) if old.run_id else None,
                        history=old.status == "completed",
                    )
                    if snapshot is None:
                        raise DomainError("SESSION_TUTOR_BINDING", "指导请求缺少固定模块。", 409)
                    session_value = value = SessionTutorTurnInput.model_validate(
                        snapshot["request"]
                    )
                validate_feedback_turn_in(
                    db, actor, old, run=db.get(AgentRun, old.run_id) if old.run_id else None
                )
                ordinary = validate_ordinary_turn_in(
                    db,
                    actor,
                    old,
                    run=db.get(AgentRun, old.run_id) if old.run_id else None,
                    history=old.status == "completed",
                )
                if ordinary is not None and ordinary["request_hash"] != fingerprint(
                    value.model_dump(mode="json")
                ):
                    raise DomainError(
                        "IDEMPOTENCY_CONFLICT", "相同消息键对应不同的完整普通请求。", 409
                    )
                token = old.focus.get(SESSION_TUTOR_KEY)
                if bool(token) != bool(session_value):
                    raise DomainError("IDEMPOTENCY_CONFLICT", "消息键不能跨导师绑定合同复用。")
                if session_value:
                    if token["request_hash"] != fingerprint(session_value.model_dump(mode="json")):
                        raise DomainError("IDEMPOTENCY_CONFLICT", "相同消息键对应不同导师请求。")
                    validate_session_tutor_in(
                        db,
                        actor,
                        old,
                        run=db.get(AgentRun, old.run_id) if old.run_id else None,
                        history=old.status == "completed",
                    )
                    if old.status in {"cancelled", "failed", "interrupted"}:
                        raise DomainError("ANSWER_INTERRUPTED", "已中断导师请求不能恢复。")
                if old.question != value.question:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "相同消息键对应不同内容。")
                if old.focus.get("gap_reanswer") != gap_reanswer:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "消息键不能跨缺口复答合同复用。", 409)
                return {
                    "turn_id": old.id,
                    "run_id": old.run_id,
                    "revision": conv.revision,
                    "status": old.status,
                }
            learning_binding = None
            if learning_request_id is not None:
                if session_value is None or guided or ordinary_entry or gap_reanswer is not None:
                    raise DomainError(
                        "LEARNING_REQUEST_BINDING", "学习请求只允许固定单次导师准入。", 409
                    )
                learning_binding = self.app.learning_requests.validate_dispatch_in(
                    db, actor, conv, learning_request_id, session_value
                )
            self.app.practice_workflows.require_no_active_in(db, identifier)
            pending = self.app.learning_requests.pending_in(db, identifier)
            if any(row.id != learning_request_id for row in pending):
                raise DomainError(
                    "LEARNING_REQUEST_PENDING", "当前导师请求尚未完成；请先处理该请求。", 409
                )
            conversation_contract_in(db, actor, conv)
            if conv.revision != value.expected_revision:
                raise DomainError("REVISION_CONFLICT", "会话焦点已变化。")
            if db.scalar(
                select(Turn.id).where(
                    Turn.conversation_id == identifier, Turn.status.in_(["queued", "running"])
                )
            ):
                raise DomainError("CONVERSATION_BUSY", "当前回答尚未完成；可查询进度或取消。")
            # Independent mode is account-wide, not bypassable by omitting an exercise ID.
            if db.scalar(
                select(Workspace.id).where(
                    Workspace.principal_id == actor,
                    Workspace.assistance_mode == "independent",
                    Workspace.sealed.is_(False),
                )
            ):
                raise DomainError(
                    "INDEPENDENT_ATTEMPT", "独立练习进行中，暂停本账号学习和编码辅助。"
                )
            if guided:
                session_value = value = self._active_session_request(db, actor, conv, value)
            policies = ensure_policies(db, actor, conv.namespace_id)
            turn = Turn(
                conversation_id=identifier,
                question=value.question,
                focus_revision=conv.revision,
                focus={
                    **{
                        k: v
                        for k, v in conv.focus.items()
                        if k
                        not in {
                            EVIDENCE_ANSWER_STATE,
                            "evidence_continuity_contract",
                            "guided_request_hash",
                            "guided_from_chat",
                        }
                    },
                    **({"guided_request_hash": guided_hash} if guided else {}),
                    **({"guided_from_chat": True} if auto_guided else {}),
                    "preferences": conv.preferences,
                    "answer_contract": "atomic-answer-1.6.1",
                    "answer_completion_contract": ANSWER_COMPLETION_CONTRACT,
                    "answer_aspect_contract": ANSWER_ASPECT_CONTRACT,
                    "question_coverage_contract": QUESTION_COVERAGE_CONTRACT,
                    **(
                        {"evidence_continuity_contract": EVIDENCE_CONTINUITY_CONTRACT}
                        if not value.funding_id
                        else {}
                    ),
                    "routing_contract_version": SCOPED_QUESTION_ROUTING_CONTRACT,
                    "comparison_gate": MULTIVALUE_COMPARISON_CONTRACT,
                    "comparison_side_coverage": True,
                    "retrieval_context_contract": RETRIEVAL_CONTEXT_CONTRACT,
                    "source_navigation_contract": SOURCE_NAVIGATION_CONTRACT,
                    "source_path_contract": SOURCE_PATH_CONTRACT,
                    # Internal durable version, never accepted from TurnInput
                    # or a coordinator proposal. Old turns retain old semantics.
                    "source_path_guard": LOCATE_PATH_GUARD,
                    **(
                        {
                            "gap_reanswer": gap_reanswer,
                            "project_source_ids": [gap_reanswer["source_id"]],
                            "topic_keys": gap_reanswer["topic_keys"],
                        }
                        if gap_reanswer
                        else {}
                    ),
                    **(
                        {
                            "source_path_source_ids": self.app.coordinator.freeze_path_source_ids(
                                db, actor, value.question, conv.focus
                            )
                        }
                        if requested_source_path(value.question) is not None
                        else {}
                    ),
                },
                policy_versions=policies,
                idempotency_key=value.idempotency_key,
            )
            if learning_binding is not None:
                turn.focus = {**turn.focus, "learning_tutor_request": learning_binding}
            db.add(turn)
            db.flush()
            session_snapshot = None
            if not session_value:
                freeze_ordinary_turn_in(db, actor, conv, turn, value)
            if session_value:
                token, session_snapshot = freeze_session_tutor_in(
                    self.app, db, actor, conv, turn, session_value, module_guidance=module_guidance
                )
                turn.focus = {
                    **turn.focus,
                    SESSION_TUTOR_KEY: token,
                    "tutor_progress_contract": TUTOR_PROGRESS_CONTRACT,
                }
            preference_clarifications: list[str] = []
            preference_events = (
                capture_dialogue_preferences_in(
                    db, actor, conv, turn, clarifications=preference_clarifications
                )
                if session_snapshot is None
                else []
            )
            projection = preference_projection_in(
                db,
                actor,
                {**conv.focus, "preferences": conv.preferences},
                conversation_id=conv.id,
                turn_id=turn.id,
            )
            if session_snapshot is not None:
                projection = session_snapshot["learning_preferences"]
                # The explicitly selected module, not an unrelated previous
                # conversation topic, supplies this turn's local continuation scope.
                turn.focus = {
                    **turn.focus,
                    "topic_keys": session_snapshot["task_spec"]["topic_keys"],
                    "retrieval_topic_keys": session_snapshot["task_spec"]["topic_keys"],
                    "topic_hints": [],
                    "project_source_ids": session_snapshot["task_spec"]["project_source_ids"],
                }
            projection = freeze_preferences_in(
                db, actor, conv, turn, projection, session=session_snapshot is not None
            )
            turn.focus = {
                **turn.focus,
                "preferences": projection["preferences"],
                "learning_preferences": projection,
                "learning_intent": learning_intent(value.question, conv.focus),
                "preference_event_ids": [event["event_id"] for event in preference_events],
                "preference_clarification": preference_clarifications[0]
                if preference_clarifications
                else None,
                "preference_feedback_only": bool(
                    preference_events and preference_feedback_only(value.question)
                ),
            }
            turn.policy_versions = self.app.rollouts.assign_in(
                db, actor, conv.namespace_id, turn.id, policies
            )
            if value.funding_id:
                run = self.app.runtime.create_in(
                    db,
                    actor,
                    conv.namespace_id,
                    value.funding_id,
                    "chat",
                    {
                        "turn_id": turn.id,
                        **(
                            {"learning_tutor_request": learning_binding} if learning_binding else {}
                        ),
                        "model_profile": self.app.provider.profile_version,
                        "funding_id": value.funding_id,
                        "answer_contract": "atomic-answer-1.6.1",
                        "answer_completion_contract": ANSWER_COMPLETION_CONTRACT,
                        "answer_aspect_contract": ANSWER_ASPECT_CONTRACT,
                        "policy_input_contract": "policy-lineage-1.6.1",
                        **(
                            {
                                "gap_reanswer": gap_reanswer,
                                "access_dependencies": gap_reanswer.get(
                                    "evidence_publication_ids", gap_reanswer["publication_ids"]
                                ),
                            }
                            if gap_reanswer
                            else {}
                        ),
                        **(
                            {FEEDBACK_TARGET_KEY: turn.focus[FEEDBACK_TARGET_KEY]}
                            if FEEDBACK_TARGET_KEY in turn.focus
                            else {}
                        ),
                        **(
                            {ORDINARY_CHECKER_KEY: turn.focus[ORDINARY_CHECKER_KEY]}
                            if ORDINARY_CHECKER_KEY in turn.focus
                            else {}
                        ),
                        **(
                            {SESSION_TUTOR_KEY: turn.focus[SESSION_TUTOR_KEY]}
                            if session_value
                            else {}
                        ),
                    },
                    call_cap=session_value.model_call_cap
                    if session_value
                    else (24 if gap_reanswer or turn.focus.get("project_source_ids") else 8),
                    key="chat:" + turn.id,
                )
                turn.run_id = run.id
                if session_value:
                    seal_session_funding_run_in(db, actor, turn, run)
                    validate_session_funding_in(db, actor, run, turn.focus[SESSION_TUTOR_KEY])
                else:
                    seal_ordinary_run_in(db, actor, turn, run)
                    seal_feedback_run_in(db, actor, turn, run)
            conv.revision += 1
            stream_event(
                db,
                turn.id,
                "turn.accepted",
                {"turn_id": turn.id, "run_id": turn.run_id, "evidence_only": not bool(turn.run_id)},
            )
            return {
                "turn_id": turn.id,
                "run_id": turn.run_id,
                "revision": conv.revision,
                "status": turn.status,
            }

    async def process(self, turn_id: str, run: tuple[str, int, str] | None = None) -> None:
        started = time.monotonic()
        with self.db.transaction() as db:
            if run:
                leased(db, *run)
            preview = db.get(Turn, turn_id)
            if preview is not None and SESSION_TUTOR_KEY in preview.focus:
                preview_conv = db.get(Conversation, preview.conversation_id)
                validate_session_tutor_in(
                    db,
                    preview_conv.principal_id,
                    preview,
                    run=db.get(AgentRun, preview.run_id) if preview.run_id else None,
                    history=preview.status == "completed",
                )
            turn = locked(db, Turn, turn_id)
            conv = db.get(Conversation, turn.conversation_id)
            if conv is None:
                raise DomainError("NOT_FOUND", "会话不存在。", 404)
            actor, namespace, question = conv.principal_id, conv.namespace_id, turn.question
            validate_ordinary_turn_in(
                db,
                actor,
                turn,
                run=db.get(AgentRun, turn.run_id) if turn.run_id else None,
                history=turn.status == "completed",
            )
            validate_feedback_turn_in(
                db, actor, turn, run=db.get(AgentRun, turn.run_id) if turn.run_id else None
            )
            privacy_generation = assert_personal_access(db, actor)
            namespace_access(db, actor, namespace, True)
            if turn.status in {"interrupted", "cancelled", "failed"}:
                raise DomainError("ANSWER_INTERRUPTED", "已中断回答不能重新执行。")
            if turn.status == "completed":
                return
            if turn.run_id and (not run or run[0] != turn.run_id):
                raise DomainError("TURN_RUN", "会话执行合同不匹配。", 403)
            active = db.get(AgentRun, run[0]) if run else None
            session_snapshot = validate_session_tutor_in(db, actor, turn, run=active)
            focus, revision = dict(turn.focus), turn.focus_revision
            focus.pop("accepted_module_guidance", None)
            if session_snapshot is not None and session_snapshot.get("module_guidance") is True:
                focus["accepted_module_guidance"] = True
            if run:
                active = db.get(AgentRun, run[0])
                if any(
                    active.input_manifest.get(key) != focus.get(key)
                    for key in ("answer_completion_contract", "answer_aspect_contract")
                ):
                    raise DomainError("ANSWER_COMPLETION_BINDING", "回答完整性合同不能降级。", 409)
                if active.input_manifest.get("gap_reanswer") != focus.get("gap_reanswer"):
                    raise DomainError("GAP_ANSWER_BINDING", "复答的固定范围已改变。", 409)
            from .dependencies import check_closure

            history = []
            previous = db.scalars(
                select(Turn)
                .where(
                    Turn.conversation_id == conv.id, Turn.status == "completed", Turn.id != turn.id
                )
                .order_by(Turn.created_at.desc())
                .limit(4)
            ).all()
            for old_turn in reversed(previous):
                fragments = []
                for block in db.scalars(
                    select(AnswerBlock)
                    .where(AnswerBlock.turn_id == old_turn.id)
                    .order_by(AnswerBlock.ordinal)
                ):
                    try:
                        check_closure(db, actor, [block.id])
                        fragments.append(block.content.get("text", "")[:600])
                    except DomainError:
                        fragments.append("[历史证据当前不可用]")
                history.append(
                    {
                        "question": old_turn.question[:600],
                        "answer_excerpt": "\n".join(fragments)[:1200],
                        "historical_not_new_evidence": True,
                    }
                )
            focus["history"] = history
            policies = frozen_policies(db, actor, namespace, turn.policy_versions)
            turn.status = "running"
        if (
            not run
            and focus.get("answer_completion_contract") == ANSWER_COMPLETION_CONTRACT
            and EVIDENCE_ANSWER_STATE in focus
        ):
            # A no-funding retry has no AgentRun or paid receipts. Reuse its
            # server-frozen factual plan rather than rerouting/retrieving a new
            # manifest or feeding a terminal suffix back into the Tutor cursor.
            plan = self.app.answers.evidence_plan(actor, turn_id)
            manifest_id = plan["retrieval_manifest_id"]
            for ordinal, block in enumerate(plan["blocks"]):
                self.app.answers.commit_block(actor, turn_id, block, manifest_id, ordinal)
            _, completion_gap = await self.app.answers.complete_question(
                actor, turn_id, manifest_id, None, processing_started_at=started
            )
            blocks = self.app.answers.committed(actor, turn_id)
            if completion_gap is not None:
                blocks.append(completion_gap)
            self.app.answers.commit(actor, turn_id, blocks, manifest_id)
            return
        old = self.app.runtime.stage(run[0], "orchestration") if run else None
        gap_reanswer = focus.get("gap_reanswer")
        if gap_reanswer:
            intent = IntentDecision(
                interpreted_intent=TaskType.IMPLEMENTATION,
                proposed_next_action="retrieve_and_tutor",
                confidence=1.0,
                project_source_ids=[gap_reanswer["source_id"]],
                topic_keys=gap_reanswer["topic_keys"],
                retrieval_topic_keys=[],
                topic_hints=gap_reanswer["topic_keys"],
                rationale_codes=["exact_gap_reanswer"],
            )
            decision = OrchestrationDecision(
                action="invoke_tutor",
                trigger_code="exact_gap_reanswer",
                principal_id=actor,
                authorized_namespace_refs=[namespace],
                focus_revision=revision,
                task_type=TaskType.IMPLEMENTATION,
                source_version_refs=intent.project_source_ids,
                topic_keys=intent.topic_keys,
                policy_version=policies["routing_policy"]["policy_id"],
            )
            if run and not old:
                self.app.runtime.save_stage(
                    *run,
                    "orchestration",
                    {
                        "intent": intent.model_dump(mode="json"),
                        "decision": decision.model_dump(mode="json"),
                    },
                )
        elif focus.get("preference_feedback_only") or focus.get("preference_clarification"):
            preference_trigger = (
                "preference_target_clarification"
                if focus.get("preference_clarification")
                else "explicit_preference_feedback"
            )
            intent = IntentDecision(
                interpreted_intent=TaskType.OTHER,
                proposed_next_action="ask_clarification"
                if focus.get("preference_clarification")
                else "direct_tool",
                reference_resolution="ambiguous"
                if focus.get("preference_clarification")
                else "resolved",
                confidence=1.0,
                topic_keys=focus.get("topic_keys", []),
                retrieval_topic_keys=focus.get("retrieval_topic_keys", focus.get("topic_keys", [])),
                topic_hints=focus.get("topic_hints", []),
                retrieval_scope_version="explicit_scope_v2",
                project_source_ids=focus.get("project_source_ids", []),
                rationale_codes=[preference_trigger],
            )
            decision = OrchestrationDecision(
                action="ask_clarification"
                if focus.get("preference_clarification")
                else "direct_tool",
                trigger_code=preference_trigger,
                principal_id=actor,
                authorized_namespace_refs=[namespace],
                focus_revision=revision,
                task_type=TaskType.OTHER,
                source_version_refs=intent.project_source_ids,
                topic_keys=intent.topic_keys,
                policy_version=policies["routing_policy"]["policy_id"],
            )
            if run and not old:
                self.app.runtime.save_stage(
                    *run,
                    "orchestration",
                    {
                        "intent": intent.model_dump(mode="json"),
                        "decision": decision.model_dump(mode="json"),
                    },
                )
        elif session_snapshot is not None:
            module = session_snapshot["module"]
            intent = IntentDecision(
                interpreted_intent=TaskType.IMPLEMENTATION,
                proposed_next_action="retrieve_and_tutor",
                reference_resolution="resolved",
                confidence=1.0,
                topic_keys=session_snapshot["task_spec"]["topic_keys"],
                project_source_ids=[module["source_id"]],
                rationale_codes=["explicit_accepted_session_module"],
            )
            decision = OrchestrationDecision(
                action="invoke_tutor",
                trigger_code="explicit_accepted_session_module",
                principal_id=actor,
                authorized_namespace_refs=[namespace],
                focus_revision=revision,
                task_type=TaskType.IMPLEMENTATION,
                source_version_refs=[module["source_id"]],
                topic_keys=intent.topic_keys,
                policy_version=policies["routing_policy"]["policy_id"],
            )
        elif old:
            intent = IntentDecision.model_validate(old["intent"])
            decision = OrchestrationDecision.model_validate(old["decision"])
        else:
            if focus.get(
                "answer_aspect_contract"
            ) == ANSWER_ASPECT_CONTRACT and inaccessible_knowledge_request(question):
                intent = IntentDecision(
                    interpreted_intent=TaskType.OTHER,
                    proposed_next_action="ask_clarification",
                    confidence=1.0,
                    reference_resolution="missing",
                    rationale_codes=["accessible_evidence_required"],
                )
                decision = OrchestrationDecision(
                    action="ask_clarification",
                    trigger_code="accessible_evidence_required",
                    principal_id=actor,
                    authorized_namespace_refs=[namespace],
                    focus_revision=revision,
                    task_type=TaskType.OTHER,
                    source_version_refs=[],
                    topic_keys=[],
                    policy_version=policies["routing_policy"]["policy_id"],
                )
            else:
                intent, decision = await self.app.coordinator.decide(
                    actor,
                    question,
                    focus,
                    revision,
                    policies,
                    run,
                    routing_contract_version=focus.get("routing_contract_version"),
                    retrieval_context_contract=focus.get("retrieval_context_contract"),
                )
            if run:
                self.app.runtime.save_stage(
                    *run,
                    "orchestration",
                    {
                        "intent": intent.model_dump(mode="json"),
                        "decision": decision.model_dump(mode="json"),
                    },
                )
        # Routing may propose an action, but cannot discard or manufacture the
        # user's explicit navigation target. Recompute on durable resume too.
        path_request = (
            None
            if session_snapshot is not None
            else navigation_path_request(
                question,
                contract=focus.get("source_path_contract"),
                guard=focus.get("source_path_guard"),
                locate=intent.interpreted_intent == TaskType.LOCATE,
            )
        )
        path_conflict = bool(
            path_request
            and (
                path_request["status"] != "exact"
                or intent.interpreted_intent != TaskType.LOCATE
                or decision.action != "direct_tool"
            )
        )
        manifest_id = None
        completion_assessment = None
        if decision.trigger_code == "preference_target_clarification":
            blocks = [
                {
                    **self.app.answers.action("clarify"),
                    "text": focus["preference_clarification"],
                    "preference_update_applied": False,
                    "competence_verified": False,
                }
            ]
        elif decision.trigger_code == "explicit_preference_feedback":
            blocks = [
                {
                    "kind": "learning_action",
                    "text": "已记录这次偏好更新；后续讲解按所选作用域和期限生效，未修改学习能力判断。",
                    "preference_event_ids": focus["preference_event_ids"],
                    "learning_preferences": focus["learning_preferences"],
                    "checked": "server_template",
                    "publication_ids": [],
                    "fact_atom_ids": [],
                    "competence_verified": False,
                }
            ]
        elif decision.trigger_code == "accessible_evidence_required":
            blocks = [
                {
                    **self.app.answers.action("gap", ["accessible_evidence_required"]),
                    "gap_category": "authorization",
                    "text": "当前授权范围内没有可用于回答的证据。你可以提供自己有权访问的材料。",
                }
            ]
        elif path_conflict and path_request is not None:
            blocks = [
                source_path_gap(
                    {
                        "status": "clarification_required",
                        "reason": path_request.get(
                            "reason", "requested_source_navigation_intent_conflict"
                        ),
                    },
                    guard=focus.get("source_path_guard"),
                )
            ]
        elif decision.action == "ask_clarification":
            blocks = [self.app.answers.action("clarify")]
        elif decision.action == "start_background_job":
            blocks = [self.app.answers.action("build")]
            if focus.get("preferences", {}).get("auto_build") and run:
                with self.db.read() as db:
                    funding_id = db.get(AgentRun, run[0]).funding_id
                repository = re.search(r"https://github\.com/[\w.-]+/[\w.-]+", question)
                try:
                    build_id = self.app.build.start(
                        actor,
                        KnowledgeBuildInput(
                            namespace_id=namespace,
                            funding_id=funding_id,
                            question=question,
                            repository_url=repository.group(0) if repository else None,
                            jd_text=question
                            if intent.interpreted_intent == TaskType.IMPORT
                            else None,
                            research_scope=(
                                "project"
                                if intent.interpreted_intent == TaskType.IMPORT or repository
                                else "focused"
                            )
                            if focus.get("routing_contract_version")
                            in {
                                ROUTING_CONTRACT_VERSION,
                                LEARNING_ROUTING_CONTRACT,
                                SCOPED_QUESTION_ROUTING_CONTRACT,
                            }
                            else None,
                            topic_keys=intent.topic_keys,
                            call_cap=focus["preferences"]["build_call_cap"],
                            idempotency_key="chat-build:" + turn_id,
                        ),
                    )
                    blocks[0]["details"] = ["authorized_background_build", build_id]
                    blocks[0]["text"] = (
                        "已按会话中预先授权的范围和预算建立知识构建任务，可继续查询已有知识或查看任务进度。"
                    )
                    blocks[0]["background_run_id"] = build_id
                except DomainError as exc:
                    blocks[0]["details"] = [exc.code]
        elif decision.action == "invoke_coding":
            blocks = [self.app.answers.action("practice")]
        elif intent.interpreted_intent == TaskType.DISPUTE:
            blocks = [self.app.answers.action("gap", ["submit_dispute_with_target_id"])]
        elif intent.interpreted_intent == TaskType.PROGRESS:
            with self.db.read() as db:
                runs = db.scalars(
                    select(AgentRun)
                    .where(AgentRun.principal_id == actor)
                    .order_by(AgentRun.created_at.desc())
                    .limit(10)
                ).all()
                rows = [
                    {
                        "run_id": r.id,
                        "workflow": r.workflow,
                        "status": r.status,
                        "calls_consumed": r.calls_consumed,
                        "call_cap": r.call_cap,
                    }
                    for r in runs
                ]
            blocks = [
                {
                    "kind": "learning_action",
                    "text": "最近任务状态已列出。",
                    "tasks": rows,
                    "checked": "server_template",
                    "publication_ids": [],
                    "fact_atom_ids": [],
                }
            ]
        else:
            frozen = self.app.runtime.stage(run[0], "retrieval") if run else None
            if session_snapshot is not None:
                with self.db.read() as db:
                    bound_turn = db.get(Turn, turn_id)
                    active = db.get(AgentRun, run[0]) if run else None
                    from .session_turn import reviewed_evidence

                    evidence = reviewed_evidence(self.app, db, actor, bound_turn, active)
                if frozen and frozen != evidence:
                    raise DomainError("SESSION_TUTOR_BINDING", "导师固定证据清单已改变。", 409)
                if run and not frozen:
                    self.app.runtime.save_stage(*run, "retrieval", evidence)
            elif frozen:
                evidence = frozen
                from .dependencies import check_closure

                with self.db.read() as db:
                    check_closure(db, actor, [evidence["retrieval_manifest_id"]])
            else:
                pair_reviewed_facets = (
                    intent.interpreted_intent == TaskType.COMPARE
                    and focus.get("comparison_gate") in PAIRED_COMPARISON_CONTRACTS
                )
                spec = RetrievalTaskSpec(
                    turn_id=turn_id,
                    task_type=intent.interpreted_intent,
                    # Navigation is source-derived state, not newly user-authored
                    # text. Never append it to an unscoped model query. Current
                    # source selection still constrains local symbol retrieval;
                    # an unresolved symbol must remain a clarification/gap.
                    user_question=question,
                    focus_revision=revision,
                    max_evidence_tokens=32000
                    if pair_reviewed_facets
                    else 24000
                    if focus.get("question_coverage_contract") == QUESTION_COVERAGE_CONTRACT
                    and needs_coverage_selection(question)
                    else 12000,
                    topic_keys=(
                        intent.retrieval_topic_keys
                        if intent.retrieval_topic_keys is not None
                        else intent.topic_keys
                    ),
                    topic_hints=intent.topic_hints,
                    retrieval_scope_version=intent.retrieval_scope_version or "explicit_scope_v2",
                    project_source_ids=intent.project_source_ids,
                    selected_routing_policy_version=policies["routing_policy"]["policy_id"],
                    **(
                        {
                            "pinned_publication_ids": gap_reanswer.get(
                                "evidence_publication_ids", gap_reanswer["publication_ids"]
                            ),
                            "freshness_policy": "explicit_pinned",
                        }
                        if gap_reanswer
                        else {}
                    ),
                )
                evidence = await self.app.retrieval.retrieve(
                    actor,
                    spec,
                    run,
                    policies["retrieval_recipe"]["content"],
                    policies["retrieval_recipe"]["policy_id"],
                    local_navigation=(
                        focus.get("code_focus")
                        if intent.interpreted_intent == TaskType.LOCATE
                        and _REFERENCE_LOCATION.fullmatch(question)
                        else None
                    ),
                    navigation_contract=focus.get("source_navigation_contract"),
                    source_path_contract=focus.get("source_path_contract"),
                    source_path_guard=focus.get("source_path_guard"),
                    retrieval_context_contract=focus.get("retrieval_context_contract"),
                    **({"paired_topic_keys": intent.topic_keys} if pair_reviewed_facets else {}),
                    question_coverage_contract=focus.get("question_coverage_contract")
                    if not pair_reviewed_facets
                    else None,
                    **(
                        {"answer_checker_contract": focus[ORDINARY_CHECKER_KEY]}
                        if ORDINARY_CHECKER_KEY in focus
                        else {}
                    ),
                )
                if run:
                    self.app.runtime.save_stage(*run, "retrieval", evidence)
            manifest_id = evidence["retrieval_manifest_id"]
            if ORDINARY_CHECKER_KEY in focus and (
                run
                or intent.interpreted_intent in {TaskType.PLAN, TaskType.LOCATE, TaskType.COMPARE}
            ):
                # Paid retrieval is already durable in its run stage. A no-run
                # selection must instead commit atomically with its evidence
                # plan, or a crash before that plan would strand the turn on an
                # otherwise unused manifest and make its next attempt fail.
                with self.db.transaction() as db:
                    current_turn = locked(db, Turn, turn_id)
                    bind_ordinary_selection_in(db, actor, current_turn, manifest_id)
                    validate_feedback_turn_in(
                        db,
                        actor,
                        current_turn,
                        run=db.get(AgentRun, current_turn.run_id) if current_turn.run_id else None,
                    )
            if intent.interpreted_intent == TaskType.PLAN:
                pubs = [u["publication_id"] for u in evidence["knowledge_units"]]
                if not pubs:
                    blocks = [self.app.answers.action("gap", ["no_reviewed_plan_material"])]
                else:
                    saved_plan = self.app.runtime.stage(run[0], "plan.proposal") if run else None
                    proposal = saved_plan or self.app.learning.propose_plan(
                        actor,
                        pubs,
                        "learn_project" if intent.project_source_ids else "technical_design",
                        "基于已审核资料的学习路线",
                    )
                    if run and not saved_plan:
                        self.app.runtime.save_stage(*run, "plan.proposal", proposal)
                    blocks = [
                        {
                            "kind": "learning_action",
                            "text": "已提出学习计划，采用前请检查知识版本和验收条件；尚未修改当前计划。",
                            "plan_proposal": proposal,
                            "checked": "server_template",
                            "publication_ids": pubs,
                            "fact_atom_ids": [],
                            "citations": [],
                        }
                    ]
            elif intent.interpreted_intent == TaskType.LOCATE:
                blocks = self._navigate_source(
                    actor, turn_id, evidence, run, focus.get("source_navigation_contract")
                )
            elif intent.interpreted_intent == TaskType.COMPARE:
                comparison = await self.app.learning.compare(
                    actor,
                    evidence,
                    intent.topic_keys,
                    expected_sources=(
                        intent.project_source_ids
                        if focus.get("comparison_gate")
                        in {
                            "evidence-paired-v1",
                            COMPARISON_SCOPE_CONTRACT,
                            *PAIRED_COMPARISON_CONTRACTS,
                        }
                        else None
                    ),
                    scoped_conditions=focus.get("comparison_gate")
                    in {COMPARISON_SCOPE_CONTRACT, *PAIRED_COMPARISON_CONTRACTS},
                    diagnostics=focus.get("comparison_gate") in PAIRED_COMPARISON_CONTRACTS,
                    distinguish_multiple=focus.get("comparison_gate")
                    == MULTIVALUE_COMPARISON_CONTRACT,
                    show_side_coverage=focus.get("comparison_side_coverage") is True,
                )
                blocks = [comparison]
            else:
                # Old durable batch receipts remain readable, but new answers use actual
                # one-block requests. Commit each yield before asking for the next block.
                saved = self.app.runtime.stage(run[0], "answer.validated") if run else None
                if saved is not None and ORDINARY_CHECKER_KEY in focus:
                    raise DomainError(
                        "ORDINARY_CHECKER_BINDING", "新普通审核合同不能复用旧批量回答缓存。", 409
                    )
                if (
                    not run
                    and focus.get("answer_completion_contract") == ANSWER_COMPLETION_CONTRACT
                ):
                    # No model is called in evidence-only generation. Freeze the
                    # checked deterministic plan before the first durable block,
                    # including zero-fact evidence gaps, so every commit boundary
                    # can resume against the same selection and original question.
                    blocks = await self.app.answers.tutor(
                        actor,
                        question,
                        evidence,
                        policies,
                        focus,
                        None,
                        answer_contract=focus.get("answer_contract"),
                    )
                    plan = self.app.answers.freeze_evidence_plan(
                        actor,
                        turn_id,
                        question,
                        manifest_id,
                        blocks,
                        privacy_generation,
                        continuation=continuation_projection(intent, turn_id),
                    )
                    blocks, manifest_id = plan["blocks"], plan["retrieval_manifest_id"]
                elif saved:
                    blocks = saved["blocks"]
                else:
                    blocks = self.app.answers.committed(actor, turn_id)
                    completed_check = (
                        self.app.runtime.stage(run[0], ANSWER_COMPLETION_STAGE)
                        if run
                        and focus.get("answer_completion_contract") == ANSWER_COMPLETION_CONTRACT
                        else None
                    )
                    if completed_check is None:
                        async for block in self.app.answers.iter_tutor(
                            actor,
                            question,
                            evidence,
                            policies,
                            focus,
                            run,
                            start_ordinal=len(blocks),
                            answer_contract=focus.get("answer_contract"),
                        ):
                            self.app.answers.commit_block(
                                actor, turn_id, block, manifest_id, len(blocks), run
                            )
                            blocks.append(block)
                if focus.get("answer_completion_contract") == ANSWER_COMPLETION_CONTRACT:
                    for ordinal, block in enumerate(blocks):
                        self.app.answers.commit_block(
                            actor, turn_id, block, manifest_id, ordinal, run
                        )
                    (
                        completion_assessment,
                        completion_gap,
                    ) = await self.app.answers.complete_question(
                        actor, turn_id, manifest_id, run, processing_started_at=started
                    )
                    if completion_gap is not None:
                        blocks.append(completion_gap)
        self.app.answers.commit(actor, turn_id, blocks, manifest_id, run)
        if (
            not run
            and completion_assessment is not None
            and focus.get("evidence_continuity_contract") == EVIDENCE_CONTINUITY_CONTRACT
        ):
            # The terminal transaction already committed the gap, operational
            # observation and revision-fenced focus together with delivery.
            return
        gaps = [
            b
            for b in blocks
            if b.get("kind") == "evidence_gap" and b.get("gap_category", "evidence") == "evidence"
        ]
        if gaps:
            with self.db.transaction() as db:
                assert_personal_access(db, actor, expected_generation=privacy_generation)
                namespace_access(db, actor, namespace, True)
                frozen_policies(db, actor, namespace, turn.policy_versions)
                if not db.scalar(select(KnowledgeGap.id).where(KnowledgeGap.turn_id == turn_id)):
                    db.add(
                        KnowledgeGap(
                            namespace_id=namespace,
                            principal_id=actor,
                            turn_id=turn_id,
                            topic_keys=intent.topic_keys,
                            source_ids=intent.project_source_ids,
                            reason_codes=sorted(
                                {
                                    reason
                                    for b in gaps
                                    for reason in b.get("details", ["insufficient_evidence"])
                                }
                            ),
                            proposed_route="analysis" if intent.project_source_ids else "research",
                        )
                    )
        self.app.rollouts.observe_turn(
            turn_id, blocks, (time.monotonic() - started) * 1000, "completed"
        )
        with self.db.transaction() as db:
            current = locked(db, Conversation, conv.id)
            assert_personal_access(db, actor, expected_generation=privacy_generation)
            namespace_access(db, actor, current.namespace_id, True)
            frozen_policies(db, actor, current.namespace_id, turn.policy_versions)
            # Delayed answers cannot silently move a newer user focus.
            if current.revision == revision + 1:
                current.focus = continued_focus(
                    current.focus, continuation_projection(intent, turn_id), blocks
                )
        if run:
            result: dict[str, Any] = {"turn_id": turn_id, "retrieval_manifest_id": manifest_id}
            if completion_assessment is not None:
                result.update(
                    answer_completion_contract=ANSWER_COMPLETION_CONTRACT,
                    answer_coverage_status=completion_assessment["status"],
                    task_satisfied=completion_assessment["task_satisfied"],
                    answer_completion_assessment_hash=completion_assessment["assessment_hash"],
                    uncovered_questions=completion_assessment["uncovered_questions"],
                    **{
                        key: completion_assessment[key]
                        for key in (
                            "answered_aspects",
                            "unsupported_aspects",
                            "conclusion_status",
                            "safe_abstention",
                        )
                        if key in completion_assessment
                    },
                )
            if path_request:
                unresolved = sorted(
                    {
                        reason
                        for block in blocks
                        if block.get("kind") == "evidence_gap"
                        for reason in block.get(
                            "details", ["requested_source_navigation_unresolved"]
                        )
                    }
                )
                if any(block.get("navigation_read", {}).get("budget_limited") for block in blocks):
                    unresolved.append("source_navigation_page_budget_exhausted")
                result.update(
                    source_path_contract=SOURCE_PATH_CONTRACT,
                    task_satisfied=False if unresolved else None,
                    uncovered_questions=unresolved,
                    navigation_outcome=(
                        "not_completed" if unresolved else "source_observations_returned"
                    ),
                )
                if focus.get("source_path_guard"):
                    result["source_path_guard"] = focus["source_path_guard"]
                # A successfully delivered gap is not successful navigation.
                # Even valid raw spans do not certify a semantic task complete.
            if SESSION_TUTOR_KEY in focus:
                saved_result = self.app.runtime.stage(run[0], "result") or {}
                for key in ("tutor_answer_status", "tutor_answer_progress"):
                    if key in saved_result:
                        result[key] = saved_result[key]
                result.update(
                    {
                        SESSION_TUTOR_KEY: focus[SESSION_TUTOR_KEY],
                        "module_completed": False,
                        "competence_verified": False,
                    }
                )
                with self.db.transaction() as db:
                    active = leased(db, *run)
                    validate_session_tutor_in(db, actor, db.get(Turn, turn_id), run=active)
                    self.app.runtime.save_stage_in(db, active, "result", result)
            else:
                self.app.runtime.save_stage(*run, "result", result)

    def _navigate_source(
        self,
        actor: str,
        turn_id: str,
        evidence: dict[str, Any],
        run: tuple[str, int, str] | None,
        contract: str | None,
    ) -> list[dict[str, Any]]:
        if contract not in {None, SOURCE_NAVIGATION_CONTRACT}:
            raise DomainError("NAVIGATION_CONTRACT", "未知的源码定位合同版本。", 422)
        blocks: list[dict[str, Any]] = []
        total_pages, page_cap, symbol_page_cap = 0, 12, 5

        def append(block: dict[str, Any]) -> None:
            if contract:
                # Persist each successful page before another read. A nearby
                # oversized span, later cancellation or worker crash cannot
                # discard the already checked source observation. The commit
                # rechecks the lease, policy, source ACL and immutable ordinal.
                self.app.answers.commit_block(
                    actor, turn_id, block, evidence["retrieval_manifest_id"], len(blocks), run
                )
            blocks.append(block)

        path_resolution = None
        with self.db.read() as db:
            turn = db.get(Turn, turn_id)
            if turn.focus.get("source_path_contract") == SOURCE_PATH_CONTRACT:
                request = navigation_path_request(
                    turn.question,
                    contract=turn.focus.get("source_path_contract"),
                    guard=turn.focus.get("source_path_guard"),
                    locate=True,
                )
                if request and request["status"] != "exact" and turn.focus.get("source_path_guard"):
                    # This boundary also protects direct calls and staged
                    # legacy evidence. Do not inspect candidates or read files.
                    path_resolution = {
                        "status": "clarification_required",
                        "reason": request["reason"],
                    }
                elif request:
                    from .dependencies import require_object

                    selection = require_object(
                        db, actor, evidence["retrieval_manifest_id"], "selection_manifest"
                    )
                    payload = selection.payload
                    task = payload.get("task_spec", {})
                    if (
                        payload.get("source_path_contract") != SOURCE_PATH_CONTRACT
                        or payload.get("source_path_guard") != turn.focus.get("source_path_guard")
                        or payload.get("source_path_request") != request
                        or task.get("user_question") != turn.question
                        or task.get("turn_id") != turn.id
                        or task.get("task_type") != TaskType.LOCATE.value
                        or task.get("project_source_ids")
                        != turn.focus.get("source_path_source_ids")
                        or len(turn.focus.get("source_path_source_ids", [])) != 1
                    ):
                        raise DomainError("SOURCE_PATH_CONFLICT", "源码定位路径与当前任务不一致。")
                    source_ids = task.get("project_source_ids", [])
                    path_resolution = resolve_source_path(db, actor, source_ids, request)
                    if any(
                        symbol.get("path") != request.get("path")
                        or symbol.get("source_id") not in source_ids
                        for symbol in evidence["symbols"]
                    ):
                        raise DomainError("SOURCE_PATH_CONFLICT", "源码定位结果越出明确路径范围。")
        if path_resolution and path_resolution["status"] != "available":
            gap = source_path_gap(path_resolution, guard=turn.focus.get("source_path_guard"))
            if path_resolution["status"] == "missing_from_selected_snapshot":
                gap["source_id"] = source_ids[0]
            append(gap)
            return blocks
        for symbol in evidence["symbols"][:5]:
            if total_pages >= page_cap:
                break
            start, end = symbol["start_line"], symbol["end_line"]
            for page in range(1, (symbol_page_cap if contract else 1) + 1):
                if total_pages >= page_cap:
                    break
                if contract:
                    # A cancelled turn must stop before issuing another source
                    # read, not merely when committing its eventual answer.
                    with self.db.transaction() as db:
                        self.app.answers._turn(db, actor, turn_id, run)
                try:
                    read = self.app.sources.read_span(
                        actor,
                        symbol["source_id"],
                        symbol["path"],
                        start,
                        end if contract else min(end, start + 69),
                        "tutor",
                        turn_id,
                        run[0] if run else None,
                        **({"paginate": True} if contract else {}),
                    )
                except DomainError as exc:
                    if not contract or exc.code not in {
                        "READ_RANGE",
                        "READ_LIMIT",
                        "READ_LINE_LIMIT",
                    }:
                        # Access, hash, source identity and lease failures are
                        # never converted to a recoverable content-size gap.
                        raise
                    gap = self.app.answers.action("gap", ["source_navigation:" + exc.code])
                    gap.update(
                        text="所选源码范围未能完整读取；其他已核验片段保留，未读取部分仍未知。",
                        source_id=symbol["source_id"],
                        navigation_attempt=symbol,
                        navigation_contract=contract,
                        unread_range={"path": symbol["path"], "start_line": start, "end_line": end},
                    )
                    append(gap)
                    break
                total_pages += 1
                continuation = read.get("continuation") if contract else None
                block = {
                    "kind": "raw_source_observation",
                    "text": read["content"],
                    "navigation": symbol,
                    "source_id": symbol["source_id"],
                    "publication_ids": [],
                    "fact_atom_ids": [],
                    "conditions": [],
                    "citations": [read],
                    "checked": "source_position",
                    "label": "实际源码定位，非新机制语义批准",
                }
                if contract:
                    block.update(
                        navigation_contract=contract,
                        navigation_read={
                            "page": page,
                            "max_pages_per_symbol": symbol_page_cap,
                            "max_total_pages": page_cap,
                            "continuation": continuation,
                            "symbol_complete": continuation is None,
                            "budget_limited": bool(
                                continuation
                                and (page == symbol_page_cap or total_pages == page_cap)
                            ),
                        },
                    )
                append(block)
                if not continuation:
                    break
                start = continuation["start_line"]
        if not blocks:
            gap = self.app.answers.action("gap", ["symbol_not_found"])
            if path_resolution:
                gap.update(
                    text="当前所选快照包含指定路径，但未能在该路径定位所需符号；未定位部分仍未知，不表示整个仓库没有实现。",
                    details=["requested_source_symbol_unresolved"],
                    source_path_contract=SOURCE_PATH_CONTRACT,
                    source_path_resolution=path_resolution,
                    source_id=source_ids[0],
                    task_satisfied=False,
                )
            append(gap)
        return blocks

    def mark_failed(self, turn_id, code):
        with self.db.transaction() as db:
            interrupt_turn(db, turn_id, code)
            turn = db.get(Turn, turn_id)
            interrupted = turn is not None and turn.status == "interrupted"
        if interrupted:
            self.app.rollouts.observe_turn(turn_id, [], 0, "interrupted")

    @staticmethod
    def _owned(db, actor, identifier, lock=False):
        row = locked(db, Conversation, identifier) if lock else db.get(Conversation, identifier)
        if not row or row.principal_id != actor:
            raise DomainError("NOT_FOUND", "会话不存在。", 404)
        namespace_access(db, actor, row.namespace_id)
        return row
