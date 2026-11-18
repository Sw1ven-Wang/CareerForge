"""Immutable, receipt-derived user additions. No model calls or spending authority."""

from copy import deepcopy

from pydantic import ValidationError
from sqlalchemy import select

from ..contracts_goal_amendment_v1 import GoalAmendmentManifestV2
from ..db import locked
from ..errors import DomainError
from ..models import CareerGoal, GoalVersion, ObjectRow
from ..models_dialogue import ConversationActionReceipt, GoalAddendum, WorkflowSession
from ..util import digest, fingerprint
from .dependencies import check_closure, depend, register, require_object
from .taxonomy import require_topic, topic_object_id, visible_topics

MAX_LINEAGE = 32
MAX_OBJECTIVES = 30


def dialogue_command_source(
    text,
    technology,
    *,
    target_span=None,
    target_depth=None,
    depth_span=None,
    learning_priority="unspecified",
    priority_span=None,
):
    """Exact code-point spans; repeated source occurrences require an explicit span."""

    def span(value, supplied):
        if supplied is not None:
            return supplied
        if value is None:
            return None
        if text.count(value) != 1:
            raise DomainError("GOAL_COMMAND_SOURCE", "用户目标位置不明确，请明确新增内容。", 422)
        start = text.index(value)
        return {"start": start, "end": start + len(value)}

    if learning_priority != "unspecified" and priority_span is None:
        terms = {"required": ("要求", "必须", "需要"), "preferred": ("优先", "最好", "希望")}.get(
            learning_priority, ()
        )
        matches = [word for word in terms if text.count(word) == 1]
        if len(matches) != 1:
            raise DomainError("GOAL_COMMAND_SOURCE", "学习优先级缺少明确来源。", 422)
        priority_span = span(matches[0], None)
    return {
        "kind": "dialogue_text",
        "text": text,
        "raw_sha256": digest(text),
        "byte_count": len(text.encode("utf-8")),
        "target_span": span(technology, target_span),
        "depth_span": span(target_depth, depth_span),
        "priority_span": priority_span,
    }


def structured_command_source(fields, *, target_path="topic"):
    return {
        "kind": "goal_revision_fields",
        "fields": deepcopy(fields),
        "fields_hash": fingerprint(fields),
        "target_path": target_path,
        "depth_path": "target_depth" if fields.get("target_depth") is not None else None,
        "priority_path": "learning_priority" if "learning_priority" in fields else None,
    }


def _fail(message="新增目标的来源、版本或命令回执不一致。"):
    raise DomainError("GOAL_AMENDMENT", message, 409)


def _mapping(db, actor, technology):
    matches = [
        row
        for row in visible_topics(db, actor)
        if technology.casefold()
        in {row.key.casefold(), row.title.casefold(), *(alias.casefold() for alias in row.aliases)}
    ]
    if len(matches) != 1:
        return {"status": "pending_mapping"}
    topic = matches[0]
    require_topic(db, actor, topic.key, topic.version)
    obj = require_object(db, actor, topic_object_id(topic.key, topic.version), "topic_version")
    return {
        "status": "mapped",
        "topic_key": topic.key,
        "topic_version": topic.version,
        "topic_object_id": obj.id,
        "topic_object_hash": obj.content_hash,
    }


def _version_hash(version, parent_object, addendum):
    return fingerprint(
        {
            "contract_version": "command-goal-v1",
            "goal_id": version.goal_id,
            "version": version.version,
            "parent_goal_version_id": version.parent_goal_version_id,
            "parent_object_content_hash": parent_object.content_hash,
            "source_text_hash": version.content_hash,
            "requirements_envelope_hash": fingerprint(version.requirements),
            "goal_addendum_id": addendum.id,
            "goal_addendum_manifest_hash": addendum.manifest_hash,
        }
    )


def command_goal_payload(db, version):
    from .research_coverage import _goal_payload

    addendum = db.get(GoalAddendum, version.goal_addendum_id)
    if addendum is None:
        _fail()
    return {
        **_goal_payload(version),
        "contract_version": "command-goal-v1",
        "origin_contract": version.origin_contract,
        "parent_goal_version_id": version.parent_goal_version_id,
        "goal_addendum_id": version.goal_addendum_id,
        "goal_addendum_manifest_hash": addendum.manifest_hash,
        "version_hash": version.version_hash,
    }


