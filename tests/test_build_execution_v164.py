"""Frozen duration planning and read-only checkpoints, using only explicit fixtures."""

from copy import deepcopy
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from support import FILES
from test_shared_builds import public_contract, subscriber

from careerforge.api import create_app
from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput, QueryPlan
from careerforge.db import aware, clock
from careerforge.errors import DomainError, LeaseLost
from careerforge.models import (
    AgentRun,
    FundingAgreement,
    Idempotency,
    ModelReceipt,
    Publication,
    SourceSet,
)
from careerforge.models_runtime_ext import SharedBuild, SharedBuildSlot
from careerforge.services.build_execution import (
    BUILD_EXECUTION_CONTRACT,
    plan_execution,
    validate_execution_manifest,
)
from careerforge.services.dependencies import invalidate
from careerforge.util import fingerprint


def body_for(env, **changes):
    source = env["app"].sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/duration",
            commit_sha="d" * 40,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )
    values = dict(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        source_id=source,
        question="Explain this implementation",
        max_units_per_project=1,
        call_cap=20,
        idempotency_key="duration-build",
    )
    values.update(changes)
    return KnowledgeBuildInput(**values)


@pytest.mark.parametrize(
    "name,value",
    [
        ("duration_minutes", 0),
        ("duration_minutes", -1),
        ("duration_minutes", 10081),
        ("duration_minutes", True),
        ("duration_minutes", "30"),
        ("duration_minutes", 1.5),
        ("planning_call_seconds", 0),
        ("planning_call_seconds", 3601),
        ("planning_call_seconds", True),
        ("planning_call_seconds", "120"),
    ],
)
def test_execution_parameters_are_strict_and_bounded(env, name, value):
    with pytest.raises(ValidationError):
        body_for(env, **{name: value})
    assert not env["model"].calls
    with env["app"].db.read() as db:
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0


def test_time_and_call_limits_jointly_plan_and_reserve_corrections(env):
    short = body_for(env, max_units_per_project=2, call_cap=100)
    low = plan_execution(short, False)
    longer = plan_execution(short.model_copy(update={"duration_minutes": 90}), False)
    assert low["analysis_rounds"] <= longer["analysis_rounds"]
    assert low["reviewer_rounds"] < longer["reviewer_rounds"]
    assert longer["analysis_calls_per_source"] == 6
    assert longer["review_calls_per_unit"] == 6
    assert low["quote_correction_calls_per_source"] == 3
    assert low["planned_call_ceiling"] <= low["call_capacity"] <= 100
    assert low["estimated_seconds"] <= 1800
    assert low["time_assumption"] == "configurable_per_call_estimate_not_a_guarantee"


def test_five_units_fail_before_model_or_reservation_and_public_parameters_can_adjust(env):
    body = body_for(env, max_units_per_project=5, call_cap=100, jd_text="熟悉 Tool Calling")
    with pytest.raises(DomainError) as error:
        env["app"].build.start(env["actor"], body)
    assert error.value.code == "BUILD_TIME_BUDGET"
    assert "duration_minutes" in error.value.message and "51" in error.value.message
    assert not env["model"].calls
    with env["app"].db.read() as db:
        assert not db.scalars(select(AgentRun)).all()
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0
    identifier = env["app"].build.start(
        env["actor"], body.model_copy(update={"duration_minutes": 90})
    )
    with env["app"].db.read() as db:
        row = db.get(AgentRun, identifier)
        assert row.input_manifest["execution_plan"]["planned_call_ceiling"] == 38
        assert abs((aware(row.deadline_at) - aware(row.created_at)).total_seconds() - 5400) < 2


def test_funding_clamps_deadline_but_cannot_invent_time(env):
    app = env["app"]
    body = body_for(env, duration_minutes=90)
    with app.db.transaction() as db:
        expiry = clock(db) + timedelta(minutes=40)
        db.get(FundingAgreement, env["funding"]).expires_at = expiry
    identifier = app.build.start(env["actor"], body)
    with app.db.read() as db:
        row = db.get(AgentRun, identifier)
        assert aware(row.deadline_at) == aware(expiry)
    app.runtime.cancel(env["actor"], identifier, 1)
    with app.db.transaction() as db:
        db.get(FundingAgreement, env["funding"]).expires_at = clock(db) + timedelta(minutes=2)
    with pytest.raises(DomainError) as error:
        app.build.start(
            env["actor"], body.model_copy(update={"idempotency_key": "too-short-funding"})
        )
    assert error.value.code == "BUILD_TIME_BUDGET"
    with app.db.read() as db:
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0


