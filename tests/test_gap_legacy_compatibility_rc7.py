"""Legacy admission compatibility and new-contract fences; all inputs are synthetic."""

import asyncio
import copy

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from test_gap_schedule_v166 import gap_fixture, no_calls, schedule

from careerforge.api import create_app
from careerforge.contracts import KnowledgeBuildInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, FundingAgreement, KnowledgeGap, ModelReceipt, Turn
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.gap_scheduling import GAP_SCHEDULE_CONTRACT
from careerforge.util import fingerprint


def legacy_gap(env):
    no_calls(env)
    with env["app"].db.transaction() as db:
        gap = KnowledgeGap(
            namespace_id=env["ns"],
            principal_id=env["actor"],
            topic_keys=["agent.tool_calling"],
            source_ids=[],
            reason_codes=["coverage_gap"],
        )
        db.add(gap)
        db.flush()
        return gap.id


def legacy_schedule(env, gap):
    return env["app"].lifecycle.schedule_gap(env["actor"], gap, env["funding"], 13, "legacy-call")


def assert_unadmitted(env, gap):
    with env["app"].db.read() as db:
        assert db.get(KnowledgeGap, gap).build_run_id is None
        assert not list(db.scalars(select(AgentRun)))
        assert not list(db.scalars(select(ModelReceipt)))
        funding = db.get(FundingAgreement, env["funding"])
        assert (funding.calls_held, funding.calls_consumed) == (0, 0)
    assert env["model"].calls == []


def erase(env):
    app, actor = env["app"], env["actor"]
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


def test_legacy_schedule_keeps_budget_idempotency_and_unknown_receipt(env, monkeypatch):
    gap = legacy_gap(env)
    first = legacy_schedule(env, gap)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        run = db.get(AgentRun, first["run_id"])
        frozen = copy.deepcopy(run.input_manifest)
        original_hash = run.input_hash
        assert "gap_schedule" not in frozen
        assert frozen["question"] == "补充知识缺口：agent.tool_calling"
        assert "_admission_privacy_generation" not in frozen
        receipt = ModelReceipt(
            run_id=run.id,
            stage_key="synthetic.unknown",
            payer_id=actor,
            epoch=1,
            role="analysis",
            request_hash="e" * 64,
            status="unknown",
            response=None,
            usage={},
            profile_version="synthetic-test",
        )
        db.add(receipt)
        db.flush()
        receipt_id = receipt.id
    monkeypatch.setattr(
        app.build, "start", lambda *args: pytest.fail("Unknown receipt was retried")
    )
    again = legacy_schedule(env, gap)
    assert again["replayed"] and again["run_id"] == first["run_id"]
    with app.db.read() as db:
        run = db.get(AgentRun, first["run_id"])
        assert run.input_manifest == frozen and run.input_hash == original_hash
        assert db.get(ModelReceipt, receipt_id).status == "unknown"
        assert db.get(ModelReceipt, receipt_id).response is None
        assert len(list(db.scalars(select(AgentRun)))) == 1
        funding = db.get(FundingAgreement, env["funding"])
        assert (funding.calls_held, funding.calls_consumed) == (13, 0)
    assert env["model"].calls == []


def test_legacy_unused_funding_settles_without_a_second_admission(env):
    gap = legacy_gap(env)
    run_id = legacy_schedule(env, gap)["run_id"]
    app = env["app"]
    epoch = app.runtime.claim(run_id, "synthetic-worker")
    app.runtime.finish(run_id, epoch, "synthetic-worker", "failed", "SYNTHETIC_FAILURE")
    assert legacy_schedule(env, gap)["run_id"] == run_id
    with app.db.read() as db:
        funding = db.get(FundingAgreement, env["funding"])
        assert (funding.calls_held, funding.calls_consumed) == (0, 0)
        assert len(list(db.scalars(select(AgentRun)))) == 1
    assert app.lifecycle.reconcile_gap(env["actor"], gap)["reason_codes"] == [
        "legacy_build_contract_unchanged"
    ]


def test_legacy_erasure_before_start_cannot_recreate_snapshot_or_reserve_budget(env, monkeypatch):
    gap = legacy_gap(env)
    app = env["app"]
    original = app.build.start

    def erase_before_start(actor, body):
        assert body._admission_privacy_generation == 0
        erase(env)
        return original(actor, body)

    monkeypatch.setattr(app.build, "start", erase_before_start)
    with pytest.raises(DomainError) as failure:
        legacy_schedule(env, gap)
    assert failure.value.code == "PRIVACY_GENERATION_CHANGED"
    assert_unadmitted(env, gap)
    with app.db.read() as db:
        assert db.get(KnowledgeGap, gap).topic_keys == []


def test_server_generation_fence_never_enters_wire_schema_or_manifest(env):
    body = KnowledgeBuildInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        question="Synthetic request",
        idempotency_key="synthetic-generation",
    )
    wire = body.model_dump(mode="json")
    before_hash = fingerprint(wire)
    body._admission_privacy_generation = 7
    assert body.model_dump(mode="json") == wire
    assert fingerprint(body.model_dump(mode="json")) == before_hash
    assert "_admission_privacy_generation" not in KnowledgeBuildInput.model_fields
    assert (
        "_admission_privacy_generation" not in KnowledgeBuildInput.model_json_schema()["properties"]
    )
    with pytest.raises(ValidationError):
        KnowledgeBuildInput.model_validate({**wire, "_admission_privacy_generation": 7})