def _validate_receipt_plan(
    receipt,
    index,
    technology,
    *,
    target_depth=None,
    learning_priority="unspecified",
    command_source=None,
):
    from ..contracts_conversation_actions_v1 import ConversationActionPlan
    from ..contracts_conversation_actions_v2 import ConversationActionPlanV2

    if not isinstance(receipt.plan, dict) or receipt.action_plan_hash != fingerprint(receipt.plan):
        _fail()
    marker = receipt.plan.get("contract_version")
    plan_type = {
        "conversation-action-plan-v1": ConversationActionPlan,
        "conversation-action-plan-v2": ConversationActionPlanV2,
    }.get(marker)
    if plan_type is None:
        _fail("未知或缺失的目标修订动作合同。")
    # Import actions are deliberately stored as a one-way hash in the existing
    # receipt audit shape. Validate that shape without fabricating original text.
    proposal = deepcopy(receipt.plan)
    redacted = {}
    steps = proposal.get("actions")
    if not isinstance(steps, list):
        _fail()
    for position, step in enumerate(steps):
        action = step.get("action") if isinstance(step, dict) else None
        if (
            isinstance(action, dict)
            and action.get("type") == "import_jd"
            and "text_sha256" in action
        ):
            value = action["text_sha256"]
            if (
                set(action) != {"type", "text_sha256"}
                or not isinstance(value, str)
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
            ):
                _fail()
            redacted[position] = deepcopy(action)
            step["action"] = {"type": "import_jd", "text": "audit-redaction-shape-only"}
    try:
        plan = plan_type.model_validate(proposal)
    except (ValidationError, TypeError, ValueError) as exc:
        raise DomainError("GOAL_AMENDMENT", "目标修订动作合同无效。", 409) from exc
    canonical = plan.model_dump(mode="json")
    for position, action in redacted.items():
        canonical["actions"][position]["action"] = action
    if (
        canonical != receipt.plan
        or plan.conversation_id != receipt.conversation_id
        or plan.idempotency_key != receipt.idempotency_key
    ):
        _fail()
    if type(index) is not int or index < 0 or index >= len(plan.actions):
        _fail()
    action = plan.actions[index].action
    if (
        action.type not in {"add_learning_topic", "add_learning_objective"}
        or action.target != technology
        or getattr(action, "target_depth", None) != target_depth
        or getattr(action, "learning_priority", "unspecified") != learning_priority
    ):
        _fail()
    if command_source is not None:
        if command_source.get("kind") == "dialogue_text":
            spans = {
                key: getattr(action, key).model_dump()
                if getattr(action, key, None) is not None
                else None
                for key in ("target_span", "depth_span", "priority_span")
            }
            expected_source = dialogue_command_source(
                command_source.get("text", ""),
                technology,
                target_depth=target_depth,
                learning_priority=learning_priority,
                **spans,
            )
            if expected_source != command_source:
                _fail()
        elif any(
            getattr(action, key, None) is not None
            for key in ("target_span", "depth_span", "priority_span")
        ):
            _fail("结构化字段不能伪装成原始文本位置。")
    return action


def _receipt_outcome(receipt, index):
    result = receipt.result if isinstance(receipt.result, dict) else {}
    outcomes = result.get("action_receipts", [])
    if not isinstance(outcomes, list):
        _fail()
    found = [
        item
        for item in outcomes
        if isinstance(item, dict)
        and type(item.get("action_index")) is int
        and item.get("action_index") == index
    ]
    if (
        len(found) != 1
        or found[0].get("status") != "applied"
        or found[0].get("action_type") != receipt.plan["actions"][index]["action"]["type"]
    ):
        _fail("新增目标缺少已提交的成功命令回执。")
    return found[0]