async def test_new_run_and_children_share_deadline_correction_contract_and_replay(env):
    app = env["app"]
    body = body_for(env, duration_minutes=90)
    run_id = app.build.start(env["actor"], body)
    with app.db.read() as db:
        before = aware(db.get(AgentRun, run_id).deadline_at)
    await app.drain()
    calls = len(env["model"].calls)
    assert app.build.start(env["actor"], body) == run_id
    await app.drain()
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
        assert aware(run.deadline_at) == before
        children = db.scalars(select(AgentRun).where(AgentRun.parent_id == run_id)).all()
        assert len(children) == 2
        assert all(aware(child.deadline_at) == before for child in children)
        for child in children:
            assert child.input_manifest["execution_contract_version"] == BUILD_EXECUTION_CONTRACT
            assert child.input_manifest["quote_correction_contract"]["max_attempts"] == 1
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0
    with pytest.raises(DomainError) as error:
        app.build.start(env["actor"], body.model_copy(update={"duration_minutes": 91}))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


async def test_legacy_key_replays_original_contract_receipts_and_deadline(env):
    app = env["app"]
    original = body_for(env, call_cap=6)
    with app.db.transaction() as db:
        manifest = app.build.prepare_in(
            db, env["actor"], original.model_copy(update={"call_cap": 20})
        )
        for key in [
            "duration_minutes",
            "planning_call_seconds",
            "execution_contract_version",
            "execution_plan",
            "quote_correction_contract",
        ]:
            manifest.pop(key)
        manifest["call_cap"] = 6
        manifest["investigation_contracts"]["analysis"]["max_rounds"] = 1
        manifest["investigation_contracts"]["reviewer"]["max_rounds"] = 1
        row = app.runtime.create_in(
            db,
            env["actor"],
            env["ns"],
            env["funding"],
            "knowledge_build",
            manifest,
            6,
            original.idempotency_key,
        )
        run_id, before, frozen = row.id, aware(row.deadline_at), deepcopy(row.input_manifest)
    assert app.build.start(env["actor"], original) == run_id
    await app.drain()
    with app.db.read() as db:
        receipts = [(r.id, r.request_hash) for r in db.scalars(select(ModelReceipt))]
        assert receipts
    assert app.build.start(env["actor"], original) == run_id
    await app.drain()
    with app.db.read() as db:
        row = db.get(AgentRun, run_id)
        assert aware(row.deadline_at) == before and row.input_manifest == frozen
        assert [(r.id, r.request_hash) for r in db.scalars(select(ModelReceipt))] == receipts
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0
    with pytest.raises(DomainError) as error:
        app.build.start(env["actor"], original.model_copy(update={"duration_minutes": 30}))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


async def test_low_remaining_time_blocks_only_new_dispatch_without_billing(env):
    app = env["app"]
    run_id = app.build.start(env["actor"], body_for(env))
    epoch = app.runtime.claim(run_id, "worker")
    with app.db.transaction() as db:
        db.get(AgentRun, run_id).deadline_at = clock(db) + timedelta(seconds=100)
    with pytest.raises(DomainError) as error:
        await app.gateway.call(
            run_id, epoch, "worker", "new-call", "planner", "plan", {}, QueryPlan
        )
    assert error.value.code == "BUILD_TIME_BUDGET"
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).calls_consumed == 0
        assert not db.scalars(select(ModelReceipt)).all()
    assert not env["model"].calls
    app.runtime.cancel(env["actor"], run_id, 2)
    with pytest.raises(LeaseLost):
        await app.gateway.call(
            run_id, epoch, "worker", "new-call", "planner", "plan", {}, QueryPlan
        )


