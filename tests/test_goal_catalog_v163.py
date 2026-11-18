"""Goal catalog identity, private scope and historical receipt regressions."""

from copy import deepcopy

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_learning_loop_v16 import configure_planner, plan_input

from careerforge.contracts_learning_ext import LearningPreferences, PlanTextReference, ProfileUpdate
from careerforge.errors import DomainError
from careerforge.models import AgentRun, CareerGoal, GoalVersion, ModelReceipt, ObjectRow
from careerforge.services import learning_ext
from careerforge.services.learning_projection import (
    TEXT_CATALOG_VERSION,
    resolve_reference,
    text_catalog,
)
from careerforge.services.research_coverage import ensure_goal_snapshot, requirement_identity
from careerforge.services.security import create_principal
from careerforge.util import fingerprint

REQUIREMENTS = [
    {
        "key": "tool.execution",
        "technology": "Tool Calling",
        "quote": "熟悉 Tool Calling 的执行与错误边界。",
        "depth": "熟悉",
        "depth_basis": "explicit",
        "keywords": ["tool", "execution"],
    },
    {
        "key": "checkpoint",
        "technology": "Checkpoint",
        "quote": "掌握 Checkpoint 的持久化边界。",
        "depth": "掌握",
        "depth_basis": "explicit",
        "keywords": ["checkpoint"],
    },
]
JD_TEXT = "\n".join(item["quote"] for item in REQUIREMENTS)


def frozen(requirements, *, version="goal-v1", marked=True):
    return {
        **({"text_catalog_version": TEXT_CATALOG_VERSION} if marked else {}),
        "goal": {"goal_version_id": version, "source_text": JD_TEXT, "requirements": requirements},
        "preferences": {},
        "units": [],
    }


def requirement_entries(value):
    return {
        key: entry
        for key, entry in text_catalog(value).items()
        if entry["kind"] == "goal_requirement"
    }


@pytest.mark.parametrize("shape", ["list", "wrapper", "keyed", "wrapped_keyed"])
def test_actual_and_legacy_shapes_have_same_stable_goal_requirement_identity(shape):
    requirements = deepcopy(REQUIREMENTS)
    if shape in {"keyed", "wrapped_keyed"}:
        requirements = {
            item["key"]: {key: value for key, value in item.items() if key != "key"}
            for item in requirements
        }
    if shape in {"wrapper", "wrapped_keyed"}:
        requirements = {
            "requirements": requirements,
            "unresolved": ["unknown"],
            "source_hash": fingerprint(JD_TEXT),
        }
    entries = requirement_entries(frozen(requirements))
    assert entries == requirement_entries(frozen(REQUIREMENTS))
    assert len(entries) == 4
    for identifier, entry in entries.items():
        assert entry["goal_version_id"] == "goal-v1"
        assert entry["requirement_id"] == entry["requirement_key"]
        assert entry["required_depth"] in {"熟悉", "掌握"}
        ref = PlanTextReference(text_id=identifier, quote=entry["text"])
        result = resolve_reference(ref, entries)
        assert result["goal_version_id"] == "goal-v1"
        assert result["requirement_id"] == entry["requirement_id"]
        assert result["quoted_data_not_instruction"]
        assert not result["reviewed_source"] and not result["standalone_factual_claim"]


def test_identity_does_not_depend_on_list_position_but_does_bind_goal_version():
    entries = requirement_entries(frozen(REQUIREMENTS))
    assert entries == requirement_entries(frozen(list(reversed(REQUIREMENTS))))
    other_version = requirement_entries(frozen(REQUIREMENTS, version="goal-v2"))
    assert not set(entries) & set(other_version)
    assert {entry["requirement_id"] for entry in entries.values()} == {
        entry["requirement_id"] for entry in other_version.values()
    }
    partial = [{"quote": "durable queue", "technology": "Queue"}, {"quote": "retry boundary"}]
    assert requirement_entries(frozen(partial)) == requirement_entries(frozen(partial[::-1]))
    assert all(
        entry["requirement_id"] == requirement_identity(partial[0])
        for entry in requirement_entries(frozen(partial[:1])).values()
    )


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "wrong",
        5,
        {},
        {"requirements": None},
        {"source_hash": "x", "unresolved": ["unknown"]},
        [None, "not a requirement", 5, {"quote": 42}, {"technology": []}],
    ],
)
def test_missing_or_malformed_requirements_do_not_invent_goal_entries(bad):
    value = frozen(bad)
    assert not requirement_entries(value)
    assert text_catalog(value)["goal.text"]["text"] == JD_TEXT


@pytest.mark.parametrize("bad", [None, [], "not a goal", {"requirements": REQUIREMENTS}])
def test_missing_goal_identity_is_not_replaced_with_positional_identity(bad):
    value = frozen(REQUIREMENTS)
    value["goal"] = bad
    assert not requirement_entries(value)