def _legacy(db, actor, identifier, source_version, conversation_id, *, register_missing=False):
    from .security import private_namespace

    own_namespace = private_namespace(db, actor).id
    rows, seen = [], set()
    while identifier:
        if identifier in seen or len(seen) >= MAX_LINEAGE:
            _fail("旧增补目标链存在循环或超出上限。")
        seen.add(identifier)
        row = db.get(GoalAddendum, identifier)
        workflow = db.get(WorkflowSession, row.workflow_id) if row else None
        if (
            row is None
            or workflow is None
            or row.author_id != actor
            or workflow.principal_id != actor
            or workflow.conversation_id != conversation_id
            or workflow.namespace_id != own_namespace
            or row.source_goal_version_id != source_version
            or row.manifest_hash != fingerprint(row.manifest)
        ):
            _fail()
        manifest = row.manifest
        if not isinstance(manifest, dict):
            _fail()
        if (
            manifest.get("version") != "goal-addendum-v1"
            or manifest.get("origin") != "user_addendum"
            or manifest.get("source_goal_version_id") != source_version
            or manifest.get("parent_addendum_id") != row.parent_addendum_id
            or manifest.get("source_jd_revision_id") != workflow.source_jd_revision_id
            or not isinstance(manifest.get("topic"), str)
            or not manifest["topic"].strip()
            or row.command_receipt_id is not None
        ):
            _fail()
        obj = db.get(ObjectRow, row.id)
        if obj is None and register_missing:
            obj = register(
                db, actor, workflow.namespace_id, "goal_addendum", deepcopy(manifest), row.id
            )
            depend(db, actor, row.id, source_version, "derived_from_goal")
        obj = require_object(db, actor, row.id, "goal_addendum")
        if obj.payload != manifest or obj.namespace_id != workflow.namespace_id:
            _fail()
        check_closure(db, actor, [obj.id])
        rows.append(row)
        identifier = row.parent_addendum_id
    if register_missing:
        for row in rows:
            if row.parent_addendum_id:
                depend(db, actor, row.id, row.parent_addendum_id, "inherits_addendum")
    return list(reversed(rows))