async def test_checkpoint_exposes_saved_reviews_not_publications_and_rechecks_access(
    env, monkeypatch
):
    app = env["app"]
    body = body_for(env)
    run_id = app.build.start(env["actor"], body)
    original = app.knowledge.save_review

    def save_then_interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise DomainError("FIXTURE_AFTER_REVIEW", "fixture interruption")

    monkeypatch.setattr(app.knowledge, "save_review", save_then_interrupt)
    await app.drain()
    api = create_app(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        response = await client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["checkpoint"]["reviews_committed"] == 1
        assert payload["checkpoint"]["accepted_unpublished_reviews"] == 1
        assert payload["checkpoint"]["publications_committed"] == 0
        assert payload["checkpoint"]["task_completion_claimed"] is False
        review = next(
            child for child in payload["checkpoint"]["children"] if child["workflow"] == "review"
        )
        assert review["status"] == "failed" and review["terminal_code"] == "FIXTURE_AFTER_REVIEW"
        assert payload["deadline_at"] and payload["execution_plan"]
        assert payload["result"]["publication_ids"] == []
        other = subscriber(env, "outsider")
        denied = await client.get(
            f"/api/v1/runs/{run_id}", headers={"Authorization": "Bearer " + other["token"]}
        )
        assert denied.status_code == 404
        with app.db.transaction() as db:
            invalidate(db, env["actor"], body.source_id, "revoked", "fixture revocation")
        denied = await client.get(f"/api/v1/runs/{run_id}")
        assert denied.status_code in {403, 409}
    with app.db.read() as db:
        assert not db.scalars(select(Publication)).all()


@pytest.mark.parametrize(
    "case", ["none", "valid", "text", "long", "foreign_actor", "changed_source"]
)
async def test_checkpoint_child_codes_are_bounded_and_current_access_checked(env, case):
    app = env["app"]
    body = body_for(env)
    run_id = app.build.start(env["actor"], body)
    epoch = app.runtime.claim(run_id, "checkpoint-root")
    with app.db.read() as db:
        root = db.get(AgentRun, run_id)
    assert await app.build.root(root, epoch, "checkpoint-root") is False
    child_id = app.runtime.stage(run_id, "analysis_child")["run_id"]
    outsider = subscriber(env, "checkpoint-outsider")
    with app.db.read() as db, pytest.raises(DomainError) as inaccessible:
        app.build.checkpoint_in(db, outsider["actor"], db.get(AgentRun, run_id))
    assert inaccessible.value.status == 404
    if case in {"valid", "text", "long"}:
        child_epoch = app.runtime.claim(child_id, "checkpoint-child")
        code = {"valid": "MODEL_SCHEMA", "text": "Private JD error text", "long": "X" * 81}[case]
        assert app.runtime.finish(child_id, child_epoch, "checkpoint-child", "failed", code)
    elif case == "foreign_actor":
        with app.db.transaction() as db:
            db.get(AgentRun, child_id).principal_id = outsider["actor"]
    elif case == "changed_source":
        with app.db.transaction() as db:
            source = db.get(SourceSet, body.source_id)
            source.files = {**source.files, "src/tools.py": "UNREAD_CONTENT"}
    with app.db.read() as db:
        if case in {"foreign_actor", "changed_source"}:
            with pytest.raises(DomainError):
                app.build.checkpoint_in(db, env["actor"], db.get(AgentRun, run_id))
        else:
            checkpoint = app.build.checkpoint_in(db, env["actor"], db.get(AgentRun, run_id))
            assert checkpoint["children"] == [
                {
                    "run_id": child_id,
                    "workflow": "analysis",
                    "status": "queued" if case == "none" else "failed",
                    "terminal_code": "MODEL_SCHEMA" if case == "valid" else None,
                }
            ]
            assert not checkpoint["task_completion_claimed"]
    assert not env["model"].calls


def test_shared_singleflight_includes_execution_identity(env):
    app = env["app"]
    body = public_contract(env)
    first = app.shared_builds.create(env["actor"], body)
    same = app.shared_builds.create(
        env["actor"], body.model_copy(update={"idempotency_key": "same-scope"})
    )
    assert same["run_id"] == first["run_id"]
    longer = app.shared_builds.create(
        env["actor"], body.model_copy(update={"duration_minutes": 90, "idempotency_key": "longer"})
    )
    assert longer["run_id"] != first["run_id"]
    with pytest.raises(DomainError) as error:
        app.shared_builds.create(env["actor"], body.model_copy(update={"duration_minutes": 90}))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_legacy_shared_key_replays_without_extending_or_new_hold(env):
    app = env["app"]
    body = public_contract(env)
    run_id = app.shared_builds.create(env["actor"], body)["run_id"]
    # Reconstruct the stored 1.6.3 shape in this temporary fixture database,
    # never in historical run/evidence databases.
    with app.db.transaction() as db:
        run = db.get(AgentRun, run_id)
        manifest = deepcopy(run.input_manifest)
        for key in (
            "duration_minutes",
            "planning_call_seconds",
            "execution_contract_version",
            "execution_plan",
            "quote_correction_contract",
        ):
            manifest.pop(key)
        manifest["investigation_contracts"]["analysis"]["max_rounds"] = 3
        manifest["investigation_contracts"]["reviewer"]["max_rounds"] = 3
        run.input_manifest, run.input_hash = manifest, fingerprint(manifest)
        shared = db.get(SharedBuild, run_id)
        semantic = {
            key: value
            for key, value in manifest.items()
            if key not in {"funding_id", "idempotency_key"}
        }
        semantic["no_subscribers_policy"] = body.no_subscribers_policy
        shared.contract_hash, shared.scope_hash = run.input_hash, fingerprint(semantic)
        slot = db.scalar(select(SharedBuildSlot).where(SharedBuildSlot.current_run_id == run_id))
        slot.scope_hash = shared.scope_hash
        for row in db.scalars(select(Idempotency).where(Idempotency.principal_id == env["actor"])):
            if row.operation == "run.create" and row.result.get("run_id") == run_id:
                row.request_hash = run.input_hash
            if row.operation == "shared_build.create" and row.key == body.idempotency_key:
                row.request_hash = fingerprint(
                    body.model_dump(
                        mode="json", exclude={"duration_minutes", "planning_call_seconds"}
                    )
                )
        before = (
            run.deadline_at,
            deepcopy(manifest),
            db.get(FundingAgreement, body.funding_id).calls_held,
        )
    assert app.shared_builds.create(env["actor"], body)["run_id"] == run_id
    with pytest.raises(DomainError) as error:
        app.shared_builds.create(env["actor"], body.model_copy(update={"duration_minutes": 30}))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
        assert (
            run.deadline_at,
            run.input_manifest,
            db.get(FundingAgreement, body.funding_id).calls_held,
        ) == before
        assert len(db.scalars(select(AgentRun)).all()) == 1
    fresh = app.shared_builds.create(
        env["actor"], body.model_copy(update={"idempotency_key": "new-execution-contract"})
    )
    assert fresh["run_id"] != run_id
    assert not env["model"].calls


def test_missing_frozen_root_field_fails_closed_instead_of_attribute_error(env):
    body = body_for(env)
    with env["app"].db.transaction() as db:
        manifest = env["app"].build.prepare_in(db, env["actor"], body)
    manifest.pop("source_id")
    with pytest.raises(DomainError) as error:
        validate_execution_manifest(manifest, body.call_cap)
    assert error.value.code == "BUILD_EXECUTION_CONTRACT"


@pytest.mark.parametrize("was_available", [False, True])
async def test_embedding_capability_changes_do_not_expand_frozen_plan(
    env, monkeypatch, was_available
):
    app = env["app"]
    monkeypatch.setattr(app.retrieval, "_available", lambda: (was_available, False))
    run_id = app.build.start(env["actor"], body_for(env))
    with app.db.read() as db:
        frozen = deepcopy(db.get(AgentRun, run_id).input_manifest)
        assert frozen["execution_plan"]["embedding_calls_per_source"] == int(was_available)
    monkeypatch.setattr(app.retrieval, "_available", lambda: (not was_available, False))

    async def unexpected_index(*args, **kwargs):
        pytest.fail("A changed capability must not add an unplanned or unavailable embedding call")

    monkeypatch.setattr(app.retrieval, "index_vectors", unexpected_index)
    await app.drain()
    result = app.runtime.stage(run_id, "result")
    assert result["publication_ids"]
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
        assert run.input_manifest == frozen
        assert run.calls_consumed <= frozen["execution_plan"]["planned_call_ceiling"]
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0


def test_cli_forwards_explicit_parameters(monkeypatch):
    from careerforge.cli import main

    sent = []
    monkeypatch.setattr(main, "config", lambda: {"namespace_id": "n", "funding_id": "f"})
    monkeypatch.setattr(main, "request", lambda *args: sent.append(args) or {})
    monkeypatch.setattr(main, "show", lambda value: None)
    main.build(
        "question",
        calls=40,
        max_units_per_project=5,
        duration_minutes=90,
        planning_call_seconds=150,
        yes=True,
    )
    payload = sent[0][2]
    assert payload["duration_minutes"] == 90 and payload["planning_call_seconds"] == 150
    assert payload["max_units_per_project"] == 5 and payload["call_cap"] == 40


async def test_completed_receipt_replays_with_small_time_remaining_without_new_charge(env):
    app = env["app"]
    run_id = app.build.start(env["actor"], body_for(env))
    epoch = app.runtime.claim(run_id, "worker")
    args = (run_id, epoch, "worker", "plan", "planner", "plan", {}, QueryPlan)
    first = await app.gateway.call(*args)
    with app.db.transaction() as db:
        db.get(AgentRun, run_id).deadline_at = clock(db) + timedelta(seconds=100)
    second = await app.gateway.call(*args)
    assert first == second and len(env["model"].calls) == 1
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).calls_consumed == 1


