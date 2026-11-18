"""Scoped user additions preserve research output schemas and existing full-JD semantics."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select
from support import FILES
from test_goal_amendments_v1 import amend_in, base_goal

from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, StageArtifact
from careerforge.services.goal_build_scope import (
    freeze_goal_build_scope_in,
    scoped_research_task_in,
    validate_goal_build_scope_in,
)
from careerforge.services.research_coverage import (
    freeze_research_task,
    read_goal_version,
    validate_research_run,
)
from careerforge.util import fingerprint


def scoped_goal(env, *, depth="掌握", technology="LangGraph", fixed=True):
    cid, wid, old, before = base_goal(env)
    app, actor = env["app"], env["actor"]
    source = (
        app.sources.import_source(
            actor,
            ImportSourceInput(
                namespace_id=env["ns"],
                repository_key="fixtures/goal-scope",
                commit_sha="a" * 40,
                license_id="MIT",
                files=FILES,
                source_metadata={"synthetic": True},
            ),
        )
        if fixed
        else None
    )
    with app.db.transaction() as db:
        child, _, _, _ = amend_in(
            env,
            db,
            cid,
            wid,
            technology=technology,
            depth=depth,
            text="增加 " + technology + ("，掌握" if depth else ""),
        )
        scope = freeze_goal_build_scope_in(db, actor, child, source_ids=[source] if source else [])
        child.target = {**child.target, "goal_build_scope": scope}
        return child.goal_version_id, scope, source, old, before


def build_body(env, version, scope, source, **changes):
    params = dict(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        goal_version_id=version,
        source_id=source,
        question=scope["original_question"],
        required_dimensions=scope["required_dimensions"],
        topic_keys=[item["topic_key"] for item in scope["topic_refs"]],
        research_scope=scope["research_scope"],
        call_cap=20,
        max_projects=1,
        max_units_per_project=1,
        idempotency_key=uuid4().hex,
    )
    params.update(changes)
    return KnowledgeBuildInput(**params)


def test_empty_employer_subset_requires_valid_real_objective(env):
    version, scope, _, old, before = scoped_goal(env)
    app, actor = env["app"], env["actor"]
    with app.db.read() as db:
        snapshot = read_goal_version(db, actor, version)
        task = scoped_research_task_in(db, actor, scope, snapshot)
        assert task.requirements == [] and task.unresolved_requirement_ids == []
        assert task.goal_version_id == version
        assert set(task.required_dimensions) >= {
            "purpose",
            "implementation",
            "boundaries",
            "tradeoffs",
        }
        assert snapshot["unresolved_requirement_ids"] == ["requirements_not_extracted"]
        assert read_goal_version(db, actor, old) == before
        legacy = freeze_research_task("question", goal_version_id=old, requirements=[])
        assert legacy.unresolved_requirement_ids == ["requirements_not_extracted"]
        invalid = {**scope, "user_objective_id": "uobj-" + "0" * 24}
        invalid["scope_hash"] = fingerprint(
            {key: value for key, value in invalid.items() if key != "scope_hash"}
        )
        with pytest.raises(DomainError):
            scoped_research_task_in(db, actor, invalid, snapshot)


@pytest.mark.parametrize("change", ["objective", "question", "depth", "source", "marker", "extra"])
def test_scope_tampering_fails_even_with_recomputed_scope_hash(env, change):
    _, scope, _, _, _ = scoped_goal(env)
    corrupt = deepcopy(scope)
    if change == "objective":
        corrupt["user_objective_id"] = "uobj-" + "1" * 24
    elif change == "question":
        corrupt["original_question"] = "unrelated question"
        corrupt["question_hash"] = fingerprint(corrupt["original_question"])
    elif change == "depth":
        corrupt["required_dimensions"] = ["purpose"]
    elif change == "source":
        corrupt["source_restrictions"][0]["commit_sha"] = "b" * 40
    elif change == "marker":
        corrupt["contract"] = "future"
    else:
        corrupt["allow_empty"] = True
    corrupt["scope_hash"] = fingerprint(
        {key: value for key, value in corrupt.items() if key != "scope_hash"}
    )
    with env["app"].db.read() as db:
        with pytest.raises(DomainError):
            validate_goal_build_scope_in(db, env["actor"], corrupt)


def test_missing_scope_or_changed_build_question_refused_before_reservation(env):
    version, scope, source, _, _ = scoped_goal(env)
    app = env["app"]
    body = build_body(env, version, scope, source)
    with pytest.raises(DomainError) as error:
        app.build.start(env["actor"], body)
    assert error.value.code == "GOAL_BUILD_SCOPE"
    with pytest.raises(DomainError):
        app.build.start(
            env["actor"], body.model_copy(update={"question": "different"}), goal_build_scope=scope
        )
    assert not env["model"].calls


async def test_scope_persists_root_context_children_and_replays_without_expansion(env):
    version, scope, source, _, _ = scoped_goal(env)
    app, actor = env["app"], env["actor"]
    body = build_body(env, version, scope, source)
    run_id = app.build.start(actor, body, goal_build_scope=scope)
    assert app.build.start(actor, body, goal_build_scope=scope) == run_id
    with pytest.raises(DomainError) as error:
        app.build.start(actor, body)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    epoch = app.runtime.claim(run_id, "scope-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
        validate_research_run(db, actor, run)
    topics, context = await app.build.prepare_goal_topics(run, epoch, "scope-worker")
    assert topics == ["framework.langgraph"]
    assert (
        context["goal_build_scope"] == scope
        and context["goal_snapshot"]["goal_version_id"] == version
    )
    assert context["research_task"]["requirements"] == []
    prepared = await app.build.prepare_sources(run, epoch, "scope-worker", topics, context)
    child, _ = await app.build.ensure_analysis_child(run, epoch, "scope-worker", prepared, context)
    with app.db.read() as db:
        analysis = db.get(AgentRun, child["run_id"])
        assert analysis.input_manifest["goal_build_scope"] == scope
        assert app.build._research_context(analysis)["goal_build_scope"] == scope
        validate_research_run(db, actor, analysis)
    assert not env["model"].calls


@pytest.mark.parametrize(
    "change",
    [
        "root_missing",
        "stage_missing",
        "stage_changed",
        "child_missing",
        "child_changed",
        "child_no_research",
        "child_no_markers",
        "root_no_markers",
    ],
)
async def test_scope_omission_or_tampering_fences_paid_dispatch(env, change):
    version, scope, source, _, _ = scoped_goal(env)
    app, actor = env["app"], env["actor"]
    body = build_body(env, version, scope, source)
    run_id = app.build.start(actor, body, goal_build_scope=scope)
    epoch = app.runtime.claim(run_id, "scope-worker")
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
    topics, context = await app.build.prepare_goal_topics(run, epoch, "scope-worker")
    prepared = await app.build.prepare_sources(run, epoch, "scope-worker", topics, context)
    child, _ = await app.build.ensure_analysis_child(run, epoch, "scope-worker", prepared, context)
    child_id = child["run_id"]
    with app.db.transaction() as db:
        target = db.get(
            AgentRun, run_id if change in {"root_missing", "root_no_markers"} else child_id
        )
        if change.startswith("stage"):
            stage = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run_id, StageArtifact.stage_key == "research_context"
                )
            )
            payload = deepcopy(stage.payload)
            if change == "stage_missing":
                payload.pop("goal_build_scope")
            else:
                payload["goal_build_scope"]["original_question"] += " altered"
            stage.payload = payload
        else:
            manifest = deepcopy(target.input_manifest)
            if change in {"child_no_markers", "root_no_markers"}:
                manifest.pop("research_contract_version", None)
                manifest.pop("goal_build_scope", None)
                manifest.pop("goal_snapshot", None)
                manifest.pop("research_task", None)
            elif change == "child_no_research":
                manifest.pop("research_contract_version")
            elif change.endswith("missing"):
                manifest.pop("goal_build_scope")
            else:
                manifest["goal_build_scope"]["original_question"] += " altered"
            target.input_manifest = manifest
            target.input_hash = fingerprint(manifest)
    calls = len(env["model"].calls)
    with app.db.read() as db:
        with pytest.raises(DomainError):
            validate_research_run(db, actor, db.get(AgentRun, child_id))
    with pytest.raises(DomainError):
        child_epoch = app.runtime.claim(child_id, "scope-child")
        await app.gateway.call(
            child_id, child_epoch, "scope-child", "scope-corruption", "analysis", "fixture", {}
        )
    assert len(env["model"].calls) == calls


def test_pending_mapping_does_not_invent_topic_or_drop_objective(env):
    from careerforge.models_dialogue import WorkflowSession

    cid, wid, _, _ = base_goal(env)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        child, _, _, _ = amend_in(env, db, cid, wid, technology="UnregisteredLanguageXYZ")
        workflow_id, version_id = child.id, child.goal_version_id
    with app.db.transaction() as db:
        with pytest.raises(DomainError) as error:
            freeze_goal_build_scope_in(db, actor, db.get(WorkflowSession, workflow_id))
        assert error.value.code == "GOAL_MAPPING_PENDING"
        objective = read_goal_version(db, actor, version_id)["user_learning_objectives"][0]
        assert objective["technology"] == "UnregisteredLanguageXYZ"
        assert objective["mapping"]["status"] == "pending_mapping"
        assert db.get(WorkflowSession, workflow_id).current_execution_id is None
