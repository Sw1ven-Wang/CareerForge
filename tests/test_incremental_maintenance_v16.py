"""Exact source updates and consented local schedules never transfer semantic approval."""

from datetime import timedelta

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from support import FILES
from test_taxonomy_governance_v16 import customize_analysis

from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput
from careerforge.contracts_governance import (
    MaintenanceInput,
    MaintenanceToggle,
    UpdateExecutionInput,
    UpdatePlanInput,
)
from careerforge.db import clock
from careerforge.errors import DomainError
from careerforge.models import (
    FundingAgreement,
    Principal,
    ProjectSelection,
    Publication,
    UnitVersion,
)
from careerforge.models_governance import MaintenanceOccurrence, MaintenanceSchedule
from careerforge.services.dependencies import invalidate
from careerforge.services.security import create_principal
from careerforge.util import digest


def import_next(env, *, files=None, commit="b" * 40, namespace=None):
    return env["app"].sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=namespace or env["ns"],
            repository_key="fixtures/agent",
            commit_sha=commit,
            license_id="MIT",
            files=files or {**FILES, "README.md": "# Updated fixture\n"},
            source_metadata={"synthetic": True},
        ),
    )


def create_plan(env, old, new, namespace=None):
    return env["app"].lifecycle.create_update_plan(
        env["actor"],
        UpdatePlanInput(namespace_id=namespace or env["ns"], old_source_id=old, new_source_id=new),
    )


def due(env, schedule_id):
    with env["app"].db.transaction() as db:
        row = db.get(MaintenanceSchedule, schedule_id)
        row.next_due_at = clock(db) - timedelta(seconds=1)


def schedule(env, source, *, models=False):
    return env["app"].lifecycle.create_maintenance(
        env["actor"],
        MaintenanceInput(
            namespace_id=env["ns"],
            source_id=source,
            interval_seconds=3600,
            allow_model_calls=models,
            funding_id=env["funding"] if models else None,
            call_cap=10,
        ),
    )


def enable(env, row):
    return env["app"].lifecycle.set_maintenance(
        env["actor"],
        row["schedule_id"],
        MaintenanceToggle(expected_revision=row["revision"], enabled=True),
    )


@pytest.mark.asyncio
async def test_exact_diff_partial_semantic_closure_requires_reinvestigation_without_mutating_history(
    env,
):
    old, publication, _ = await publish_fixture(env)
    files = {**FILES, "README.md": "# Changed description\n", "new.txt": "new content"}
    del files["LICENSE"]
    new = import_next(env, files=files)
    before_calls = len(env["model"].calls)
    with env["app"].db.read() as db:
        before = env["app"].knowledge.publication_pack(db, env["actor"], publication)
    plan = create_plan(env, old, new)
    manifest = plan["manifest"]
    changes = {change["path"]: change for change in manifest["changes"]}
    assert {path: change["kind"] for path, change in changes.items()} == {
        "LICENSE": "deleted",
        "README.md": "modified",
        "new.txt": "added",
    }
    assert changes["README.md"]["before_hash"] == digest(FILES["README.md"])
    assert changes["README.md"]["after_hash"] == digest(files["README.md"])
    assert "+# Changed description" in changes["README.md"]["unified_diff"]
    assert manifest["units"][0]["route"] == "C_reinvestigate"
    assert manifest["units"][0]["dependency_closure"] == "partial"
    assert manifest["units"][0]["automatic_semantic_carry_forward"] is False
    assert manifest["old_publications_revoked"] is False and manifest["new_head_adopted"] is False
    assert len(env["model"].calls) == before_calls
    with env["app"].db.read() as db:
        assert env["app"].knowledge.publication_pack(db, env["actor"], publication) == before
        assert db.scalar(select(ProjectSelection)).source_id == old