@pytest.mark.parametrize("restriction", ["cancel", "funding", "deadline"])
async def test_late_reply_never_authorizes_business_writes_or_restart(
    env, monkeypatch, restriction
):
    app = env["app"]
    body = body_for(env)
    run_id = app.build.start(env["actor"], body)
    epoch = app.runtime.claim(run_id, "worker")
    complete = env["model"].complete

    async def late(request):
        result = await complete(request)
        if restriction == "cancel":
            app.runtime.cancel(env["actor"], run_id, 2)
        elif restriction == "funding":
            app.runtime.revoke_funding(env["actor"], env["funding"])
        else:
            with app.db.transaction() as db:
                db.get(AgentRun, run_id).deadline_at = clock(db) - timedelta(seconds=1)
        return result

    monkeypatch.setattr(env["model"], "complete", late)
    with pytest.raises(DomainError):
        await app.gateway.call(run_id, epoch, "worker", "late", "planner", "plan", {}, QueryPlan)
    app.runtime.reap()
    with pytest.raises(LeaseLost):
        app.runtime.save_stage(run_id, epoch, "worker", "illegal-late-result", {})
    assert not app.runtime.finish(run_id, epoch, "worker", "succeeded")
    assert app.build.start(env["actor"], body) == run_id
    with app.db.read() as db:
        row = db.get(AgentRun, run_id)
        assert row.status in {"failed", "cancelled"} and row.calls_consumed == 1
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0
        assert len(db.scalars(select(ModelReceipt)).all()) == 1
    assert len(env["model"].calls) == 1


def test_worker_recovery_does_not_extend_new_frozen_deadline(env):
    app = env["app"]
    body = body_for(env)
    run_id = app.build.start(env["actor"], body)
    first_epoch = app.runtime.claim(run_id, "old")
    with app.db.transaction() as db:
        row = db.get(AgentRun, run_id)
        deadline = row.deadline_at
        row.lease_until = clock(db) - timedelta(seconds=1)
    app.runtime.reap()
    second_epoch = app.runtime.claim(run_id, "new")
    assert second_epoch > first_epoch
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).deadline_at == deadline
    with pytest.raises(LeaseLost):
        app.runtime.save_stage(run_id, first_epoch, "old", "stale", {})