@pytest.mark.parametrize(
    "options",
    [
        {"contract_version": GAP_SCHEDULE_CONTRACT},
        {"source_id": None},
        {"max_units_per_project": 2},
    ],
)
def test_any_new_marker_or_option_forbids_no_turn_legacy_fallback(env, monkeypatch, options):
    gap = legacy_gap(env)
    app = env["app"]
    monkeypatch.setattr(
        app.build, "start", lambda *args: pytest.fail("New request used legacy start")
    )
    with pytest.raises(DomainError) as failure:
        app.lifecycle.schedule_gap(env["actor"], gap, env["funding"], 13, "new-request", **options)
    assert failure.value.code == "GAP_ORIGIN"
    assert_unadmitted(env, gap)


@pytest.mark.parametrize("marker", [None, "legacy", "exact-question-gap-schedule-future"])
def test_unknown_explicit_service_marker_is_not_an_opt_out(env, marker):
    gap = legacy_gap(env)
    with pytest.raises(DomainError) as failure:
        env["app"].lifecycle.schedule_gap(
            env["actor"], gap, env["funding"], 13, "unknown-version", contract_version=marker
        )
    assert failure.value.code == "GAP_CONTRACT_VERSION"
    assert_unadmitted(env, gap)


@pytest.mark.parametrize(
    "has_origin,marker,code",
    [
        (False, None, "GAP_ORIGIN"),
        (True, None, "GAP_SOURCE_SCOPE"),
        (False, "legacy", None),
        (True, "exact-question-gap-schedule-future", None),
    ],
)
def test_new_http_api_never_downgrades_missing_source_or_origin(env, has_origin, marker, code):
    gap = gap_fixture(env, source=False)[0] if has_origin else legacy_gap(env)
    no_calls(env)
    body = {
        "funding_id": env["funding"],
        "call_cap": 13,
        "idempotency_key": "new-api",
        "confirm": "BUILD",
    }
    if marker:
        body["contract_version"] = marker
    with TestClient(create_app(env["app"])) as client:
        response = client.post(
            "/api/v1/knowledge/gaps/" + gap + "/schedule",
            json=body,
            headers={"Authorization": "Bearer " + env["owner"]["token"]},
        )
    assert response.status_code in {409, 422}
    if code:
        assert response.json()["error"]["code"] == code
    else:
        assert response.status_code == 422
    assert_unadmitted(env, gap)


@pytest.mark.parametrize(
    "damage", ["missing_turn", "missing_origin", "future_marker", "null_marker", "lost_link"]
)
def test_stored_new_marker_never_downgrades_after_origin_or_link_damage(env, monkeypatch, damage):
    gap, _, source = gap_fixture(env)
    no_calls(env)
    app, actor = env["app"], env["actor"]
    run_id = schedule(env, gap, source)["run_id"]
    with app.db.transaction() as db:
        row = db.get(KnowledgeGap, gap)
        run = db.get(AgentRun, run_id)
        manifest = copy.deepcopy(run.input_manifest)
        if damage in {"missing_turn", "lost_link"}:
            row.turn_id = None
            if damage == "lost_link":
                row.build_run_id = None
        elif damage == "missing_origin":
            del manifest["gap_schedule"]["origin"]
        elif damage == "future_marker":
            manifest["gap_schedule"]["version"] = "exact-question-gap-schedule-future"
        else:
            manifest["gap_schedule"] = None
        run.input_manifest, run.input_hash = manifest, fingerprint(manifest)
        preserved_hash = run.input_hash
    monkeypatch.setattr(
        app.build, "start", lambda *args: pytest.fail("Damaged new contract downgraded")
    )
    with pytest.raises(DomainError) as failure:
        app.lifecycle.schedule_gap(actor, gap, env["funding"], 15, "synthetic-gap-request")
    assert failure.value.code in {"GAP_ORIGIN", "GAP_BUILD_BINDING"}
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).input_hash == preserved_hash
        assert len(list(db.scalars(select(AgentRun)))) == 1
        funding = db.get(FundingAgreement, env["funding"])
        assert (funding.calls_held, funding.calls_consumed) == (15, 0)
        assert not list(db.scalars(select(ModelReceipt)))


def test_new_contract_generation_change_rolls_back_admission_atomically(env, monkeypatch):
    gap, _, source = gap_fixture(env)
    no_calls(env)
    app, actor = env["app"], env["actor"]
    prepare = app.build.prepare_in

    def synthetic_generation_change(db, principal, body):
        manifest = prepare(db, principal, body)
        db.get(PrivacyState, actor).generation += 1
        return manifest

    monkeypatch.setattr(app.build, "prepare_in", synthetic_generation_change)
    with pytest.raises(DomainError) as failure:
        schedule(env, gap, source)
    assert failure.value.code == "PRIVACY_GENERATION_CHANGED"
    assert_unadmitted(env, gap)
    with app.db.read() as db:
        assert db.get(PrivacyState, actor).generation == 0
        assert db.get(Turn, db.get(KnowledgeGap, gap).turn_id).status == "completed"