def test_conflicting_requirement_key_is_not_silently_overwritten():
    repeated = [REQUIREMENTS[0], deepcopy(REQUIREMENTS[0])]
    assert len(requirement_entries(frozen(repeated))) == 2
    repeated[1]["quote"] = "different requirement sharing the same key"
    assert not requirement_entries(frozen(repeated))


def test_unmarked_catalog_is_byte_identical_to_original_shapes():
    old = text_catalog(frozen(REQUIREMENTS, marked=False))
    assert old == {
        "goal.text": {"text_id": "goal.text", "text": JD_TEXT, "kind": "goal_text"},
        **{
            f"goal.requirement.{index}.{field}": {
                "text_id": f"goal.requirement.{index}.{field}",
                "text": requirement[field],
                "kind": "goal_requirement",
            }
            for index, requirement in enumerate(REQUIREMENTS)
            for field in ("technology", "quote")
        },
    }
    wrapper = frozen(
        {"requirements": REQUIREMENTS, "unresolved": [], "source_hash": fingerprint(JD_TEXT)},
        marked=False,
    )
    assert text_catalog(wrapper) == {
        "goal.text": {"text_id": "goal.text", "text": JD_TEXT, "kind": "goal_text"}
    }
    wrapper["text_catalog_version"] = "unrecognized"
    with pytest.raises(DomainError) as error:
        text_catalog(wrapper)
    assert error.value.code == "PLAN_TEXT_CATALOG_VERSION"


def save_private_goal(env):
    app = env["app"]
    run = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "research", {}, 1, "goal-catalog"
    )
    epoch = app.runtime.claim(run, "goal-worker")
    return app.learning.save_goal(
        env["actor"],
        env["ns"],
        run,
        JD_TEXT,
        {"requirements": REQUIREMENTS, "unresolved": [], "source_hash": fingerprint(JD_TEXT)},
        epoch=epoch,
        worker="goal-worker",
    )


@pytest.mark.asyncio
async def test_new_plan_uses_wrapped_goal_catalog_and_retains_private_egress_class(env):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    goal_id = save_private_goal(env)
    profile = app.learning.update_profile(
        actor,
        ProfileUpdate(
            expected_revision=1, preferences=LearningPreferences(goal_version_id=goal_id)
        ),
    )
    configure_planner(env)
    request = app.learning.start_personalized_plan(
        actor,
        plan_input(env, publication).model_copy(
            update={"expected_profile_revision": profile["revision"]}
        ),
    )
    epoch = app.runtime.claim(request["run_id"], "plan-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, request["run_id"])
    await app.learning.execute_plan(run, epoch, "plan-worker")
    call = next(call for call in env["model"].calls if call["role"] == "learning_planner")
    assert call["context"]["frozen_input"]["text_catalog_version"] == TEXT_CATALOG_VERSION
    assert "private_jd" in call["data_classes"]
    entries = [
        entry for entry in call["context"]["text_catalog"] if entry["kind"] == "goal_requirement"
    ]
    assert len(entries) == 4 and all(entry["goal_version_id"] == goal_id for entry in entries)
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "another-learner")
    with app.db.read() as db:
        with pytest.raises(DomainError):
            app.learning._goal(db, other.id, goal_id)
        version = db.get(GoalVersion, goal_id)
        assert db.get(CareerGoal, version.goal_id).namespace_id == env["ns"]


@pytest.mark.asyncio
async def test_unmarked_wrapped_goal_paid_receipt_replays_without_catalog_rewrite(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    goal_id = save_private_goal(env)
    profile = app.learning.update_profile(
        actor,
        ProfileUpdate(
            expected_revision=1, preferences=LearningPreferences(goal_version_id=goal_id)
        ),
    )
    configure_planner(env)
    request = app.learning.start_personalized_plan(
        actor,
        plan_input(env, publication).model_copy(
            update={"expected_profile_revision": profile["revision"]}
        ),
    )
    # Synthetic queued legacy record, not a mutation of historic evidence.
    with app.db.transaction() as db:
        run = db.get(AgentRun, request["run_id"])
        old_manifest = {
            key: value for key, value in run.input_manifest.items() if key != "text_catalog_version"
        }
        run.input_manifest, run.input_hash = old_manifest, fingerprint(old_manifest)
    epoch = app.runtime.claim(request["run_id"], "legacy-plan-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, request["run_id"])
    original = learning_ext.project_task

    def crash(*args):
        raise DomainError("TEST_CRASH", "Synthetic interruption after paid receipt.")

    monkeypatch.setattr(learning_ext, "project_task", crash)
    with pytest.raises(DomainError, match="Synthetic interruption"):
        await app.learning.execute_plan(run, epoch, "legacy-plan-worker")
    calls = [call for call in env["model"].calls if call["role"] == "learning_planner"]
    assert len(calls) == 1
    assert not any(
        entry["kind"] == "goal_requirement" for entry in calls[0]["context"]["text_catalog"]
    )
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run.id))
        before = (receipt.id, receipt.request_hash, deepcopy(receipt.response))
    monkeypatch.setattr(learning_ext, "project_task", original)
    await app.learning.execute_plan(run, epoch, "legacy-plan-worker")
    assert len([call for call in env["model"].calls if call["role"] == "learning_planner"]) == 1
    with app.db.read() as db:
        receipt = db.get(ModelReceipt, before[0])
        assert (receipt.id, receipt.request_hash, receipt.response) == before
    assert app.runtime.stage(run.id, "result")