@pytest.mark.asyncio
async def test_structural_whitelist_carry_forward_has_new_binding_not_new_semantic_publication(env):
    # Template says only that a syntax node exists; not behavior, purity or runtime success.
    customize_analysis(
        env,
        "agent.tool_calling",
        facet_key=None,
        statement="结构声明：src/tools.py::echo [FunctionDef]",
    )
    old, publication, _ = await publish_fixture(env)
    new = import_next(env)
    calls = len(env["model"].calls)
    plan = create_plan(env, old, new)
    unit = plan["manifest"]["units"][0]
    assert unit["route"] == "A_structural_carry_forward"
    binding = unit["structural_bindings"][0]
    assert binding["new_source_id"] == new and binding["new_commit"] == "b" * 40
    assert binding["file_hash"] == digest(FILES["src/tools.py"])
    assert binding["closure_grade"] == "complete_for_structural_claim"
    assert binding["runtime_observed"] is False and binding["reviewer_reread"] is False
    assert binding["qualifies_as_publication"] is False
    assert unit["requires_new_publication_review"] is True
    with env["app"].db.read() as db:
        assert list(db.scalars(select(Publication.id))) == [publication]
    assert len(env["model"].calls) == calls
    # Even structural carry-forward is rejected if configuration/environment context changes.
    third = import_next(
        env, files={**FILES, "pyproject.toml": "[project]\nname='changed'\n"}, commit="c" * 40
    )
    assert create_plan(env, old, third)["manifest"]["units"][0]["route"] == "C_reinvestigate"


@pytest.mark.asyncio
async def test_execute_update_creates_fresh_independent_review_and_never_adopts_new_source_head(
    env,
):
    old, previous, _ = await publish_fixture(env)
    new = import_next(env)
    plan = create_plan(env, old, new)
    before = len(env["model"].calls)
    scheduled = env["app"].lifecycle.execute_update(
        env["actor"],
        plan["plan_id"],
        UpdateExecutionInput(expected_revision=1, funding_id=env["funding"], call_cap=10),
    )
    assert len(env["model"].calls) == before  # scheduling itself performs no inference
    with pytest.raises(DomainError) as exc:
        env["app"].lifecycle.execute_update(
            env["actor"],
            plan["plan_id"],
            UpdateExecutionInput(expected_revision=1, funding_id=env["funding"], call_cap=10),
        )
    assert exc.value.code == "UPDATE_CONFLICT"
    await env["app"].drain()
    result = env["app"].runtime.stage(scheduled["run_id"], "result")
    assert result and result["publication_ids"]
    new_pub = result["publication_ids"][0]
    assert new_pub != previous
    assert {call["role"] for call in env["model"].calls[before:]} >= {
        "analysis",
        "reviewer",
        "coverage",
    }
    with env["app"].db.read() as db:
        assert env["app"].knowledge.publication_pack(db, env["actor"], new_pub)["source_id"] == new
        assert env["app"].knowledge.publication_pack(db, env["actor"], previous)["source_id"] == old
        assert db.get(Publication, previous).review_id != db.get(Publication, new_pub).review_id
        assert db.scalar(select(ProjectSelection)).source_id == old


@pytest.mark.asyncio
async def test_plan_revalidates_source_and_projection_integrity_before_spending(env):
    old, publication, _ = await publish_fixture(env)
    new = import_next(env)
    plan = create_plan(env, old, new)
    before = len(env["model"].calls)
    with env["app"].db.transaction() as db:
        invalidate(db, env["actor"], new, "revoked", "maintenance source withdrawn")
    with pytest.raises(DomainError):
        env["app"].lifecycle.execute_update(
            env["actor"],
            plan["plan_id"],
            UpdateExecutionInput(expected_revision=1, funding_id=env["funding"]),
        )
    assert len(env["model"].calls) == before
    # A mutable projection cannot assert complete closure and qualify for shortcut reuse.
    with env["app"].db.transaction() as db:
        unit = db.get(UnitVersion, db.get(Publication, publication).unit_version_id)
        unit.dependency_manifest = {**unit.dependency_manifest, "closure": "complete"}
    with env["app"].db.read() as db, pytest.raises(DomainError) as exc:
        env["app"].knowledge.publication_pack(db, env["actor"], publication)
    assert exc.value.code == "UNIT_HASH"


