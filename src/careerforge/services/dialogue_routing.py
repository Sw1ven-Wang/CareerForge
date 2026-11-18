"""Conservative zero-model routes for explicit conversation controls."""

import re

from ..contracts_conversation_actions_v1 import ActionStep, ConversationActionPlan

_PROGRESS = re.compile(
    r"(?:现在|当前|目前)?\s*(?:什么阶段了?|什么状态|进展如何|进行到哪一步了?|到哪一步了?|进度(?:怎么样|如何)?|任务状态)|"
    r"(?:status|progress|/status)",
    re.I,
)
_ADD = re.compile(
    r"(?:我)?(?:需要|想|请)?\s*(?:补充|增补|加上)\s*([\w.+# /-]+?)(?:技术栈|学习目标)?$", re.I
)
_REPO = re.compile(
    r"(?:请|帮我)?(?:把)?(?:学习|导入|分析)?\s*(https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:\s*(?:入库|导入知识库))?", re.I
)
_TOPIC = re.compile(r"(?:我想学习|我想学|学习|开始学习)\s*([^？?。！!，,]{1,120})")
# Only caller-marked imports bypass semantic recognition. Merely mentioning a
# job description (including quoted or negated text) must not save a new JD.
_JD = re.compile(
    r"^(?:(?:请|帮我)?\s*导入\s*(?:以下|这份)?\s*)?JD\s*[:：]\s*(?P<text>\S[\s\S]*)$|"
    r"^(?:请|帮我)?\s*导入\s*(?:以下|这份)?\s*(?:岗位要求|岗位职责|任职资格|职位描述)\s*[:：]\s*(?P<description>\S[\s\S]*)$",
    re.I,
)
_JD_REQUIREMENT = re.compile(
    r"^(?:我们要求|岗位要求\s*[:：]|任职资格\s*[:：])\s*(?:熟悉|掌握|精通|具备|具有).+",
    re.S,
)


