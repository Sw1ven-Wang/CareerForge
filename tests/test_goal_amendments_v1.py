"""Zero-model domain fixtures, not native acceptance or employer extraction evidence."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from careerforge.contracts_conversation_actions_v2 import ConversationActionPlanV2
from careerforge.errors import DomainError
from careerforge.models import AgentRun, CareerGoal, Conversation, GoalVersion, ModelReceipt
from careerforge.models_dialogue import ConversationActionReceipt, GoalAddendum, WorkflowSession
from careerforge.services.goal_amendments import (
    create_goal_amendment_in,
    dialogue_command_source,
    structured_command_source,
)
from careerforge.services.research_coverage import ensure_goal_snapshot, read_goal_version
from careerforge.util import fingerprint


def base_goal(env):
    app, actor = env["app"], env["actor"]
    run = app.runtime.create(actor, env["ns"], env["funding"], "analysis", {}, 1, uuid4().hex)
    epoch = app.runtime.claim(run, "legacy-fixture")
    text = "原岗位要求\r\nPython 或 Go；可选 Rust；未知深度。"
    version = app.learning.save_goal(
        actor,
        env["ns"],
        run,
        text,
        {
            "requirements": [],
            "limitations": ["fixture legacy extraction"],
            "groups": [{"operator": "OR", "members": ["Python", "Go"]}],
        },
        epoch=epoch,
        worker="legacy-fixture",
    )
    with app.db.transaction() as db:
        conv = Conversation(principal_id=actor, namespace_id=env["ns"])
        db.add(conv)
        db.flush()
        workflow = app.workflows.create_in(db, conv)
        workflow.goal_version_id = version
        app.workflows.focus_in(db, conv, workflow)
        snapshot = ensure_goal_snapshot(db, actor, version)
        return conv.id, workflow.id, version, deepcopy(snapshot)


def amend_in(
    env,
    db,
    conv_id,
    workflow_id,
    *,
    technology="LangGraph",
    depth=None,
    priority="unspecified",
    text=None,
    source=None,
    key=None,
    expected_goal_revision=None,
    finalize=True,
):
    app, actor = env["app"], env["actor"]
    conv = db.get(Conversation, conv_id)
    parent = db.get(WorkflowSession, workflow_id)
    source = source or dialogue_command_source(
        text or "补充 " + technology, technology, target_depth=depth, learning_priority=priority
    )
    action = {
        "type": "add_learning_objective",
        "target": technology,
        "target_depth": depth,
        "learning_priority": priority,
    }
    if source.get("kind") == "dialogue_text":
        action.update(
            {name: source.get(name) for name in ("target_span", "depth_span", "priority_span")}
        )
    key = key or uuid4().hex
    plan = ConversationActionPlanV2(
        conversation_id=conv.id,
        expected_focus_revision=conv.revision,
        target_workflow_id=parent.id,
        idempotency_key=key,
        actions=[{"step_id": "1", "action": action}],
    ).model_dump(mode="json")
    receipt = ConversationActionReceipt(
        conversation_id=conv.id,
        workflow_id=parent.id,
        idempotency_key=key,
        request_hash=fingerprint({"text": text, "source": source, "technology": technology}),
        action_plan_hash=fingerprint(plan),
        plan=plan,
        result={},
    )
    db.add(receipt)
    db.flush()
    child, result = create_goal_amendment_in(
        app,
        db,
        actor,
        conv,
        parent,
        receipt,
        0,
        technology=technology,
        command_source=source,
        target_depth=depth,
        learning_priority=priority,
        expected_goal_revision=expected_goal_revision,
    )
    if finalize:
        receipt.result = {
            "action_receipts": [
                {
                    **result,
                    "action_index": 0,
                    "action_type": "add_learning_objective",
                    "status": "applied",
                }
            ]
        }
        db.flush()
    return child, result, receipt, source


def test_exact_source_distinct_objective_and_old_version_unchanged(env):
    cid, wid, old_id, before = base_goal(env)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        old = db.get(GoalVersion, old_id)
        unchanged = deepcopy(old.requirements)
        count = db.scalar(select(func.count()).select_from(AgentRun))
        child, result, _, _ = amend_in(
            env,
            db,
            cid,
            wid,
            depth="掌握",
            priority="required",
            text="  增加 LangGraph 技术栈，要求掌握。\r\n",
        )
        new = db.get(GoalVersion, child.goal_version_id)
        assert new.id != old.id and new.goal_id == old.goal_id and new.version == 2
        assert new.run_id is None and old.run_id is not None
        assert new.source_text == old.source_text and new.requirements == unchanged
        assert new.content_hash == old.content_hash
        assert read_goal_version(db, actor, old_id) == before
        view = read_goal_version(db, actor, new.id)
        objective = view["user_learning_objectives"][0]
        assert objective["technology"] == "LangGraph" and objective["target_depth"] == "掌握"
        assert objective["learning_priority"] == "required" and objective["user_mastery_unchanged"]
        assert objective["source"]["text"].endswith("\r\n")
        assert child.current_execution_id is None and result["execution_id"] is None
        assert child.milestone == "awaiting_goal_confirmation"
        assert db.scalar(select(func.count()).select_from(AgentRun)) == count
        assert not db.scalar(select(ModelReceipt.id))
    assert not env["model"].calls


def test_reader_requires_successful_index_and_replay_precedes_active_cas(env):
    cid, wid, _, _ = base_goal(env)
    app = env["app"]
    with app.db.transaction() as db:
        child, result, receipt, source = amend_in(env, db, cid, wid, finalize=False)
        with pytest.raises(DomainError, match="成功命令"):
            read_goal_version(db, env["actor"], child.goal_version_id)
        receipt.result = {
            "action_receipts": [
                {
                    **result,
                    "action_index": 0,
                    "action_type": "add_learning_objective",
                    "status": "applied",
                }
            ]
        }
        second, _, _, _ = amend_in(env, db, cid, child.id, technology="Rust")
        assert db.get(GoalVersion, second.goal_version_id).version == 3
        replay, original = create_goal_amendment_in(
            app,
            db,
            env["actor"],
            db.get(Conversation, cid),
            db.get(WorkflowSession, wid),
            receipt,
            0,
            technology="LangGraph",
            command_source=source,
        )
        assert replay.id == child.id and original["new_goal_version_id"] == child.goal_version_id
        receipt.result = {
            "action_receipts": [
                {
                    **result,
                    "action_index": 1,
                    "action_type": "add_learning_objective",
                    "status": "applied",
                }
            ]
        }
        with pytest.raises(DomainError):
            read_goal_version(db, env["actor"], child.goal_version_id)


def test_same_parent_different_command_conflicts_and_rollback_has_no_orphans(env):
    cid, wid, old, _ = base_goal(env)
    app = env["app"]
    with app.db.transaction() as db:
        with pytest.raises(RuntimeError):
            with db.begin_nested():
                amend_in(env, db, cid, wid)
                raise RuntimeError("before outer commit")
        assert db.get(CareerGoal, db.get(GoalVersion, old).goal_id).active_version_id == old
        assert not db.scalar(select(GoalAddendum.id))
        amend_in(env, db, cid, wid)
        with pytest.raises(DomainError) as error:
            with db.begin_nested():
                amend_in(env, db, cid, wid, technology="Rust")
        assert error.value.code == "REVISION_CONFLICT"
        assert db.scalar(select(func.count()).select_from(GoalVersion)) == 2


@pytest.mark.parametrize("source_change", ["hash", "span", "target", "priority"])
def test_source_tampering_rejected(env, source_change):
    cid, wid, _, _ = base_goal(env)
    source = dialogue_command_source("补充 LangGraph", "LangGraph")
    if source_change == "hash":
        source["raw_sha256"] = "a" * 64
    if source_change == "span":
        source["target_span"]["start"] += 1
    if source_change == "target":
        source["text"] += "X"
    priority = "required" if source_change == "priority" else "unspecified"
    with env["app"].db.transaction() as db:
        with pytest.raises(DomainError):
            amend_in(env, db, cid, wid, source=source, priority=priority)


def test_structured_source_and_unknown_depth_preserved(env):
    cid, wid, _, _ = base_goal(env)
    fields = {"topic": "LangGraph", "target_depth": None, "learning_priority": "unspecified"}
    with env["app"].db.transaction() as db:
        child, _, _, _ = amend_in(env, db, cid, wid, source=structured_command_source(fields))
        view = read_goal_version(db, env["actor"], child.goal_version_id)
        objective = view["user_learning_objectives"][0]
        assert objective["target_depth"] is None and objective["depth_basis"] == "unspecified"
        assert objective["source"]["fields"] == fields
        assert "text" not in objective["source"]


def test_unicode_repeated_target_requires_explicit_codepoint_span():
    with pytest.raises(DomainError):
        dialogue_command_source("LangGraph，补充 LangGraph", "LangGraph")
    source = dialogue_command_source("🙂\r\n补充 LangGraph", "LangGraph")
    assert source["target_span"] == {"start": 6, "end": 15}
    assert source["byte_count"] != len(source["text"])


@pytest.mark.parametrize("tamper", ["manifest", "receipt", "parent", "version_hash"])
def test_stored_lineage_tampering_fails_closed(env, tamper):
    cid, wid, _, _ = base_goal(env)
    with env["app"].db.transaction() as db:
        child, _, receipt, _ = amend_in(env, db, cid, wid)
        version = db.get(GoalVersion, child.goal_version_id)
        if tamper == "manifest":
            addendum = db.get(GoalAddendum, version.goal_addendum_id)
            addendum.manifest = {**addendum.manifest, "operation": "delete_requirement"}
        elif tamper == "receipt":
            receipt.result = {"action_receipts": []}
        elif tamper == "parent":
            version.parent_goal_version_id = version.id
        else:
            version.version_hash = "a" * 64
        with pytest.raises(DomainError):
            read_goal_version(db, env["actor"], version.id)


def test_legacy_addendum_chain_carries_only_known_provenance(env):
    cid, wid, old, _ = base_goal(env)
    app = env["app"]
    with app.db.transaction() as db:
        conv = db.get(Conversation, cid)
        parent = db.get(WorkflowSession, wid)
        legacy = app.dialogue._add_topic(db, env["actor"], conv, parent, "OldTopic")
        old_row = db.get(GoalAddendum, legacy.target["goal_addendum_id"])
        preserved = deepcopy(old_row.manifest)
        child, _, _, _ = amend_in(env, db, cid, legacy.id)
        view = read_goal_version(db, env["actor"], child.goal_version_id)
        first, second = view["user_learning_objectives"]
        assert first["origin"] == "legacy_user_addendum" and first["technology"] == "OldTopic"
        assert first["target_depth"] is None and first["source"]["kind"] == "legacy_addendum_ref"
        assert "text" not in first["source"] and old_row.manifest == preserved
        assert second["technology"] == "LangGraph"
        assert db.get(GoalVersion, old).version == 1


def test_stale_goal_revision_failure_and_same_key_changed_objective(env):
    cid, wid, _, _ = base_goal(env)
    app = env["app"]
    with app.db.transaction() as db:
        with pytest.raises(DomainError) as error:
            with db.begin_nested():
                amend_in(env, db, cid, wid, expected_goal_revision=99)
        assert error.value.code == "REVISION_CONFLICT"
        _, _, receipt, source = amend_in(env, db, cid, wid)
        with pytest.raises(DomainError) as error:
            create_goal_amendment_in(
                app,
                db,
                env["actor"],
                db.get(Conversation, cid),
                db.get(WorkflowSession, wid),
                receipt,
                0,
                technology="Rust",
                command_source=source,
            )
        assert error.value.code == "IDEMPOTENCY_CONFLICT"


async def test_real_jd_contract_keeps_or_not_optional_nulls_and_offsets(env):
    from test_research_goal_context_v166 import extracted_goal

    document, old_id = await extracted_goal(env)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        old = db.get(GoalVersion, old_id)
        before = deepcopy(old.requirements)
        conv = Conversation(principal_id=actor, namespace_id=env["ns"])
        db.add(conv)
        db.flush()
        workflow = app.workflows.create_in(db, conv)
        workflow.goal_version_id = old_id
        workflow.source_jd_revision_id = document["jd_revision_id"]
        calls = len(env["model"].calls)
        child, _, _, _ = amend_in(
            env,
            db,
            conv.id,
            workflow.id,
            depth="掌握",
            priority="required",
            text="增加 LangGraph 技术栈，要求掌握。",
        )
        new = db.get(GoalVersion, child.goal_version_id)
        assert old.requirements == new.requirements == before
        assert old.source_text == new.source_text == document["raw_text"]
        assert {r["modality"] for r in before["requirements"]} >= {
            "required",
            "preferred",
            "bonus",
            "not_required",
            "unspecified",
        }
        assert any(r["depth"] is None for r in before["requirements"])
        assert {g["operator"] for g in before["groups"]} >= {"OR", "NOT"}
        for row in before["requirements"]:
            assert new.source_text[row["span_start"] : row["span_end"]] == row["quote"]
        assert len(env["model"].calls) == calls
        assert (
            read_goal_version(db, actor, new.id)["user_learning_objectives"][0]["technology"]
            == "LangGraph"
        )


def test_bounded_lineage_rejects_atomically(env, monkeypatch):
    from careerforge.services import goal_amendments

    cid, wid, _, _ = base_goal(env)
    monkeypatch.setattr(goal_amendments, "MAX_OBJECTIVES", 1)
    with env["app"].db.transaction() as db:
        child, _, _, _ = amend_in(env, db, cid, wid)
        with pytest.raises(DomainError):
            with db.begin_nested():
                amend_in(env, db, cid, child.id, technology="Rust")
        assert db.scalar(select(func.count()).select_from(GoalVersion)) == 2


@pytest.mark.parametrize(
    "change",
    [
        "unknown_contract",
        "missing_contract",
        "authority",
        "depth",
        "priority",
        "span",
        "outcome_type",
        "missing_field",
    ],
)
def test_typed_receipt_cannot_counterfeit_source_qualifiers(env, change):
    cid, wid, _, _ = base_goal(env)
    with env["app"].db.transaction() as db:
        child, _, receipt, _ = amend_in(
            env, db, cid, wid, depth="掌握", priority="required", text="增加 LangGraph，要求掌握。"
        )
        plan = deepcopy(receipt.plan)
        if change == "unknown_contract":
            plan["contract_version"] = "unknown"
        elif change == "missing_contract":
            plan.pop("contract_version")
        elif change == "authority":
            plan["actions"][0]["action"]["actor"] = env["actor"]
        elif change == "depth":
            plan["actions"][0]["action"]["target_depth"] = "了解"
        elif change == "priority":
            plan["actions"][0]["action"]["learning_priority"] = "preferred"
        elif change == "span":
            plan["actions"][0]["action"]["target_span"]["start"] += 1
        elif change == "missing_field":
            plan["actions"][0]["action"].pop("priority_span")
        else:
            result = deepcopy(receipt.result)
            result["action_receipts"][0]["action_type"] = "accept_plan"
            receipt.result = result
        receipt.plan, receipt.action_plan_hash = plan, fingerprint(plan)
        with pytest.raises(DomainError):
            read_goal_version(db, env["actor"], child.goal_version_id)


def test_old_typed_target_action_retains_v1_shape(env):
    from careerforge.contracts_conversation_actions_v1 import ConversationActionPlan

    cid, wid, _, _ = base_goal(env)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        conv, parent = db.get(Conversation, cid), db.get(WorkflowSession, wid)
        key = uuid4().hex
        plan = ConversationActionPlan(
            conversation_id=cid,
            expected_focus_revision=conv.revision,
            target_workflow_id=wid,
            idempotency_key=key,
            actions=[
                {"step_id": "1", "action": {"type": "add_learning_topic", "target": "LangGraph"}}
            ],
        ).model_dump(mode="json")
        frozen = deepcopy(plan)
        receipt = ConversationActionReceipt(
            conversation_id=cid,
            workflow_id=wid,
            idempotency_key=key,
            request_hash=fingerprint({"text": "补充 LangGraph"}),
            action_plan_hash=fingerprint(plan),
            plan=plan,
            result={},
        )
        db.add(receipt)
        db.flush()
        child, result = create_goal_amendment_in(
            app,
            db,
            actor,
            conv,
            parent,
            receipt,
            0,
            technology="LangGraph",
            command_source=dialogue_command_source("补充 LangGraph", "LangGraph"),
        )
        receipt.result = {
            "action_receipts": [
                {
                    **result,
                    "action_index": 0,
                    "action_type": "add_learning_topic",
                    "status": "applied",
                }
            ]
        }
        assert (
            read_goal_version(db, actor, child.goal_version_id)["user_learning_objectives"][0][
                "target_depth"
            ]
            is None
        )
        assert receipt.plan == frozen


def test_indexed_compound_receipt_not_final_workflow_pointer_is_authority(env):
    cid, wid, _, _ = base_goal(env)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        conv, parent = db.get(Conversation, cid), db.get(WorkflowSession, wid)
        key = uuid4().hex
        plan = ConversationActionPlanV2(
            conversation_id=cid,
            expected_focus_revision=conv.revision,
            target_workflow_id=wid,
            idempotency_key=key,
            actions=[
                {"step_id": "1", "action": {"type": "query_progress"}},
                {
                    "step_id": "2",
                    "action": {"type": "add_learning_objective", "target": "LangGraph"},
                    "depends_on": ["1"],
                },
            ],
        ).model_dump(mode="json")
        receipt = ConversationActionReceipt(
            conversation_id=cid,
            workflow_id=wid,
            idempotency_key=key,
            request_hash=fingerprint({"text": "补充 LangGraph"}),
            action_plan_hash=fingerprint(plan),
            plan=plan,
            result={},
        )
        db.add(receipt)
        db.flush()
        child, result = create_goal_amendment_in(
            app,
            db,
            actor,
            conv,
            parent,
            receipt,
            1,
            technology="LangGraph",
            command_source=dialogue_command_source("补充 LangGraph", "LangGraph"),
        )
        receipt.result = {
            "action_receipts": [
                {"action_index": 0, "action_type": "query_progress", "status": "applied"},
                {
                    **result,
                    "action_index": 1,
                    "action_type": "add_learning_objective",
                    "status": "applied",
                },
            ]
        }
        assert receipt.workflow_id == wid and receipt.workflow_id != child.id
        view = read_goal_version(db, actor, child.goal_version_id)
        assert view["command_action_index"] == 1
        corrupted = deepcopy(receipt.result)
        corrupted["action_receipts"][1]["action_index"] = True
        receipt.result = corrupted
        with pytest.raises(DomainError):
            read_goal_version(db, actor, child.goal_version_id)