def validate_goal_amendment_in(db, actor, version_id):
    """Validate every ancestor, source object and actual indexed successful receipt."""
    from .research_coverage import _goal_payload, _goal_projection

    seen, chain, cursor = set(), [], version_id
    while cursor:
        if cursor in seen or len(seen) >= MAX_LINEAGE:
            _fail("目标版本链存在循环或超出上限。")
        seen.add(cursor)
        version, goal = _goal_projection(db, actor, cursor)
        obj = require_object(db, actor, cursor, "goal_version")
        if obj.namespace_id != goal.namespace_id:
            _fail()
        check_closure(db, actor, [cursor])
        if version.origin_contract == "run-goal-v1":
            if (
                version.run_id is None
                or any(
                    (version.parent_goal_version_id, version.goal_addendum_id, version.version_hash)
                )
                or obj.payload != _goal_payload(version)
            ):
                _fail()
            break
        if version.origin_contract != "command-goal-v1" or version.run_id is not None:
            _fail()
        parent = db.get(GoalVersion, version.parent_goal_version_id)
        addendum = db.get(GoalAddendum, version.goal_addendum_id)
        if (
            parent is None
            or addendum is None
            or parent.goal_id != version.goal_id
            or version.version != parent.version + 1
            or version.source_text != parent.source_text
            or version.content_hash != parent.content_hash
            or version.requirements != parent.requirements
        ):
            _fail()
        try:
            manifest = GoalAmendmentManifestV2.model_validate(addendum.manifest)
        except (ValidationError, TypeError, ValueError) as exc:
            raise DomainError("GOAL_AMENDMENT", "新增目标来源合同无效。", 409) from exc
        parent_object = require_object(db, actor, parent.id, "goal_version")
        source = require_object(db, actor, addendum.id, "goal_addendum")
        workflow = db.get(WorkflowSession, addendum.workflow_id)
        receipt = db.get(ConversationActionReceipt, addendum.command_receipt_id)
        parent_workflow = (
            db.get(WorkflowSession, workflow.parent_workflow_id)
            if workflow and workflow.parent_workflow_id
            else None
        )
        if (
            workflow is None
            or receipt is None
            or parent_workflow is None
            or parent_workflow.goal_version_id != parent.id
            or parent_workflow.principal_id != actor
            or parent_workflow.conversation_id != workflow.conversation_id
            or parent_workflow.namespace_id != goal.namespace_id
            or workflow.target.get("goal_addendum_id") != addendum.id
            or workflow.principal_id != actor
            or workflow.namespace_id != goal.namespace_id
            or workflow.goal_version_id != version.id
            or receipt.conversation_id != workflow.conversation_id
            or addendum.author_id != actor
            or source.namespace_id != goal.namespace_id
            or addendum.manifest_hash != fingerprint(addendum.manifest)
            or source.payload != addendum.manifest
        ):
            _fail()
        if (
            manifest.source_goal_version_id,
            manifest.source_goal_object_hash,
            manifest.parent_addendum_id,
            manifest.command_receipt_id,
            manifest.command_action_index,
            manifest.command_request_hash,
            manifest.source_jd_revision_id,
        ) != (
            parent.id,
            parent_object.content_hash,
            addendum.parent_addendum_id,
            receipt.id,
            addendum.command_action_index,
            receipt.request_hash,
            workflow.source_jd_revision_id,
        ) or addendum.source_goal_version_id != parent.id:
            _fail()
        original_jd = (
            parent.requirements.get("jd_revision_id")
            if isinstance(parent.requirements, dict)
            else None
        )
        if (
            workflow.source_jd_revision_id != original_jd
            or parent_workflow.source_jd_revision_id != original_jd
        ):
            _fail()
        if parent.goal_addendum_id and addendum.parent_addendum_id != parent.goal_addendum_id:
            _fail()
        _validate_receipt_plan(
            receipt,
            manifest.command_action_index,
            manifest.objective.technology,
            target_depth=manifest.objective.target_depth,
            learning_priority=manifest.objective.learning_priority,
            command_source=manifest.command_source.model_dump(mode="json"),
        )
        outcome = _receipt_outcome(receipt, manifest.command_action_index)
        for key, value in {
            "old_goal_version_id": parent.id,
            "new_goal_version_id": version.id,
            "goal_addendum_id": addendum.id,
            "workflow_id": workflow.id,
        }.items():
            if outcome.get(key) != value:
                _fail()
        objective = manifest.objective
        expected_id = (
            "uobj-"
            + fingerprint(
                {
                    "receipt_id": receipt.id,
                    "action_index": manifest.command_action_index,
                    "operation_index": 0,
                }
            )[:24]
        )
        if (
            objective.objective_id != expected_id
            or version.version_hash != _version_hash(version, parent_object, addendum)
            or obj.payload != command_goal_payload(db, version)
        ):
            _fail()
        mapping = objective.mapping
        if mapping.status == "mapped":
            require_topic(db, actor, mapping.topic_key, mapping.topic_version, allow_retired=True)
            topic = require_object(db, actor, mapping.topic_object_id, "topic_version")
            if (
                mapping.topic_object_id != topic_object_id(mapping.topic_key, mapping.topic_version)
                or topic.content_hash != mapping.topic_object_hash
            ):
                _fail()
        legacy = []
        if manifest.inherited_legacy_addenda:
            if parent.origin_contract != "run-goal-v1":
                _fail()
            legacy = _legacy(
                db, actor, addendum.parent_addendum_id, parent.id, workflow.conversation_id
            )
            if [
                {"addendum_id": item.id, "manifest_hash": item.manifest_hash} for item in legacy
            ] != [item.model_dump() for item in manifest.inherited_legacy_addenda]:
                _fail()
        elif addendum.parent_addendum_id and not parent.goal_addendum_id:
            _fail()
        chain.append((version, addendum, manifest, legacy))
        cursor = parent.id
    objectives = []
    for _, _, manifest, legacy in reversed(chain):
        objectives.extend(
            {
                "origin": "legacy_user_addendum",
                "objective_id": "legacy-uobj-" + item.id,
                "technology": item.manifest["topic"],
                "target_depth": None,
                "depth_basis": "unspecified",
                "learning_priority": "unspecified",
                "source": {
                    "kind": "legacy_addendum_ref",
                    "addendum_id": item.id,
                    "manifest_hash": item.manifest_hash,
                },
                "mapping": {"status": "pending_mapping"},
                "user_mastery_unchanged": True,
            }
            for item in legacy
        )
        objectives.append(
            {
                **manifest.objective.model_dump(mode="json"),
                "source": manifest.command_source.model_dump(mode="json"),
            }
        )
    if len(objectives) > MAX_OBJECTIVES:
        _fail("有效学习目标超过上限。")
    first = chain[0] if chain else None
    return (
        {}
        if first is None
        else {
            "origin_contract": "command-goal-v1",
            "version_hash": first[0].version_hash,
            "parent_goal_version_id": first[0].parent_goal_version_id,
            "goal_addendum_id": first[1].id,
            "command_receipt_id": first[1].command_receipt_id,
            "command_action_index": first[1].command_action_index,
            "user_learning_objectives": objectives,
        }
    )