def explicit_actions(message):
    original = message.text
    phrase = original.strip().rstrip("。？?！!；;")
    # Anchored caller commands; quoted lesson/JD content is never parsed as a submission.
    if message.submission is not None:
        return [("submit_practice_answer", {})]
    if message.module_ack is not None:
        return [("advance_learning_module", {})]
    if message.publication_ids:
        return [("use_reviewed_material", {})]
    if phrase in {"评估我的答案", "评估练习", "评价我的答案", "/assess"}:
        return [("assess_practice", {})]
    if re.fullmatch(r"(?:请)?(?:查询|查看)(?:任务|学习)?进度", phrase):
        return [("query_progress", {})]
    if re.fullmatch(r"(?:我)?确认(?:采用|启用)(?:刚才的|这份|当前)?学习计划(?:[，,].*)?", phrase):
        return [("accept_plan", {}), ("start_tutor", {})]
    if re.fullmatch(r"(?:请)?(?:生成|创建)(?:一道)?练习(?:题)?|开始练习|给我一道练习题", phrase):
        return [("start_practice", {})]
    if re.fullmatch(r"(?:创建|生成|制定)(?:一份)?学习计划", phrase):
        return [("propose_plan", {})]
    if message.kind == "jd":
        return [("import_jd", {"text": original})]
    repo = _REPO.fullmatch(phrase)
    if message.kind == "repository":
        return [("set_repository", {"target": repo.group(1) if repo else phrase})]
    if message.kind == "topic":
        return [("set_learning_topic", {"target": phrase})]
    if phrase in {"搜项目并入库", "搜索项目并入库", "搜集项目并入库", "/projects"}:
        return [("start_project_discovery", {})]
    if re.fullmatch(r"/language(?:\s.*)?", original.strip(), re.I) or re.fullmatch(
        r"(?:后端|实现语言)(?:选择|使用|用|选|是|[:：])\s*.+", phrase, re.I
    ):
        return [("select_implementation_language", {})]
    # A natural language instruction cannot name another account as a control target.
    if re.search(r"(?:暂停|恢复|继续).*(?:另一个用户|其他用户|别人的|他人)", phrase):
        return [("request_pause", {"foreign": True})]
    # Explicit same-chat module choice is never a paid question about the old module.
    selection = re.fullmatch(
        r"(?:/module\s+|(?:选择|切换到?)\s*(?:模块\s*|第))(.+?)(?:个?模块)?(?:(并讲解)|\s+(--teach))?",
        phrase,
        re.I,
    )
    if selection:
        actions = [("select_learning_module", {"selector": selection.group(1).strip()})]
        if selection.group(2) or selection.group(3):
            actions.append(("start_tutor", {}))
        return actions
    if (
        phrase.startswith("/module")
        or phrase.startswith("选择模块")
        or phrase.startswith("切换模块")
    ):
        return [("ask_clarification", {"slot": "scope_or_confirmation"})]
    if _PROGRESS.fullmatch(phrase):
        return [("query_progress", {})]
    if re.fullmatch(r"(?:请)?取消(?:当前任务|任务|当前工作流|工作流)?|/cancel", phrase):
        return [("cancel_workflow", {})]
    amendment = re.fullmatch(
        r"(?:我)?(?:需要|想|请)?\s*(?:增加|补充|增补|加上)\s*([\w.+# /-]+?)(?:技术栈|学习目标)?[，,]\s*(要求)?(掌握|熟悉|了解|精通)",
        phrase,
    )
    if amendment:
        base = len(original) - len(original.lstrip())
        target = amendment.group(1).strip()
        start = (
            base + amendment.start(1) + len(amendment.group(1)) - len(amendment.group(1).lstrip())
        )
        return [
            (
                "add_learning_objective",
                {
                    "target": target,
                    "target_depth": amendment.group(3),
                    "learning_priority": "required" if amendment.group(2) else "unspecified",
                    "target_span": {"start": start, "end": start + len(target)},
                    "depth_span": {
                        "start": base + amendment.start(3),
                        "end": base + amendment.end(3),
                    },
                    "priority_span": {
                        "start": base + amendment.start(2),
                        "end": base + amendment.end(2),
                    }
                    if amendment.group(2)
                    else None,
                },
            )
        ]
    if re.fullmatch(r"(?:先)?暂停(?:当前任务|任务|一下)?|/pause", phrase):
        return [("request_pause", {})]
    if re.fullmatch(r"(?:先)?暂停.*(?:另外导入|切换|另一个|其它的技术栈|其他技术栈).*", phrase):
        return [
            ("request_pause", {}),
            ("switch_focus", {"mode": "new"}),
            ("ask_clarification", {"slot": "new_jd_or_topic"}),
        ]
    if re.fullmatch(r"(?:请)?继续(?:刚才|之前|原来|原先)(?:的)?.{0,18}|/resume", phrase):
        return [("resume_workflow", {})]
    if re.fullmatch(r"(?:切换|先看).*(?:技术栈|项目|目标)", phrase):
        return [("switch_focus", {"mode": "new"})]
    if re.fullmatch(r"(?:我)?(?:需要|想|请)?(?:修改|调整)(?:一下)?", phrase):
        return [("ask_clarification", {"slot": "target_to_modify"})]
    topic = _ADD.fullmatch(phrase)
    if topic:
        return [("add_learning_topic", {"target": topic.group(1).strip()})]
    if phrase in {"补证重试", "重新研究", "/retry-build"}:
        return [("retry_build", {})]
    if phrase in {"确认提取", "开始提取", "/extract"}:
        return [("start_extraction", {})]
    if re.fullmatch(r"(?:我)?(?:确认|接受)(?:学习)?计划(?:[，,].*)?", phrase):
        return [("accept_plan", {}), ("start_tutor", {})]
    if re.fullmatch(
        r"(?:我)?(?:确认|同意)(?:岗位|学习)?(?:目标|要求)(?:[，,].*)?|(?:开始|请开始)研究这份岗位要求|/build",
        phrase,
    ):
        return [("start_build", {})]
    if re.fullmatch(r"(?:给我|帮我|请)?制定(?:学习)?计划|/plan", phrase):
        return [("propose_plan", {})]
    if re.fullmatch(r"开始下一步|下一步|继续分析", phrase):
        return [("propose_plan", {})]
    if re.fullmatch(r"(?:开始|继续)(?:当前)?(?:模块)?讲解|(?:请)?(?:开始|继续)学习|/teach", phrase):
        return [("start_tutor", {})]
    if re.fullmatch(r"(?:我)?(?:想|开始|请)?练习(?:这个机制|当前模块)?(?:[，,].*)?", phrase):
        return [("start_practice", {})]
    if phrase in {"没理解", "没听懂", "我没理解", "我没听懂", "不理解", "我不理解", "记录练习反馈"}:
        return [("record_feedback", {})]
    jd = _JD.fullmatch(original.strip())
    if jd:
        text = jd.group("text") or jd.group("description")
        return [("import_jd", {"text": text})]
    # Retain direct declarative requirements, but never shortcut a question or
    # an instruction about importing/explaining the requirements themselves.
    if _JD_REQUIREMENT.fullmatch(phrase) and not re.search(
        r"[？?]|如何|怎么|什么|是否|吗|解释|含义|导入", phrase
    ):
        return [("import_jd", {"text": original})]
    implementation_question = bool(re.search(r"如何|怎么|实现|调用关系|源码|函数", phrase))
    if repo and not implementation_question:
        return [("set_repository", {"target": repo.group(1)})]
    topic = _TOPIC.fullmatch(phrase)
    if topic and not implementation_question:
        return [("set_learning_topic", {"target": topic.group(1)})]
    return None


