"""Lossless model payload and size guards; synthetic provider, no live semantic score."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_learning_loop_v16 import configure_planner

from careerforge.adapters.model import strict_schema
from careerforge.contracts_learning_ext import LearningPreferences, ProfileUpdate
from careerforge.contracts_session_plan_v166 import SessionPlanInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, CareerGoal, GoalVersion, ModelReceipt
from careerforge.models_learning_ext import PlanProposalManifest
from careerforge.services.knowledge import KnowledgeService
from careerforge.services.learning_ext import RUBRIC_TEXT, STEP_TEXT, _planner_contract
from careerforge.services.learning_projection import text_catalog
from careerforge.services.planner_context_v166 import (
    BOUNDED_PLANNER_CONTEXT_VERSION,
    PLANNER_CONTEXT_VERSION,
    planner_model_context,
    projection_rules,
    require_visible_choices,
)
from careerforge.util import canonical, fingerprint


def snapshot():
    return json.loads(
        (Path(__file__).parent / "fixtures/planner_context_native_public_v166.json").read_text()
    )["input_manifest"]


def dependencies(frozen):
    return sorted(
        {
            frozen["selection_manifest_id"],
            frozen["goal"]["goal_version_id"],
            *(
                identifier
                for pack in frozen["units"]
                for identifier in KnowledgeService.pack_dependencies(pack)
            ),
        }
    )


def request(frozen):
    rules, schema = _planner_contract(frozen)
    context = planner_model_context(frozen, STEP_TEXT, RUBRIC_TEXT)
    return {
        "role": "learning_planner",
        "context": context,
        "messages": [
            {"role": "system", "content": rules + projection_rules(frozen)},
            {"role": "user", "content": "UNTRUSTED_TASK_DATA\n" + canonical(context)},
        ],
        "schema": strict_schema(schema.model_json_schema()),
        "tools": [],
        "max_output_tokens": 8192,
        "profile_version": frozen["model_profile"],
        "data_classes": frozen["data_classes"],
        "access_dependencies": dependencies(frozen),
    }


def expand(value, groups):
    if isinstance(value, list):
        return [expand(item, groups) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        ("group_path" if key == "group_path_refs" else key): (
            [deepcopy(groups[ref]) for ref in item]
            if key == "group_path_refs"
            else expand(item, groups)
        )
        for key, item in value.items()
    }


def test_real_width_three_same_source_units_twenty_requirements_preserve_all_data_under_cap():
    original = snapshot()
    frozen = {**original, "planner_model_context_version": PLANNER_CONTEXT_VERSION}
    before = fingerprint(frozen)
    old_request, new_request = request(original), request(frozen)
    assert (
        len(original["units"]) == 3 and len({unit["source_id"] for unit in original["units"]}) == 1
    )
    assert len(original["goal"]["requirements"]["requirements"]) == 20
    assert len(canonical(old_request).encode()) > 250_000
    assert len(canonical(new_request).encode()) < 225_000
    context = new_request["context"]
    assert context["frozen_input"]["units"] == original["units"]
    assert expand(context["frozen_input"], context["group_catalog"]) == frozen
    restored = []
    for entry in context["text_catalog"]:
        row = {key: value for key, value in entry.items() if key != "scope_ref"}
        row.update(context["scope_catalog"].get(entry.get("scope_ref"), {}))
        restored.append(expand(row, context["group_catalog"]))
    assert restored == list(text_catalog(frozen).values())
    assert fingerprint(frozen) == before
    assert {row["operator"] for row in context["group_catalog"].values()} >= {"AND", "OR"}
    assert any(entry.get("modality") == "bonus" for entry in restored)
    assert any(
        entry.get("required_depth") is None
        for entry in restored
        if entry["kind"] == "goal_requirement"
    )


def test_budgeted_projection_preserves_goal_and_whole_visible_evidence():
    frozen = {**snapshot(), "planner_model_context_version": BOUNDED_PLANNER_CONTEXT_VERSION}
    frozen["units"][1]["source_id"] = "second-pinned-source"
    first = frozen["units"][0]
    oversized = first["facts"][0]
    oversized["text"] = "source exact text " * 12_000
    original = fingerprint(frozen)
    payload = request(frozen)
    context = payload["context"]
    assert len(canonical(payload).encode()) < 250_000
    assert expand(context["frozen_input"]["goal"], context["group_catalog"]) == frozen["goal"]
    assert fingerprint(frozen) == original
    visible = {entry["text_id"]: entry for entry in context["text_catalog"]}
    assert f"fact.{oversized['fact_atom_id']}.text" not in visible
    eligible_sources = {
        unit["source_id"] for unit in context["frozen_input"]["units"] if unit["facts"]
    }
    assert len(eligible_sources) == 2
    authoritative = text_catalog(frozen)
    for unit in context["frozen_input"]["units"]:
        for fact in unit["facts"]:
            entries = [
                entry
                for entry in authoritative.values()
                if entry.get("fact_atom_id") == fact["fact_atom_id"]
            ]
            assert [entry["text_id"] for entry in entries] == fact["text_refs"]
            assert all(visible[entry["text_id"]]["text"] == entry["text"] for entry in entries)
    assert planner_model_context(frozen, STEP_TEXT, RUBRIC_TEXT) == context

    def draft(facts, reference):
        return SimpleNamespace(
            modules=[SimpleNamespace(publication_id=first["publication_id"], focus_fact_ids=facts)],
            model_dump=lambda **_: {"nested": [{"text_id": reference}]},
        )

    with pytest.raises(DomainError) as error:
        require_visible_choices(draft([oversized["fact_atom_id"]], "goal.text"), context)
    assert error.value.code == "PLAN_EVIDENCE"
    with pytest.raises(DomainError) as error:
        require_visible_choices(draft([], f"fact.{oversized['fact_atom_id']}.text"), context)
    assert error.value.code == "PLAN_TEXT_REFERENCE"
    require_visible_choices(draft([], "goal.text"), context)


def test_historical_model_context_and_system_remain_byte_identical():
    frozen = snapshot()
    expected = {
        "frozen_input": frozen,
        "step_catalog": STEP_TEXT,
        "rubric_catalog": RUBRIC_TEXT,
        "text_catalog": list(text_catalog(frozen).values()),
    }
    assert canonical(planner_model_context(frozen, STEP_TEXT, RUBRIC_TEXT)) == canonical(expected)
    assert projection_rules(frozen) == ""


def test_unknown_model_projection_stops_before_any_call():
    frozen = {**snapshot(), "planner_model_context_version": "unknown"}
    with pytest.raises(DomainError) as error:
        planner_model_context(frozen, STEP_TEXT, RUBRIC_TEXT)
    assert error.value.code == "PLAN_MODEL_CONTEXT_VERSION"


@pytest.mark.asyncio
async def test_existing_gateway_limit_still_rejects_oversized_context_before_charge(env):
    frozen = snapshot()
    rules, schema = _planner_contract(frozen)
    with pytest.raises(DomainError) as error:
        await env["app"].gateway.call(
            "not-admitted",
            0,
            "none",
            "oversize",
            "learning_planner",
            rules,
            planner_model_context(frozen, STEP_TEXT, RUBRIC_TEXT),
            schema,
            data_classes=frozen["data_classes"],
            access_dependencies=dependencies(frozen),
        )
    assert error.value.code == "MODEL_CONTEXT_LIMIT"
    assert env["model"].calls == []
    # A new marker does not remove the guard or truncate genuinely huge data.
    frozen.update(
        planner_model_context_version=PLANNER_CONTEXT_VERSION, title="不可截断的目标" * 30000
    )
    with pytest.raises(DomainError) as error:
        await env["app"].gateway.call(
            "not-admitted",
            0,
            "none",
            "oversize-new",
            "learning_planner",
            rules + projection_rules(frozen),
            planner_model_context(frozen, STEP_TEXT, RUBRIC_TEXT),
            schema,
        )
    assert error.value.code == "MODEL_CONTEXT_LIMIT" and env["model"].calls == []


@pytest.mark.asyncio
async def test_real_plan_service_uses_compact_request_but_retains_complete_authority_manifest(env):
    app, actor = env["app"], env["actor"]
    publications, sources = [], []
    for index in range(3):
        source, publication, _ = await publish_fixture(env, key=f"context-unit-{index}")
        sources.append(source)
        publications.append(publication)
    assert len(set(sources)) == 1 and len(set(publications)) == 3
    original = snapshot()["goal"]
    # Explicit synthetic legacy-owned goal: actual source-bound JD registration
    # is tested separately by JD tests and the native continuous replay.
    run_id = app.runtime.create(
        actor, env["ns"], env["funding"], "learning_plan", {}, 1, "goal-fixture"
    )
    with app.db.transaction() as db:
        goal = CareerGoal(principal_id=actor, namespace_id=env["ns"], title="Context-size test")
        db.add(goal)
        db.flush()
        version = GoalVersion(
            goal_id=goal.id,
            version=1,
            source_text=original["source_text"],
            content_hash=fingerprint(original["source_text"]),
            requirements=original["requirements"],
            run_id=run_id,
        )
        db.add(version)
        db.flush()
        goal_id = version.id
    profile = app.learning.update_profile(
        actor,
        ProfileUpdate(
            expected_revision=1, preferences=LearningPreferences(goal_version_id=goal_id)
        ),
    )
    configure_planner(env)
    started = app.learning.start_session_plan(
        actor,
        SessionPlanInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            publication_ids=publications,
            learning_intent="function",
            title="三份来源与复杂需求",
            total_minutes=25,
            expected_profile_revision=profile["revision"],
            idempotency_key="compact-plan",
        ),
    )
    with app.db.read() as db:
        admitted = db.get(AgentRun, started["run_id"])
        assert admitted.input_manifest["planner_model_context_version"] == PLANNER_CONTEXT_VERSION
        assert admitted.input_hash == fingerprint(admitted.input_manifest)
    await app.execute(started["run_id"])
    result = app.runtime.stage(started["run_id"], "result")
    assert result and not result["accepted"]
    call = [item for item in env["model"].calls if item["role"] == "learning_planner"][-1]
    assert call["context"]["planner_model_context_version"] == PLANNER_CONTEXT_VERSION
    assert len(canonical(call).encode()) < 250_000
    assert len(call["context"]["frozen_input"]["units"]) == 3
    assert len(call["context"]["frozen_input"]["goal"]["requirements"]["requirements"]) == 20
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
        manifest = db.scalar(
            select(PlanProposalManifest).where(PlanProposalManifest.run_id == run.id)
        )
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run.id))
        assert run.calls_consumed == 1 and receipt.status == "completed"
        assert manifest.payload["input"] == run.input_manifest
        assert "group_path" in run.input_manifest["goal"]["requirements"]["requirements"][0]
        assert "scope_catalog" not in run.input_manifest
        assert run.input_manifest["planner_model_context_version"] == PLANNER_CONTEXT_VERSION