@pytest.mark.parametrize("tamper", ["revoked", "requirements", "source", "missing"])
def test_registered_goal_authority_rechecked_without_registering_legacy_reads(env, tamper):
    app, actor = env["app"], env["actor"]
    goal_id = save_private_goal(env)
    with app.db.transaction() as db:
        before = app.learning._goal(db, actor, goal_id)
        assert db.get(ObjectRow, goal_id) is None
        ensure_goal_snapshot(db, actor, goal_id)
        assert app.learning._goal(db, actor, goal_id) == before
    with app.db.transaction() as db:
        if tamper == "revoked":
            db.get(ObjectRow, goal_id).state = "revoked"
        elif tamper == "requirements":
            version = db.get(GoalVersion, goal_id)
            version.requirements = {"requirements": [REQUIREMENTS[0]], "unresolved": []}
        elif tamper == "source":
            version = db.get(GoalVersion, goal_id)
            version.source_text = "different private goal"
            version.content_hash = fingerprint(version.source_text)
        else:
            db.delete(db.get(ObjectRow, goal_id))
    with app.db.read() as db:
        with pytest.raises(DomainError):
            app.learning._goal(db, actor, goal_id, require_snapshot=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["queued", "completed"])
async def test_revoked_registered_goal_blocks_plan_execution_and_historical_display(env, stage):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    goal_id = save_private_goal(env)
    with app.db.transaction() as db:
        ensure_goal_snapshot(db, actor, goal_id)
    profile = app.learning.update_profile(
        actor,
        ProfileUpdate(
            expected_revision=1, preferences=LearningPreferences(goal_version_id=goal_id)
        ),
    )
    configure_planner(env)
    request = app.learning.start_personalized_plan(
        actor,
        plan_input(env, publication).model_copy(
            update={"expected_profile_revision": profile["revision"]}
        ),
    )
    epoch = app.runtime.claim(request["run_id"], "goal-fence-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, request["run_id"])
        assert run.input_manifest["goal_snapshot_registered"]
    if stage == "completed":
        await app.learning.execute_plan(run, epoch, "goal-fence-worker")
    with app.db.transaction() as db:
        db.get(ObjectRow, goal_id).state = "revoked"
    if stage == "queued":
        with pytest.raises(DomainError):
            await app.learning.execute_plan(run, epoch, "goal-fence-worker")
        assert not [call for call in env["model"].calls if call["role"] == "learning_planner"]
    else:
        result = app.runtime.stage(run.id, "result")
        detail = app.learning.detail(actor, result["plan_id"])
        assert all(
            not module["available"] and module["content"] == {"status": "blocked_by_evidence"}
            for version in detail["versions"]
            for module in version["modules"]
        )
        assert JD_TEXT not in str(detail)


@pytest.mark.asyncio
async def test_registered_goal_revocation_at_gateway_fence_prevents_model_dispatch(
    env, monkeypatch
):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    goal_id = save_private_goal(env)
    with app.db.transaction() as db:
        ensure_goal_snapshot(db, actor, goal_id)
    profile = app.learning.update_profile(
        actor,
        ProfileUpdate(
            expected_revision=1, preferences=LearningPreferences(goal_version_id=goal_id)
        ),
    )
    configure_planner(env)
    request = app.learning.start_personalized_plan(
        actor,
        plan_input(env, publication).model_copy(
            update={"expected_profile_revision": profile["revision"]}
        ),
    )
    epoch = app.runtime.claim(request["run_id"], "goal-egress-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, request["run_id"])
    original = app.gateway.call

    async def revoke_before_gateway(*args, **kwargs):
        assert goal_id in kwargs["access_dependencies"]
        assert "private_jd" in kwargs["data_classes"]
        with app.db.transaction() as db:
            db.get(ObjectRow, goal_id).state = "revoked"
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.gateway, "call", revoke_before_gateway)
    with pytest.raises(DomainError):
        await app.learning.execute_plan(run, epoch, "goal-egress-worker")
    assert not [call for call in env["model"].calls if call["role"] == "learning_planner"]