def bound_plan(conversation, workflow, message, actions):
    from ..contracts_practice_dialogue_v1 import PracticeDialogueMessageV1

    if isinstance(message, PracticeDialogueMessageV1):
        from ..contracts_conversation_actions_v4 import ConversationActionPlanV4

        return ConversationActionPlanV4(
            conversation_id=conversation.id,
            expected_focus_revision=message.expected_focus_revision,
            target_workflow_id=workflow.id if workflow else None,
            idempotency_key=message.idempotency_key,
            actions=[dict(step_id="1", action={"type": message.intent.type})],
        )
    if getattr(message, "contract", None) == "learning-dialogue-v1" or any(
        name
        in {
            "start_learning_session",
            "learning_followup",
            "authorize_tutor_request",
            "select_learning_module",
        }
        for name, _ in actions
    ):
        from ..contracts_conversation_actions_v3 import ActionStepV3, ConversationActionPlanV3

        return ConversationActionPlanV3(
            conversation_id=conversation.id,
            expected_focus_revision=message.expected_focus_revision,
            target_workflow_id=workflow.id if workflow else None,
            idempotency_key=message.idempotency_key,
            actions=[
                ActionStepV3(
                    step_id=str(index),
                    depends_on=[str(index - 1)] if index > 1 else [],
                    action={"type": name, **args},
                )
                for index, (name, args) in enumerate(actions, 1)
            ],
        )
    if any(name in {"add_learning_objective", "cancel_workflow"} for name, _ in actions):
        from ..contracts_conversation_actions_v2 import ActionStepV2, ConversationActionPlanV2

        return ConversationActionPlanV2(
            conversation_id=conversation.id,
            expected_focus_revision=message.expected_focus_revision,
            target_workflow_id=workflow.id if workflow else None,
            idempotency_key=message.idempotency_key,
            actions=[
                ActionStepV2(
                    step_id=str(index),
                    depends_on=[str(index - 1)] if index > 1 else [],
                    action={"type": name, **args},
                )
                for index, (name, args) in enumerate(actions, 1)
            ],
        )
    steps = [
        ActionStep(
            step_id=str(index),
            depends_on=[str(index - 1)] if index > 1 else [],
            action={"type": name, **args},
        )
        for index, (name, args) in enumerate(actions, 1)
    ]
    return ConversationActionPlan(
        conversation_id=conversation.id,
        expected_focus_revision=message.expected_focus_revision,
        target_workflow_id=workflow.id if workflow else None,
        idempotency_key=message.idempotency_key,
        actions=steps,
    )
