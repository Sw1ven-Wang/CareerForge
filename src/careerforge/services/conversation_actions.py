"""Validate a bounded conversation proposal and apply actions under owner/CAS fences."""

from sqlalchemy import select

from ..contracts import TurnInput
from ..contracts_jd_v166 import JDExtractionInput, JDImportInput
from ..db import locked
from ..errors import DomainError
from ..models import AgentRun, FundingAgreement, Principal
from ..models_dialogue import ConversationActionReceipt, GoalAddendum
from ..util import fingerprint
from .dialogue_routing import bound_plan
from .run_pause import request_pause_in, resume_family, resume_precheck
from .runtime import TERMINAL, lock_run
from .workflow_status import ONBOARDING, workflow_runs

_CLARIFICATION = {
    "target_to_modify": "你想修改原 JD、学习目标，还是学习计划？",
    "new_jd_or_topic": ONBOARDING,
    "scope_or_confirmation": "请明确你要处理的任务及授权范围。",
}


class ActionExecutor:
    def __init__(self, app):
        self.app, self.db, self.workflows = app, app.db, app.workflows

    def dispatch(
        self,
        actor,
        conversation_id,
        message,
        *,
        forced_actions=None,
        workflow_id=None,
        expected_workflow_revision=None,
        expected_goal=None,
        expected_goal_revision=None,
        command_source=None,
        legacy_goal_revision=False,
        learning_input=None,
    ):
        from ..contracts_practice_dialogue_v1 import PracticeDialogueMessageV1

        if isinstance(message, PracticeDialogueMessageV1):
            expected = [(message.intent.type, {})]
            if forced_actions is not None and forced_actions != expected:
                raise DomainError("PRACTICE_CALLER_CONTRACT", "动作必须与本人明确输入一致。", 422)
            forced_actions = expected
            workflow_id = message.workflow_id
            expected_workflow_revision = message.expected_workflow_revision
        request = {
            "message": message.model_dump(
                mode="json",
                exclude=(
                    (
                        {"expected_focus_revision"}
                        if expected_workflow_revision is not None
                        and not isinstance(message, PracticeDialogueMessageV1)
                        else set()
                    )
                    # Preserve byte-equivalent historical request identities
                    # when new admission choices have their original defaults.
                    | ({"research_question"} if message.research_question is None else set())
                    | ({"auto_evidence_retry"} if not message.auto_evidence_retry else set())
                    | ({"submission"} if message.submission is None else set())
                    | ({"module_ack"} if message.module_ack is None else set())
                    | ({"publication_ids"} if not message.publication_ids else set())
                ),
            ),
            "actions": forced_actions,
            "workflow_id": workflow_id,
            "workflow_revision": expected_workflow_revision,
            "expected_goal": expected_goal,
        }
        if expected_goal_revision is not None:
            request["expected_goal_revision"] = expected_goal_revision
        if command_source is not None:
            request["command_source"] = command_source
        if learning_input is not None:
            request["learning_input"] = learning_input.model_dump(mode="json")
        request_hash = fingerprint(request)
        with self.db.transaction() as db:
            # Match the existing answer admission order, including independent attempts.
            locked(db, Principal, actor)
            conv = self.workflows.conversation(db, actor, conversation_id, lock=True)
            old = db.scalar(
                select(ConversationActionReceipt).where(
                    ConversationActionReceipt.conversation_id == conv.id,
                    ConversationActionReceipt.idempotency_key == message.idempotency_key,
                )
            )
            if old is not None:
                if old.request_hash != request_hash:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "相同消息键对应不同内容。", 409)
                if old.workflow_id:
                    self.workflows.owned_in(db, conv, old.workflow_id)
                return old.result
            if (
                expected_workflow_revision is None or isinstance(message, PracticeDialogueMessageV1)
            ) and conv.revision != message.expected_focus_revision:
                raise DomainError("REVISION_CONFLICT", "会话焦点已变化，请刷新后重试。", 409)
            workflow = self.workflows.owned_in(db, conv, workflow_id)
            if expected_workflow_revision is not None and (
                workflow is None or workflow.revision != expected_workflow_revision
            ):
                raise DomainError("REVISION_CONFLICT", "工作流版本已变化，请刷新后重试。", 409)
            if expected_goal is not None and (
                workflow is None or workflow.goal_version_id != expected_goal
            ):
                raise DomainError("REVISION_CONFLICT", "目标版本已变化。", 409)
            for source_id in message.source_ids or (
                [message.source_id] if message.source_id else []
            ):
                self.app.sources.get(db, actor, source_id)
            if message.authorization_ref:
                self._funding_in(db, actor, conv.namespace_id, message.authorization_ref)
            actions = (
                forced_actions
                if forced_actions is not None
                else self.app.coordinator.dialogue_actions(message)
            )
            if (
                forced_actions is None
                and workflow is not None
                and workflow.milestone == "awaiting_language_choice"
            ):
                from .learning_language import bare_language_choice

                if bare_language_choice(message.text):
                    actions = [("select_implementation_language", {})]
                elif actions is None:
                    # Stay in the explicit onboarding choice; a free-form reply
                    # must not enter an auto-build/paid-answer path by accident.
                    actions = [("query_progress", {})]
            if actions and any(arguments.get("foreign") for _, arguments in actions):
                raise DomainError("NOT_FOUND", "工作流不存在。", 404)
            if (
                actions is None
                and workflow is not None
                and workflow.learning_graph_version
                and workflow.target.get("accepted_plan")
            ):
                actions = [("learning_followup", {})]
            if actions is None:
                return self._answer_in(db, actor, conv, workflow, message, request_hash)
            # Lock the single selected grant before ANY action can hold Plan/Profile,
            # including a compound module-select then teach request.
            if (workflow and workflow.learning_graph_version) or any(
                name == "start_learning_session" for name, _ in actions
            ):
                initial_admission = any(name == "start_learning_session" for name, _ in actions)
                selected_funding = message.authorization_ref or (
                    workflow.target.get("funding_ref")
                    if workflow
                    and not initial_admission
                    and not isinstance(message, PracticeDialogueMessageV1)
                    else None
                )
                if selected_funding:
                    self._funding_in(db, actor, conv.namespace_id, selected_funding)
                    locked(db, FundingAgreement, selected_funding)
            plan = bound_plan(conv, workflow, message, actions)
            frozen = plan.model_dump(mode="json")
            # JD content is stored once in the immutable document, not duplicated into event logs.
            for step in frozen["actions"]:
                value = step["action"]
                if value["type"] == "import_jd":
                    value["text_sha256"] = fingerprint(value.pop("text"))
            row = self._receipt_in(db, conv, workflow, message, request_hash, frozen)
            executed, reply, refs, applied, response_fields = [], [], [], set(), {}
            for index, step in enumerate(plan.actions):
                sub = {
                    "receipt_id": f"{row.id}:{index}",
                    "action_index": index,
                    "action_type": step.action.type,
                    "workflow_id": workflow.id if workflow else None,
                }
                if not set(step.depends_on) <= applied:
                    executed.append(
                        {**sub, "status": "rejected", "failure_code": "ACTION_DEPENDENCY"}
                    )
                    continue
                try:
                    with db.begin_nested():
                        workflow, result = self._apply_in(
                            db,
                            actor,
                            conv,
                            workflow,
                            message,
                            step.action,
                            receipt=row,
                            action_index=index,
                            expected_goal_revision=expected_goal_revision,
                            command_source=command_source,
                            legacy_goal_revision=legacy_goal_revision,
                            learning_input=learning_input,
                        )
                        # Readers of a freshly created V2 require the actual indexed success,
                        # even inside a compound command. Savepoint rollback removes it too.
                        row.result = {
                            "action_receipts": [
                                *executed,
                                {
                                    **sub,
                                    "status": result.get("status", "applied"),
                                    "workflow_id": workflow.id if workflow else None,
                                    **result,
                                },
                            ]
                        }
                        db.flush()
                        if step.action.type in {
                            "add_learning_objective",
                            "add_learning_topic",
                        } and result.get("new_goal_version_id"):
                            from .learning_language import inherit_language_in

                            inherit_language_in(db, actor, workflow)
                        self.app.session_workflows.record_action_in(
                            db, conv, workflow, row, index, step.action.type, message, result
                        )
                        if (
                            workflow
                            and workflow.learning_graph_version
                            and result.get("request_id")
                        ):
                            execution = self.app.session_workflows.current(db, workflow)
                            if execution and execution.workflow_type == "learning_session":
                                self.app.learning_workflows.wake_in(
                                    db, workflow, execution, "action"
                                )
                except DomainError as exc:
                    executed.append({**sub, "status": "failed", "failure_code": exc.code})
                    row.result = {"action_receipts": list(executed)}
                    reply.append(exc.message)
                    continue
                outcome = result.pop("status", "applied")
                if outcome == "applied":
                    applied.add(step.step_id)
                executed.append(
                    {
                        **sub,
                        "status": outcome,
                        "workflow_id": workflow.id if workflow else None,
                        **result,
                    }
                )
                refs.extend(result.get("run_refs", []))
                if result.get("turn_id"):
                    response_fields.update(turn_id=result["turn_id"], run_id=result.get("run_id"))
                if result.get("reply"):
                    reply.append(result["reply"])
            row.workflow_id = workflow.id if workflow else None
            result = {
                "lane": "control",
                "receipt_id": row.id,
                "recognized_intent": plan.actions[0].action.type,
                "action_receipts": executed,
                "reply": "\n".join(reply),
                "revision": conv.revision,
                "workflow_id": row.workflow_id,
                "run_refs": list(dict.fromkeys(refs)),
                **response_fields,
            }
            e = self.workflows.emit_in(
                db,
                conv,
                "workflow.action",
                f"action:{row.id}",
                workflow,
                receipt_id=row.id,
                action_types=[item["action_type"] for item in executed],
            )
            row.result = {**result, "cursor": e.seq}
            return row.result

    @staticmethod
    def _receipt_in(db, conv, workflow, message, request_hash, plan):
        receipt = ConversationActionReceipt(
            conversation_id=conv.id,
            workflow_id=workflow.id if workflow else None,
            idempotency_key=message.idempotency_key,
            request_hash=request_hash,
            action_plan_hash=fingerprint(plan),
            plan=plan,
            result={},
        )
        db.add(receipt)
        db.flush()
        return receipt

    def _answer_in(self, db, actor, conv, workflow, message, request_hash):
        if workflow is not None and workflow.control_state in {
            "cancelled",
            "cancel_requested",
            "resume_requested",
        }:
            raise DomainError("WORKFLOW_NOT_ACTIVE", "当前工作流不能继续教学。", 409)
        if workflow is not None and workflow.control_state in {"paused", "pause_requested"}:
            raise DomainError("WORKFLOW_PAUSED", "工作流已暂停，恢复后才能继续教学。", 409)
        if workflow is not None and workflow.target.get("accepted_plan"):
            result = self.app.continuation.tutor(
                db,
                actor,
                conv,
                workflow,
                message,
                question=message.text,
            )
        else:
            result = self.app.conversations._submit(
                actor,
                conv.id,
                TurnInput(
                    question=message.text,
                    expected_revision=conv.revision,
                    idempotency_key="dialogue:" + fingerprint(message.idempotency_key),
                    funding_id=message.authorization_ref,
                ),
                ordinary_entry=True,
                db=db,
            )
            if workflow and result["run_id"]:
                self.workflows.link_in(db, workflow, db.get(AgentRun, result["run_id"]), "tutor")
        row = self._receipt_in(db, conv, workflow, message, request_hash, {"lane": "answer"})
        row.result = {
            **result,
            "lane": "answer",
            "recognized_intent": "answer",
            "receipt_id": row.id,
            "workflow_id": workflow.id if workflow else None,
            "action_receipts": [],
            "run_refs": [result["run_id"]] if result.get("run_id") else [],
        }
        return row.result

    @staticmethod
    def _funding_in(db, actor, namespace_id, identifier):
        funding = db.get(FundingAgreement, identifier)
        if funding is None or funding.payer_id != actor or funding.namespace_id != namespace_id:
            raise DomainError("NOT_FOUND", "找不到当前会话可用的授权。", 404)
        return funding

    def _apply_in(
        self,
        db,
        actor,
        conv,
        workflow,
        message,
        action,
        *,
        receipt=None,
        action_index=None,
        expected_goal_revision=None,
        command_source=None,
        legacy_goal_revision=False,
        learning_input=None,
    ):
        name = action.type
        from ..contracts_conversation_actions_v4 import PracticeAction

        if isinstance(action, PracticeAction):
            if workflow is None:
                raise DomainError("NOT_FOUND", "练习工作流不存在。", 404)
            return workflow, self.app.practice_workflows.command_in(
                db, actor, conv, workflow, message, receipt, action_index
            )
        if name in {
            "use_reviewed_material",
            "submit_practice_answer",
            "assess_practice",
            "advance_learning_module",
        }:
            from .desk_learning_actions import apply_learning_action

            return apply_learning_action(self.app, db, actor, conv, workflow, message, name)
        if name == "select_learning_module" and (
            workflow is None or not workflow.learning_graph_version
        ):
            raise DomainError("LEARNING_ADMISSION_REQUIRED", "请先明确准入学习计划。", 409)
        if name == "start_learning_session":
            from ..contracts_conversation_actions_v3 import LearningSessionAdmissionV1

            if not isinstance(learning_input, LearningSessionAdmissionV1):
                raise DomainError("LEARNING_CALLER_CONTRACT", "新学习准入需要明确版本化输入。", 422)
            return self.app.learning_workflows.admit_accepted_in(
                db, actor, conv, receipt, action_index, learning_input, message
            )
        if (
            workflow
            and workflow.learning_graph_version
            and name
            in {
                "accept_plan",
                "start_tutor",
                "learning_followup",
                "authorize_tutor_request",
                "select_learning_module",
            }
        ):
            if workflow.control_state != "active":
                raise DomainError("WORKFLOW_NOT_ACTIVE", "请先恢复当前学习工作流。", 409)
            if conv.focus.get("active_workflow_id") != workflow.id:
                raise DomainError("WORKFLOW_FOCUS", "请先切回当前学习工作流。", 409)
            requests = self.app.learning_requests
            funding_id = message.authorization_ref or workflow.target.get("funding_ref")
            if funding_id and name != "select_learning_module":
                self._funding_in(db, actor, conv.namespace_id, funding_id)
                locked(db, FundingAgreement, funding_id)
            if name == "accept_plan":
                result = self.app.continuation.accept_plan(db, actor, conv, workflow, message)
                if result.get("status") == "already_applied":
                    initial = requests.initial_in(db, workflow)
                    if initial is None:
                        raise DomainError("LEARNING_REQUEST_BINDING", "原采纳缺少学习请求。", 409)
                    return workflow, {**requests.response(initial), "status": "already_applied"}
                if not result.get("version_id"):
                    return workflow, result
                request = requests.record_in(
                    db, conv, workflow, receipt, action_index, message, initial=True
                )
                return workflow, {**result, **requests.response(request)}
            if name == "authorize_tutor_request":
                from ..contracts_conversation_actions_v3 import TutorRequestAuthorizationV1

                if not isinstance(learning_input, TutorRequestAuthorizationV1):
                    raise DomainError("LEARNING_CALLER_CONTRACT", "请明确授权原请求。", 422)
                return workflow, requests.authorize_in(
                    db, conv, workflow, receipt, action_index, learning_input
                )
            if name == "select_learning_module":
                from .module_selection import select_module_in, selection_from_message_in

                if learning_input is None:
                    learning_input = selection_from_message_in(
                        self.app, db, actor, conv, workflow, message, action.selector
                    )
                return workflow, select_module_in(
                    self.app, db, actor, conv, workflow, receipt, action_index, learning_input
                )
            initial = requests.initial_in(db, workflow)
            if name == "start_tutor" and initial and initial.action_receipt_id == receipt.id:
                return workflow, requests.response(initial)
            execution = self.app.session_workflows.current(db, workflow)
            if execution is None or execution.workflow_type != "learning_session":
                raise DomainError("LEARNING_HANDOFF_PENDING", "等待知识图真实完成交接。", 409)
            self.app.learning_workflows.validate_in(db, workflow, execution)
            request = requests.record_in(
                db,
                conv,
                workflow,
                receipt,
                action_index,
                message,
                explicit_question=message.text if name == "learning_followup" else None,
            )
            return workflow, requests.response(request)
        if name == "query_progress":
            view = self.workflows.projector.current(db, actor, conv, workflow)
            active = "、".join(
                f"{stage['label']} {stage['status']}" for stage in view["active_stages"]
            )
            reply = (
                view.get("language_question")
                or view.get("onboarding")
                or (
                    f"阶段 {view['milestone']}，控制状态 {view['control_state']}，"
                    f"发布 {view['publication_count']}，已用 {view['calls_consumed']}/{view['call_cap']} 次。"
                    + (f" 当前 {active}。" if active else "")
                )
            )
            return workflow, {"reply": reply, "view": view, "run_refs": view["run_refs"]}
        if name == "ask_clarification":
            return workflow, {"reply": _CLARIFICATION[action.slot]}
        if name == "switch_focus":
            if action.mode == "selected":
                if workflow is None:
                    raise DomainError("NOT_FOUND", "工作流不存在。", 404)
                self.workflows.focus_in(db, conv, workflow)
            else:
                workflow = self.workflows.create_in(db, conv, parent=workflow)
            self.workflows.emit_in(
                db, conv, "workflow.focus_changed", f"focus:{conv.revision}", workflow
            )
            return workflow, {"reply": "已切换会话焦点。"}
        if name == "import_jd":
            if (
                workflow is None
                or workflow.source_jd_revision_id
                or workflow.goal_version_id
                or workflow.target.get("entry")
            ):
                workflow = self.workflows.create_in(db, conv, parent=workflow)
            doc = self.app.jd.import_jd(
                actor,
                JDImportInput(
                    namespace_id=conv.namespace_id,
                    jd_id=f"dialogue-{workflow.id}",
                    raw_text=action.text,
                ),
                db=db,
            )
            workflow.source_jd_revision_id = doc["jd_revision_id"]
            from .learning_language import LANGUAGE_CONTRACT

            workflow.target = {**workflow.target, "learning_language_contract": LANGUAGE_CONTRACT}
            self._set_wait(workflow, "authorize_extraction", "awaiting_extraction")
            if message.authorization_ref:
                extraction = self._extract(db, actor, workflow, message.authorization_ref)
                return workflow, {**extraction, "jd_revision_id": doc["jd_revision_id"]}
            return workflow, {
                "jd_revision_id": doc["jd_revision_id"],
                "reply": "已保存 JD 原文，确认授权后提取。",
            }
        if name == "start_extraction":
            if workflow is None or not workflow.source_jd_revision_id:
                raise DomainError("JD_REQUIRED", "请先导入 JD。", 409)
            return workflow, self._extract(db, actor, workflow, message.authorization_ref)
        if name == "select_implementation_language":
            import re

            from .learning_language import select_language_in

            text = re.sub(
                r"^(?:/language\s+|(?:后端|实现语言)(?:选择|使用|用|选|是|[:：])\s*)",
                "",
                message.text.strip(),
                flags=re.I,
            )
            languages = [part for part in re.split(r"[,，、\s]+", text.rstrip("。.!！")) if part]
            try:
                return workflow, select_language_in(
                    self.app, db, actor, conv, workflow, languages, receipt, action_index
                )
            except ValueError as exc:
                raise DomainError("LANGUAGE_INPUT", str(exc), 422) from exc
        if name in {"set_learning_topic", "set_repository"}:
            if name == "set_repository" and not action.target.startswith("https://github.com/"):
                raise DomainError("REPOSITORY_REQUIRED", "请输入明确的 GitHub 仓库地址。", 422)
            if (
                workflow is None
                or workflow.source_jd_revision_id
                or workflow.goal_version_id
                or workflow.target.get("entry")
            ):
                workflow = self.workflows.create_in(db, conv, parent=workflow)
            workflow.target = {"entry": name, "target": action.target}
            self._set_wait(workflow, "confirm_scope", "awaiting_goal_confirmation")
            return workflow, {
                "reply": (
                    "已保存指定项目地址，尚未完成知识入库。请明确学习范围（也可从项目架构和核心流程开始），"
                    "并确认来源与构建额度；之后会固定源码版本、提取知识、独立审核，通过后发布。"
                    if name == "set_repository"
                    else "已保存学习方向，请确认来源与构建额度。"
                )
            }
        if name == "add_learning_objective" or (
            name == "add_learning_topic"
            and workflow is not None
            and not legacy_goal_revision
            and workflow.orchestration_engine == "langgraph"
            and workflow.goal_version_id is not None
        ):
            from .goal_amendments import create_goal_amendment_in, dialogue_command_source

            if workflow is None or not workflow.goal_version_id:
                raise DomainError("GOAL_REQUIRED", "新增版本需要已有的岗位目标。", 409)
            if workflow.orchestration_engine != "langgraph":
                raise DomainError("GRAPH_ENGINE", "新增目标版本需要显式启用 LangGraph 会话。", 409)
            depth = getattr(action, "target_depth", None)
            priority = getattr(action, "learning_priority", "unspecified")
            spans = {
                key: getattr(action, key).model_dump() if getattr(action, key, None) else None
                for key in ("target_span", "depth_span", "priority_span")
            }
            source = command_source or dialogue_command_source(
                message.text, action.target, target_depth=depth, learning_priority=priority, **spans
            )
            updated, outcome = create_goal_amendment_in(
                self.app,
                db,
                actor,
                conv,
                workflow,
                receipt,
                action_index,
                technology=action.target,
                command_source=source,
                target_depth=depth,
                learning_priority=priority,
                expected_goal_revision=expected_goal_revision,
            )
            updated.orchestration_engine = "langgraph"
            updated.session_graph_version = "knowledge-session-v2"
            if (
                self.app.session_workflows.active(db, workflow)
                or any(run.status not in TERMINAL for run in self._roots_in(db, workflow))
            ) and workflow.control_state == "active":
                self.app.workflow_controls.apply_in(
                    db, conv, workflow, receipt, action_index, "pause"
                )
            self.workflows.focus_in(db, conv, updated)
            return updated, outcome
        if name == "add_learning_topic":
            if workflow is None:
                raise DomainError("GOAL_REQUIRED", "请先建立一个学习方向。", 409)
            if legacy_goal_revision and workflow.goal_version_id:
                from ..models import GoalVersion

                version = db.get(GoalVersion, workflow.goal_version_id)
                if version and version.origin_contract == "command-goal-v1":
                    raise DomainError(
                        "GOAL_REVISION_CONTRACT",
                        "新增版本必须使用 goal-amendments 提交目标版本与修订号。",
                        409,
                    )
            updated = self._add_topic(db, actor, conv, workflow, action.target)
            return updated, {
                "goal_addendum_id": updated.target["goal_addendum_id"],
                "reply": f"已记录 {action.target} 为你的新增学习目标，等待确认受影响的范围。",
            }
        if name == "propose_goal_revision":
            return workflow, {"reply": _CLARIFICATION["target_to_modify"]}
        if workflow is not None and (
            name == "cancel_workflow"
            or (name == "request_pause" and workflow.orchestration_engine == "langgraph")
            or (name == "resume_workflow" and workflow.orchestration_engine == "langgraph")
        ):
            import re

            if (
                name == "resume_workflow"
                and re.fullmatch(
                    r"\s*(?:请)?继续(?:刚才|之前|原来|原先)(?:的)?.{0,18}[。？?！!；;]?\s*",
                    message.text,
                )
                and workflow.control_state not in {"paused", "pause_requested"}
            ):
                ancestor = workflow
                for _ in range(32):
                    if not ancestor.parent_workflow_id:
                        break
                    ancestor = self.workflows.owned_in(db, conv, ancestor.parent_workflow_id)
                    if ancestor.control_state in {"paused", "pause_requested"}:
                        workflow = ancestor
                        break
            result = self.app.workflow_controls.apply_in(
                db,
                conv,
                workflow,
                receipt,
                action_index,
                {
                    "request_pause": "pause",
                    "resume_workflow": "resume",
                    "cancel_workflow": "cancel",
                }[name],
                message,
            )
            if (
                name == "resume_workflow"
                and workflow.session_graph_version != "evolution-session-v1"
            ):
                # Evolution is an independent background child, not a replacement
                # for the caller's learning focus. Explicit focus switching remains
                # available as a separate command; legacy resume is unchanged.
                self.workflows.focus_in(db, conv, workflow)
            return workflow, result
        return self._workflow_action_in(db, actor, conv, workflow, message, action)

    @staticmethod
    def _set_wait(workflow, waiting, milestone):
        workflow.target = {**workflow.target, "waiting_user_action": waiting}
        workflow.milestone = milestone
        workflow.revision += 1

    def _extract(self, db, actor, workflow, funding_id):
        if not funding_id:
            return {"status": "waiting_confirmation", "reply": "请先确认 JD 提取授权。"}
        if workflow.control_state != "active":
            raise DomainError("WORKFLOW_PAUSED", "恢复工作流后才可提取。", 409)
        result = self.app.jd.start_extraction(
            actor,
            JDExtractionInput(
                namespace_id=workflow.namespace_id,
                funding_id=funding_id,
                jd_revision_id=workflow.source_jd_revision_id,
                idempotency_key="dialogue.extract:" + workflow.id,
            ),
            db=db,
        )
        self.workflows.link_in(db, workflow, db.get(AgentRun, result["run_id"]), "extract")
        self._set_wait(workflow, None, "extracting")
        return {"run_refs": [result["run_id"]], "reply": "已保存 JD，需求提取已进入队列。"}

    def _add_topic(self, db, actor, conv, parent, topic):
        old = parent.target
        draft = self.workflows.create_in(db, conv, parent=parent)
        draft.goal_version_id = parent.goal_version_id
        draft.source_jd_revision_id = parent.source_jd_revision_id
        previous = old.get("goal_addendum_id")
        topics = list(dict.fromkeys([*old.get("user_topics", []), topic]))
        manifest = {
            "version": "goal-addendum-v1",
            "origin": "user_addendum",
            "source_goal_version_id": parent.goal_version_id,
            "source_jd_revision_id": parent.source_jd_revision_id,
            "parent_addendum_id": previous,
            "topic": topic,
            "topics": topics,
            "modality": None,
            "required_depth": None,
        }
        addendum = GoalAddendum(
            workflow_id=draft.id,
            author_id=actor,
            source_goal_version_id=parent.goal_version_id,
            parent_addendum_id=previous,
            manifest=manifest,
            manifest_hash=fingerprint(manifest),
        )
        db.add(addendum)
        db.flush()
        draft.target = {
            key: old[key]
            for key in ("entry", "target", "source_id", "repository_url")
            if key in old
        }
        draft.target.update(goal_addendum_id=addendum.id, user_topics=topics)
        self._set_wait(draft, "confirm_scope", "awaiting_goal_confirmation")
        conv.focus = {**conv.focus, "active_goal_version_id": draft.goal_version_id}
        self.workflows.emit_in(
            db,
            conv,
            "workflow.goal_revision_committed",
            f"addendum:{addendum.id}",
            draft,
            addendum_id=addendum.id,
        )
        return draft

    def _roots_in(self, db, workflow):
        ordered = sorted(
            (root for _, root in workflow_runs(db, workflow.id)),
            key=lambda root: (root.funding_id, root.id),
        )
        return [lock_run(db, root.id) for root in ordered]

    def _pause_in(self, db, conv, workflow):
        if workflow.control_state in {"pause_requested", "paused"}:
            return {"reply": "暂停请求已记录。", "control_state": workflow.control_state}
        roots = self._roots_in(db, workflow)
        session_active = self.app.session_workflows.active(db, workflow)
        if roots and all(root.status in TERMINAL for root in roots) and not session_active:
            raise DomainError("TERMINAL_RUN", "任务已经结束，不能暂停。", 409)
        workflow.control_state = (
            "pause_requested" if any(root.status not in TERMINAL for root in roots) else "paused"
        )
        self.app.session_workflows.pause_in(db, workflow)
        workflow.revision += 1
        refs = []
        for root in roots:
            if root.status not in TERMINAL:
                refs.extend(
                    run.id for run in request_pause_in(db, root) if run.status not in TERMINAL
                )
        self.workflows.emit_in(
            db,
            conv,
            "workflow.pause_requested",
            f"pause:{workflow.id}:{workflow.revision}",
            workflow,
            run_ids=refs,
        )
        return {
            "reply": "已请求暂停，等待后台安全检查点。" if roots else "当前工作流已暂停。",
            "control_state": workflow.control_state,
            "run_refs": refs,
        }

    def _resume_in(self, db, conv, workflow, message):
        if (
            workflow.control_state not in {"paused", "pause_requested"}
            and workflow.parent_workflow_id
        ):
            previous = self.workflows.owned_in(db, conv, workflow.parent_workflow_id)
            if previous.control_state in {"paused", "pause_requested"}:
                workflow = previous
        roots = self._roots_in(db, workflow)
        waiting = [root for root in roots if root.status not in TERMINAL]
        for root in roots:
            if root.status == "cancelled":
                agreement = db.get(FundingAgreement, root.funding_id)
                if agreement and agreement.status != "active":
                    self._set_wait(workflow, "waiting_reauthorization", workflow.milestone)
                    return workflow, {
                        "status": "waiting_confirmation",
                        "failure_code": "FUNDING_UNAVAILABLE",
                        "reply": "原授权已失效，请授权新任务。",
                    }
                raise DomainError("TERMINAL_RUN", "任务已取消，必须新建任务。", 409)
        if workflow.control_state not in {"paused", "pause_requested"}:
            raise DomainError("NOT_PAUSED", "该工作流当前无需恢复。", 409)
        pending_plan = (
            workflow.milestone == "building"
            and bool(workflow.target.get("publication_ids"))
            and not waiting
        )
        session_active = self.app.session_workflows.active(db, workflow)
        if roots and not waiting and not pending_plan and not session_active:
            raise DomainError("TERMINAL_RUN", "任务已结束，必须新建任务。", 409)
        families = []
        for root in waiting:
            if message.authorization_ref and message.authorization_ref != root.funding_id:
                self._set_wait(workflow, "waiting_reauthorization", workflow.milestone)
                return workflow, {
                    "status": "waiting_confirmation",
                    "failure_code": "WAITING_REAUTHORIZATION",
                    "reply": "新授权不能覆盖旧任务，请按新范围建立任务。",
                }
            try:
                families.append(resume_precheck(db, root))
            except DomainError as exc:
                if exc.code not in {"FUNDING_UNAVAILABLE", "WAITING_REAUTHORIZATION"}:
                    raise
                self._set_wait(workflow, "waiting_reauthorization", workflow.milestone)
                return workflow, {
                    "status": "waiting_confirmation",
                    "failure_code": exc.code,
                    "reply": exc.message,
                }
        for family in families:
            for run in family:
                if run.input_manifest.get("model_profile") not in (
                    None,
                    self.app.provider.profile_version,
                ):
                    raise DomainError(
                        "MODEL_PROFILE_CHANGED", "模型配置已变化，请建立新任务。", 409
                    )
        workflow.control_state = "resume_requested"
        workflow.revision += 1
        self.workflows.emit_in(
            db,
            conv,
            "workflow.resume_requested",
            f"resume-request:{workflow.id}:{workflow.revision}",
            workflow,
        )
        for family in families:
            resume_family(db, family)
        workflow.control_state = "active"
        if workflow.target.get("waiting_user_action") == "waiting_reauthorization":
            workflow.target = {**workflow.target, "waiting_user_action": None}
        self.workflows.focus_in(db, conv, workflow)
        self.workflows.emit_in(
            db, conv, "workflow.resumed", f"resume:{workflow.id}:{workflow.revision}", workflow
        )
        if session_active:
            self.app.session_workflows.wake_in(db, workflow, kind="resume")
            return workflow, {
                "control_state": "active",
                "run_refs": [root.id for root in waiting],
                "reply": "已恢复会话执行，将重新核验当前等待条件。",
            }
        if pending_plan:
            result = self.app.continuation.propose_plan(db, conv.principal_id, conv, workflow)
            return workflow, {**result, "control_state": "active"}
        return workflow, {
            "control_state": "active",
            "run_refs": [root.id for root in waiting],
            "reply": "已重新核验授权与冻结任务，继续执行。",
        }

    def _workflow_action_in(self, db, actor, conv, workflow, message, action):
        if workflow is None:
            raise DomainError("WORKFLOW_REQUIRED", "请先导入 JD 或指定学习方向。", 409)
        if workflow.control_state in {"cancelled", "cancel_requested", "resume_requested"}:
            raise DomainError("WORKFLOW_NOT_ACTIVE", "工作流当前不能开始新操作。", 409)
        if action.type == "request_pause":
            return workflow, self._pause_in(db, conv, workflow)
        if action.type == "resume_workflow":
            return self._resume_in(db, conv, workflow, message)
        if workflow.control_state in {"paused", "pause_requested"}:
            raise DomainError("WORKFLOW_PAUSED", "恢复工作流后才可开始新的执行。", 409)
        handler = self.app.continuation
        if conv.focus.get("active_workflow_id") != workflow.id:
            raise DomainError("WORKFLOW_FOCUS", "请先切换到该工作流。", 409)
        if action.type == "retry_build":
            return self.app.continuation.retry_build(db, actor, conv, workflow, message)
        if action.type == "start_build":
            if (
                workflow.goal_version_id
                and workflow.target.get("learning_language_contract")
                and not message.source_id
                and not message.source_ids
                and not workflow.target.get("source_ids")
                and workflow.target.get("entry") != "set_repository"
            ):
                return workflow, handler.start_project_discovery(db, actor, conv, workflow, message)
            return workflow, handler.start_build(db, actor, conv, workflow, message)
        if action.type == "start_project_discovery":
            return workflow, handler.start_project_discovery(db, actor, conv, workflow, message)
        if action.type == "propose_plan":
            if workflow.orchestration_engine == "langgraph":
                execution = self.app.session_workflows.current(db, workflow)
                if execution is None:
                    return workflow, {
                        "status": "waiting_confirmation",
                        "reply": "请先确认构建范围。",
                    }
                if execution.status in {"completed", "failed", "cancelled"}:
                    raise DomainError(
                        "TERMINAL_WORKFLOW", "会话执行已结束，请新建明确范围的任务。", 409
                    )
                if workflow.target.get("proposal") or self.app.session_workflows.runs(
                    db, workflow, "plan"
                ):
                    return workflow, handler.propose_plan(db, actor, conv, workflow)
                return workflow, {
                    "execution_id": execution.id,
                    "reply": "已登记计划请求，将重新核验授权。",
                }
            return workflow, handler.propose_plan(
                db, actor, conv, workflow, message.authorization_ref
            )
        if action.type == "accept_plan":
            return workflow, handler.accept_plan(db, actor, conv, workflow, message)
        if action.type == "start_tutor":
            return workflow, handler.tutor(db, actor, conv, workflow, message)
        if workflow.learning_graph_version == "learning-session-practice-v1":
            raise DomainError("PRACTICE_CALLER_CONTRACT", "练习与反馈需要明确的版本化输入。", 422)
        if action.type == "start_practice":
            return workflow, handler.practice(db, actor, workflow, message)
        return workflow, handler.feedback(db, actor, conv, workflow, message)