@pytest.mark.asyncio
async def test_public_plan_never_exposes_private_consumer_identifiers(env):
    public = env["owner"]["public_namespace_id"]
    source, public_pub, _ = await publish_fixture(env, namespace=public)
    run = env["app"].build.start(
        env["actor"],
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            source_id=source,
            question="Private study of public source",
            topic_keys=["agent.tool_calling"],
            call_cap=10,
            max_units_per_project=1,
            idempotency_key="private-consumer",
        ),
    )
    await env["app"].drain()
    private_versions = set(env["app"].runtime.stage(run, "result")["unit_version_ids"])
    new = import_next(env, namespace=public)
    plan = create_plan(env, source, new, namespace=public)
    assert [unit["publication_id"] for unit in plan["manifest"]["units"]] == [public_pub]
    assert not private_versions & {unit["unit_version_id"] for unit in plan["manifest"]["units"]}


@pytest.mark.asyncio
async def test_maintenance_default_off_explicit_enable_local_only_and_no_old_snapshot_rollback(env):
    old, _, _ = await publish_fixture(env)
    new = import_next(env)
    row = schedule(env, old)
    due(env, row["schedule_id"])
    before = len(env["model"].calls)
    assert row["enabled"] is False and row["external_fetch"] is False
    assert env["app"].lifecycle.run_due_maintenance() == []
    assert (
        env["app"].lifecycle.tick_maintenance(env["actor"], row["schedule_id"])["status"]
        == "disabled"
    )
    enable(env, row)
    results = env["app"].lifecycle.run_due_maintenance()
    assert len(results) == 1 and results[0]["status"] == "planned"
    assert results[0]["model_calls"] == 0
    assert env["app"].lifecycle.run_due_maintenance() == []
    due(env, row["schedule_id"])
    repeated = env["app"].lifecycle.run_due_maintenance()
    assert repeated[0]["status"] == "unchanged"
    with env["app"].db.read() as db:
        assert db.get(MaintenanceSchedule, row["schedule_id"]).source_id == new
        assert db.scalar(select(ProjectSelection)).source_id == old
        assert len(list(db.scalars(select(MaintenanceOccurrence)))) == 2
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
async def test_maintenance_rechecks_principal_and_disables_blocked_schedule_without_models(env):
    old, _, _ = await publish_fixture(env)
    import_next(env)
    row = schedule(env, old)
    enable(env, row)
    due(env, row["schedule_id"])
    with env["app"].db.transaction() as db:
        db.get(Principal, env["actor"]).active = False
    calls = len(env["model"].calls)
    result = env["app"].lifecycle.run_due_maintenance()
    assert result[0]["status"] == "blocked" and result[0]["reason"] == "UNAUTHENTICATED"
    with env["app"].db.read() as db:
        assert db.get(MaintenanceSchedule, row["schedule_id"]).enabled is False
        assert db.scalar(select(MaintenanceOccurrence)).status == "blocked"
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
async def test_maintenance_funding_withdrawal_cannot_spend_under_prior_enable(env):
    old, _, _ = await publish_fixture(env)
    import_next(env)
    row = schedule(env, old, models=True)
    enable(env, row)
    due(env, row["schedule_id"])
    with env["app"].db.transaction() as db:
        db.get(FundingAgreement, env["funding"]).status = "revoked"
    before = len(env["model"].calls)
    results = env["app"].lifecycle.run_due_maintenance()
    assert results[0]["status"] == "blocked"
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
async def test_authorized_maintenance_schedules_real_existing_build_workflow(env):
    old, _, _ = await publish_fixture(env)
    import_next(env)
    row = schedule(env, old, models=True)
    enable(env, row)
    due(env, row["schedule_id"])
    results = env["app"].lifecycle.run_due_maintenance()
    assert len(results) == 1 and results[0]["new_review_required"] is True
    await env["app"].drain()
    assert env["app"].runtime.stage(results[0]["run_id"], "result")["publication_ids"]