def read_effective_goal_in(db, actor, version_id):
    from .research_coverage import read_goal_version

    return read_goal_version(db, actor, version_id)


def create_goal_amendment_in(
    app,
    db,
    actor,
    conv,
    parent_workflow,
    receipt,
    action_index,
    *,
    technology,
    command_source,
    target_depth=None,
    learning_priority="unspecified",
    expected_goal_revision=None,
):
    from .research_coverage import ensure_goal_snapshot
    from .security import private_namespace

    own = private_namespace(db, actor)
    if expected_goal_revision is not None and (
        type(expected_goal_revision) is not int or expected_goal_revision < 1
    ):
        _fail("目标版本比较值无效。")
    if (
        parent_workflow is None
        or parent_workflow.principal_id != actor
        or parent_workflow.conversation_id != conv.id
        or parent_workflow.namespace_id != own.id
        or conv.principal_id != actor
        or receipt.conversation_id != conv.id
        or type(action_index) is not int
        or action_index < 0
    ):
        _fail()
    previous = db.scalar(
        select(GoalAddendum).where(
            GoalAddendum.command_receipt_id == receipt.id,
            GoalAddendum.command_action_index == action_index,
        )
    )
    if previous is not None:
        version = db.scalar(select(GoalVersion).where(GoalVersion.goal_addendum_id == previous.id))
        if version is None:
            _fail()
        validate_goal_amendment_in(db, actor, version.id)
        if (
            previous.manifest["objective"]["technology"] != technology
            or previous.manifest["objective"]["target_depth"] != target_depth
            or previous.manifest["objective"]["learning_priority"] != learning_priority
            or previous.manifest["command_source"] != command_source
        ):
            raise DomainError("IDEMPOTENCY_CONFLICT", "相同命令对应不同增补目标。", 409)
        return db.get(WorkflowSession, previous.workflow_id), deepcopy(
            _receipt_outcome(receipt, action_index)
        )
    _validate_receipt_plan(
        receipt,
        action_index,
        technology,
        target_depth=target_depth,
        learning_priority=learning_priority,
        command_source=command_source,
    )
    parent = db.get(GoalVersion, parent_workflow.goal_version_id)
    if parent is None:
        raise DomainError("GOAL_REQUIRED", "此修订需要已有职业目标版本。", 409)
    goal = locked(db, CareerGoal, parent.goal_id)
    if goal is None or goal.principal_id != actor or goal.namespace_id != own.id:
        _fail()
    if goal.active_version_id != parent.id or (
        expected_goal_revision is not None and goal.revision != expected_goal_revision
    ):
        raise DomainError("REVISION_CONFLICT", "目标版本已变化，请刷新后重新提交。", 409)
    if isinstance(command_source, dict) and command_source.get("kind") == "goal_revision_fields":
        fields = command_source.get("fields", {})
        if (
            fields.get("source_goal_version_id", parent.id) != parent.id
            or fields.get("expected_goal_revision", goal.revision) != goal.revision
        ):
            _fail("提交字段与目标版本不一致。")
    if parent.version >= MAX_LINEAGE:
        _fail("目标版本超过允许的有界链长度。")
    snapshot = ensure_goal_snapshot(db, actor, parent.id)
    parent_object = require_object(db, actor, parent.id, "goal_version")
    if parent_workflow.source_jd_revision_id != (
        parent.requirements.get("jd_revision_id") if isinstance(parent.requirements, dict) else None
    ):
        _fail("工作流与原始岗位版本不匹配。")
    inherited = []
    previous_addendum = parent_workflow.target.get("goal_addendum_id")
    if parent.goal_addendum_id:
        if previous_addendum != parent.goal_addendum_id:
            _fail()
    elif previous_addendum:
        inherited = _legacy(db, actor, previous_addendum, parent.id, conv.id, register_missing=True)
    if len(snapshot.get("user_learning_objectives", [])) + len(inherited) + 1 > MAX_OBJECTIVES:
        _fail("有效学习目标超过上限。")
    objective = {
        "objective_id": "uobj-"
        + fingerprint(
            {"receipt_id": receipt.id, "action_index": action_index, "operation_index": 0}
        )[:24],
        "technology": technology,
        "target_depth": target_depth,
        "depth_basis": "explicit" if target_depth is not None else "unspecified",
        "learning_priority": learning_priority,
        "mapping": _mapping(db, actor, technology),
    }
    try:
        manifest = GoalAmendmentManifestV2(
            source_goal_version_id=parent.id,
            source_goal_object_hash=parent_object.content_hash,
            source_jd_revision_id=parent_workflow.source_jd_revision_id,
            parent_addendum_id=previous_addendum,
            command_receipt_id=receipt.id,
            command_action_index=action_index,
            command_request_hash=receipt.request_hash,
            command_source=command_source,
            objective=objective,
            inherited_legacy_addenda=[
                {"addendum_id": item.id, "manifest_hash": item.manifest_hash} for item in inherited
            ],
        ).model_dump(mode="json")
    except (ValidationError, TypeError, ValueError) as exc:
        raise DomainError(
            "GOAL_COMMAND_SOURCE", "新增学习目标及限定词必须来自用户的明确输入。", 422
        ) from exc
    child = app.workflows.create_in(db, conv, parent=parent_workflow)
    child.source_jd_revision_id = parent_workflow.source_jd_revision_id
    addendum = GoalAddendum(
        workflow_id=child.id,
        author_id=actor,
        source_goal_version_id=parent.id,
        parent_addendum_id=previous_addendum,
        command_receipt_id=receipt.id,
        command_action_index=action_index,
        manifest=manifest,
        manifest_hash=fingerprint(manifest),
    )
    db.add(addendum)
    db.flush()
    register(db, actor, own.id, "goal_addendum", deepcopy(manifest), addendum.id)
    depend(db, actor, addendum.id, parent.id, "derived_from_goal")
    if previous_addendum:
        depend(db, actor, addendum.id, previous_addendum, "inherits_addendum")
    mapping = manifest["objective"]["mapping"]
    if mapping["status"] == "mapped":
        depend(db, actor, addendum.id, mapping["topic_object_id"], "maps_user_objective_to_topic")
    version = GoalVersion(
        goal_id=parent.goal_id,
        version=parent.version + 1,
        source_text=parent.source_text,
        content_hash=parent.content_hash,
        requirements=deepcopy(parent.requirements),
        run_id=None,
        origin_contract="command-goal-v1",
        parent_goal_version_id=parent.id,
        goal_addendum_id=addendum.id,
    )
    version.version_hash = _version_hash(version, parent_object, addendum)
    db.add(version)
    db.flush()
    register(db, actor, own.id, "goal_version", command_goal_payload(db, version), version.id)
    depend(db, actor, version.id, parent.id, "amends_goal")
    depend(db, actor, version.id, addendum.id, "applies_addendum")
    goal.active_version_id = version.id
    goal.revision += 1
    child.goal_version_id = version.id
    child.milestone = "awaiting_goal_confirmation"
    child.target = {
        "entry": "user_learning_addition",
        "target": technology,
        "goal_addendum_id": addendum.id,
        "user_topics": [item["technology"] for item in snapshot.get("user_learning_objectives", [])]
        + [item.manifest["topic"] for item in inherited]
        + [technology],
        "waiting_user_action": "confirm_scope",
    }
    child.revision += 1
    app.workflows.focus_in(db, conv, child)
    app.workflows.emit_in(
        db,
        conv,
        "workflow.goal_revision_committed",
        "goal-amendment:" + addendum.id,
        child,
        old_goal_version_id=parent.id,
        new_goal_version_id=version.id,
        addendum_id=addendum.id,
    )
    return child, {
        "old_goal_version_id": parent.id,
        "new_goal_version_id": version.id,
        "goal_version_id": version.id,
        "goal_revision": goal.revision,
        "goal_addendum_id": addendum.id,
        "workflow_id": child.id,
        "execution_id": None,
        "reply": "已创建独立的用户学习目标版本，等待确认来源范围与构建授权。",
    }