@pytest.mark.asyncio
async def test_schedule_scope_and_toggle_revision_enforced(env):
    old, _, _ = await publish_fixture(env)
    row = schedule(env, old)
    enabled = enable(env, row)
    with pytest.raises(DomainError) as exc:
        enable(env, row)
    assert exc.value.code == "REVISION_CONFLICT"
    with env["app"].db.transaction() as db:
        other, _, _ = create_principal(db, "schedule-outsider")
        other_id = other.id
    with pytest.raises(DomainError):
        env["app"].lifecycle.set_maintenance(
            other_id,
            row["schedule_id"],
            MaintenanceToggle(expected_revision=enabled["revision"], enabled=False),
        )
    with pytest.raises(DomainError):
        env["app"].lifecycle.tick_maintenance(other_id, row["schedule_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["roles", "source", "data"])
async def test_periodic_work_never_widens_original_funding_authorization(env, restriction):
    old, _, _ = await publish_fixture(env)
    new = import_next(env)
    row = schedule(env, old, models=True)
    enable(env, row)
    due(env, row["schedule_id"])
    with env["app"].db.transaction() as db:
        funding = db.get(FundingAgreement, env["funding"])
        if restriction == "roles":
            funding.allowed_roles = ["analysis"]
        elif restriction == "source":
            funding.allowed_source_ids = [old]
        else:
            funding.allowed_data_classes = ["public_source"]
        old_funding = (
            list(funding.allowed_roles),
            list(funding.allowed_source_ids),
            list(funding.allowed_data_classes),
        )
    before = len(env["model"].calls)
    result = env["app"].lifecycle.run_due_maintenance()[0]
    assert result["status"] == "blocked"
    assert (
        result["reason"]
        == {
            "roles": "MAINTENANCE_ROLE_AUTHORIZATION",
            "source": "FUNDING_SOURCE_SCOPE",
            "data": "MAINTENANCE_DATA_AUTHORIZATION",
        }[restriction]
    )
    with env["app"].db.read() as db:
        funding = db.get(FundingAgreement, env["funding"])
        assert (
            funding.allowed_roles,
            funding.allowed_source_ids,
            funding.allowed_data_classes,
        ) == old_funding
        assert db.scalar(select(MaintenanceOccurrence)).reason.startswith(result["reason"])
        assert db.get(MaintenanceSchedule, row["schedule_id"]).source_id == old
    assert len(env["model"].calls) == before
    assert new != old


@pytest.mark.asyncio
async def test_bounded_local_review_cannot_substitute_an_easier_different_claim(env):
    old, _, _ = await publish_fixture(env)
    new = import_next(env)
    plan = create_plan(env, old, new)
    assert plan["manifest"]["units"][0]["route"] == "B_targeted_review"
    assert plan["manifest"]["units"][0]["old_review_id"]
    scheduled = env["app"].lifecycle.execute_update(
        env["actor"],
        plan["plan_id"],
        UpdateExecutionInput(expected_revision=1, funding_id=env["funding"]),
    )
    customize_analysis(
        env,
        "agent.tool_calling",
        facet_key=None,
        statement="This unrelated easier claim is not the requested recheck.",
    )
    await env["app"].drain()
    result = env["app"].runtime.stage(scheduled["run_id"], "result")
    assert not result or not result["publication_ids"]


@pytest.mark.asyncio
async def test_update_model_context_rechecks_old_evidence_after_queueing(env):
    old, publication, _ = await publish_fixture(env)
    new = import_next(env)
    plan = create_plan(env, old, new)
    scheduled = env["app"].lifecycle.execute_update(
        env["actor"],
        plan["plan_id"],
        UpdateExecutionInput(expected_revision=1, funding_id=env["funding"]),
    )
    with env["app"].db.transaction() as db:
        invalidate(db, env["actor"], publication, "revoked", "old observation permission withdrawn")
    before = len(env["model"].calls)
    await env["app"].drain()
    assert len(env["model"].calls) == before
    result = env["app"].runtime.stage(scheduled["run_id"], "result")
    assert not result or not result["publication_ids"]


@pytest.mark.asyncio
async def test_dynamic_dependency_or_changed_entrypoint_never_qualifies_for_b_local_review(env):
    old, _, _ = await publish_fixture(env)
    for index, code in enumerate(
        [
            "def echo(value):\n    return value.method()\n",
            "def echo(value, new_parameter):\n    return value\n",
            "import external_dependency\ndef echo(value):\n    return value\n",
        ]
    ):
        new = import_next(env, files={**FILES, "src/tools.py": code}, commit=str(index + 1) * 40)
        assert create_plan(env, old, new)["manifest"]["units"][0]["route"] == "C_reinvestigate"


def test_gap_scheduling_cannot_restore_erased_state_between_transactions(env, monkeypatch):
    import asyncio

    from careerforge.models import AgentRun, KnowledgeGap

    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        gap = KnowledgeGap(
            namespace_id=env["ns"],
            principal_id=actor,
            topic_keys=["agent.tool_calling"],
            source_ids=[],
            reason_codes=["coverage_gap"],
        )
        db.add(gap)
        db.flush()
        gap_id = gap.id
    original_start = app.build.start
    scheduled = []

    def erase_after_start(principal, body):
        run = original_start(principal, body)
        scheduled.append(run)
        proposal = app.privacy.propose(actor)
        result = asyncio.run(
            app.privacy.confirm(
                actor,
                proposal["proposal_id"],
                proposal["confirmation_nonce"],
                proposal["plan_hash"],
                True,
            )
        )
        assert result["state"] == "completed"
        return run

    monkeypatch.setattr(app.build, "start", erase_after_start)
    with pytest.raises(DomainError) as exc:
        app.lifecycle.schedule_gap(actor, gap_id, env["funding"], 13, "gap-erasure-race")
    assert exc.value.code == "PRIVACY_GENERATION_CHANGED"
    with app.db.read() as db:
        gap = db.get(KnowledgeGap, gap_id)
        assert gap.status != "scheduled" and gap.build_run_id is None
        assert gap.topic_keys == []
        assert db.get(AgentRun, scheduled[0]).status == "cancelled"
    assert env["model"].calls == []


def test_gap_scheduled_replay_is_blocked_during_personal_erasure(env):
    from careerforge.models import KnowledgeGap
    from careerforge.models_privacy_ext import PrivacyState

    with env["app"].db.transaction() as db:
        gap = KnowledgeGap(
            namespace_id=env["ns"],
            principal_id=env["actor"],
            status="scheduled",
            topic_keys=[],
            source_ids=[],
            reason_codes=[],
        )
        db.add(gap)
        db.flush()
        gap_id = gap.id
        db.get(PrivacyState, env["actor"]).blocked = True
    with pytest.raises(DomainError) as exc:
        env["app"].lifecycle.schedule_gap(
            env["actor"], gap_id, env["funding"], 10, "replay-erasure"
        )
    assert exc.value.code == "PRIVACY_ERASURE_PENDING"


def test_gap_low_budget_rejected_without_admission_or_funding(env):
    from careerforge.models import AgentRun, KnowledgeGap, ModelReceipt

    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        gap = KnowledgeGap(
            namespace_id=env["ns"],
            principal_id=actor,
            topic_keys=["agent.tool_calling"],
            source_ids=[],
            reason_codes=["coverage_gap"],
        )
        db.add(gap)
        db.flush()
        gap_id = gap.id
        funding = db.get(FundingAgreement, env["funding"])
        before_funding = (funding.calls_held, funding.calls_consumed)
    with pytest.raises(DomainError) as exc:
        app.lifecycle.schedule_gap(actor, gap_id, env["funding"], 10, "gap-low-budget")
    assert exc.value.code == "BUILD_BUDGET"
    with app.db.read() as db:
        gap = db.get(KnowledgeGap, gap_id)
        assert gap.status != "scheduled" and gap.build_run_id is None
        assert gap.topic_keys == ["agent.tool_calling"]
        assert not db.scalars(select(AgentRun)).all()
        assert not db.scalars(select(ModelReceipt)).all()
        funding = db.get(FundingAgreement, env["funding"])
        assert (funding.calls_held, funding.calls_consumed) == before_funding
    assert env["model"].calls == []
