"""Synthetic state fixtures exercise admission and gates, never native evidence."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from careerforge.api import create_app
from careerforge.contracts import ImportSourceInput
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    FundingAgreement,
    KnowledgeGap,
    ModelReceipt,
    StageArtifact,
    Turn,
)
from careerforge.services.gap_scheduling import GAP_SCHEDULE_CONTRACT
from careerforge.util import fingerprint


class NoCalls:
    synthetic = False
    supports_embeddings = False
    supports_reranking = False
    profile_version = "no-calls-admission-test"

    async def complete(self, request):
        raise AssertionError("Admission or reconciliation unexpectedly dispatched a model")


def gap_fixture(env, *, question="Which exact behavior is missing?", source=True):
    app, actor = env["app"], env["actor"]
    sid = (
        app.sources.import_source(
            actor,
            ImportSourceInput(
                namespace_id=env["ns"],
                repository_key="fixtures/gap",
                commit_sha="c" * 40,
                license_id="MIT",
                files={"module.py": "def sample():\n    return None\n"},
                source_metadata={"synthetic": True},
            ),
        )
        if source
        else None
    )
    conversation = app.conversations.create(actor)
    with app.db.transaction() as db:
        turn = Turn(
            conversation_id=conversation,
            question=question,
            focus_revision=1,
            focus={"project_source_ids": [sid] if sid else []},
            policy_versions={},
            status="completed",
            idempotency_key="synthetic-origin",
        )
        db.add(turn)
        db.flush()
        gap = KnowledgeGap(
            namespace_id=env["ns"],
            principal_id=actor,
            turn_id=turn.id,
            topic_keys=[],
            source_ids=[sid] if sid else [],
            reason_codes=["no_relevant_reviewed_evidence"],
        )
        db.add(gap)
        db.flush()
        return gap.id, turn.id, sid


def schedule(env, gap, source=None, **overrides):
    values = {
        "source_id": source,
        "max_units_per_project": 1,
        "duration_minutes": 240,
        "planning_call_seconds": 300,
        **overrides,
    }
    return env["app"].lifecycle.schedule_gap(
        env["actor"], gap, env["funding"], 15, "synthetic-gap-request", **values
    )


def no_calls(env):
    env["app"].provider = NoCalls()
    env["app"].gateway.provider = env["app"].provider


def test_exact_question_atomic_scope_budget_and_zero_dispatch(env):
    question = "  Original question, without topic substitution?\n"
    gap, turn, source = gap_fixture(env, question=question)
    no_calls(env)
    result = schedule(env, gap, source)
    with env["app"].db.read() as db:
        run = db.get(AgentRun, result["run_id"])
        assert run.input_manifest["question"] == question
        assert run.input_manifest["research_scope"] == "focused"
        assert (
            run.input_manifest["max_projects"] == run.input_manifest["max_units_per_project"] == 1
        )
        assert run.input_manifest["gap_schedule"]["origin"]["turn_id"] == turn
        assert run.input_manifest["gap_schedule"]["version"] == GAP_SCHEDULE_CONTRACT
        assert run.input_hash == fingerprint(run.input_manifest)
        assert run.call_cap == 15
        assert run.input_manifest["execution_plan"]["planned_call_ceiling"] == 15
        assert run.input_manifest["execution_plan"]["reviewer_rounds"] == 5
        assert run.input_manifest["execution_plan"]["search_call_reserve"] == 0
        assert db.get(KnowledgeGap, gap).build_run_id == run.id
        assert db.get(FundingAgreement, env["funding"]).calls_held == 15
        assert db.get(FundingAgreement, env["funding"]).calls_consumed == 0
        assert db.scalar(select(func.count()).select_from(ModelReceipt)) == 0
    assert not result["automatic_publication_approval"]


def test_replay_preserves_contract_and_rejects_changed_limits(env):
    gap, _, source = gap_fixture(env)
    no_calls(env)
    first = schedule(env, gap, source)
    second = schedule(env, gap, source)
    assert first["run_id"] == second["run_id"] and second["replayed"]
    with pytest.raises(DomainError, match="固定来源或预算"):
        schedule(env, gap, source, max_units_per_project=2)
    with env["app"].db.read() as db:
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 1
        assert db.get(FundingAgreement, env["funding"]).calls_held == 15
        assert db.get(FundingAgreement, env["funding"]).calls_consumed == 0


def test_legacy_queued_association_is_replayed_without_upgrade(env):
    gap, _, source = gap_fixture(env)
    no_calls(env)
    app = env["app"]
    run_id = app.runtime.create(
        env["actor"],
        env["ns"],
        env["funding"],
        "knowledge_build",
        {"source_id": source, "question": "legacy label"},
        8,
        "legacy-synthetic-request",
    )
    with app.db.transaction() as db:
        row = db.get(KnowledgeGap, gap)
        row.build_run_id, row.status = run_id, "scheduled"
        original = fingerprint(db.get(AgentRun, run_id).input_manifest)
    assert schedule(env, gap, source)["run_id"] == run_id
    assert app.lifecycle.reconcile_gap(env["actor"], gap)["reason_codes"] == [
        "legacy_build_contract_unchanged"
    ]
    with app.db.read() as db:
        assert fingerprint(db.get(AgentRun, run_id).input_manifest) == original
        assert db.get(AgentRun, run_id).status == "queued"
        assert db.scalar(select(func.count()).select_from(StageArtifact)) == 0


@pytest.mark.parametrize(
    "change,code",
    [
        ({"max_units_per_project": 4}, "GAP_UNIT_LIMIT"),
        ({"max_projects": 2}, "GAP_SOURCE_SCOPE"),
        ({"duration_minutes": 1}, "BUILD_TIME_BUDGET"),
        ({"source_id": "other-source"}, "GAP_SOURCE_SCOPE"),
    ],
)
def test_invalid_limits_fail_before_reservation(env, change, code):
    gap, _, source = gap_fixture(env)
    no_calls(env)
    values = {"source_id": source, **change}
    with pytest.raises(DomainError) as error:
        schedule(env, gap, **values)
    assert error.value.code == code
    with env["app"].db.read() as db:
        assert db.get(KnowledgeGap, gap).build_run_id is None
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 0


def test_unscoped_new_gap_needs_explicit_source_and_owner(env):
    gap, _, _ = gap_fixture(env, source=False)
    no_calls(env)
    with pytest.raises(DomainError) as error:
        schedule(env, gap)
    assert error.value.code == "GAP_SOURCE_SCOPE"
    from careerforge.services.security import create_principal

    with env["app"].db.transaction() as db:
        other, _, _ = create_principal(db, "stranger")
        other_id = other.id
    with pytest.raises(DomainError) as error:
        env["app"].lifecycle.schedule_gap(other_id, gap, env["funding"], 15, "denied")
    assert error.value.code == "NOT_FOUND"


def test_origin_must_be_completed_owned_and_unchanged(env):
    gap, turn, source = gap_fixture(env)
    no_calls(env)
    with env["app"].db.transaction() as db:
        db.get(Turn, turn).status = "running"
    with pytest.raises(DomainError) as error:
        schedule(env, gap, source)
    assert error.value.code == "GAP_ORIGIN"
    with env["app"].db.transaction() as db:
        db.get(Turn, turn).status = "completed"
    schedule(env, gap, source)
    with env["app"].db.transaction() as db:
        db.get(Turn, turn).question = "tampered fixture"
    with pytest.raises(DomainError) as error:
        env["app"].lifecycle.reconcile_gap(env["actor"], gap)
    assert error.value.code == "GAP_BUILD_BINDING"


def test_terminal_failure_is_unresolved_not_automatic_closure(env):
    gap, _, source = gap_fixture(env)
    no_calls(env)
    app = env["app"]
    started = schedule(env, gap, source)
    assert app.lifecycle.reconcile_gap(env["actor"], gap)["reason_codes"] == ["build_not_terminal"]
    epoch = app.runtime.claim(started["run_id"], "synthetic-worker")
    app.runtime.finish(started["run_id"], epoch, "synthetic-worker", "failed", "TEST_FAILURE")
    result = app.lifecycle.reconcile_gap(env["actor"], gap)
    assert result["status"] == "unresolved" and not result["resolved"]
    assert result["reason_codes"] == ["build_not_successful"]
    assert app.lifecycle.reconcile_gap(env["actor"], gap)["replayed"]
    with app.db.read() as db:
        assert db.get(KnowledgeGap, gap).build_run_id == started["run_id"]
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0
        assert db.get(FundingAgreement, env["funding"]).calls_consumed == 0
        assert db.get(AgentRun, started["run_id"]).budget_settled
        assert db.scalar(select(func.count()).select_from(ModelReceipt)) == 0


def test_api_requires_confirmation_and_exposes_exact_bounded_request(env):
    gap, _, source = gap_fixture(env)
    no_calls(env)
    client = TestClient(create_app(env["app"]))
    headers = {"Authorization": "Bearer " + env["owner"]["token"]}
    body = {
        "funding_id": env["funding"],
        "call_cap": 15,
        "idempotency_key": "api-gap",
        "source_id": source,
        "max_projects": 1,
        "max_units_per_project": 1,
        "duration_minutes": 240,
        "planning_call_seconds": 300,
    }
    path = f"/api/v1/knowledge/gaps/{gap}/schedule"
    assert client.post(path, json=body, headers=headers).status_code == 422
    response = client.post(path, json={**body, "confirm": "BUILD"}, headers=headers)
    assert response.status_code == 202, response.text
    assert (
        response.json()["gap_schedule"]["origin"]["question"] == "Which exact behavior is missing?"
    )
    result = client.post(
        f"/api/v1/knowledge/gaps/{gap}/reconcile", json={"confirm": "RECONCILE"}, headers=headers
    )
    assert result.status_code == 200 and not result.json()["resolved"]


def test_legacy_unknown_receipt_is_neither_reset_nor_reissued(env):
    gap, _, source = gap_fixture(env)
    no_calls(env)
    app = env["app"]
    run_id = app.runtime.create(
        env["actor"],
        env["ns"],
        env["funding"],
        "knowledge_build",
        {"source_id": source, "question": "legacy uncertain fixture"},
        8,
        "legacy-unknown",
    )
    with app.db.transaction() as db:
        row = db.get(KnowledgeGap, gap)
        row.build_run_id, row.status = run_id, "scheduled"
        receipt = ModelReceipt(
            run_id=run_id,
            stage_key="legacy.analysis",
            payer_id=env["actor"],
            epoch=1,
            role="analysis",
            request_hash="f" * 64,
            status="unknown",
            response=None,
            usage={},
            profile_version="legacy",
        )
        db.add(receipt)
        db.flush()
        receipt_id = receipt.id
    repeated = schedule(env, gap, source)
    assert repeated["replayed"] and repeated["run_id"] == run_id
    with app.db.read() as db:
        receipt = db.get(ModelReceipt, receipt_id)
        assert receipt.status == "unknown" and receipt.response is None
        assert receipt.request_hash == "f" * 64
        assert db.scalar(select(func.count()).select_from(ModelReceipt)) == 1
        assert "gap_schedule" not in db.get(AgentRun, run_id).input_manifest


def test_terminal_publicationless_success_and_partial_remain_unresolved(env):
    gap, _, source = gap_fixture(env)
    no_calls(env)
    app = env["app"]
    run_id = schedule(env, gap, source)["run_id"]
    epoch = app.runtime.claim(run_id, "synthetic-worker")
    app.runtime.save_stage(
        run_id,
        epoch,
        "synthetic-worker",
        "result",
        {"publication_ids": [], "gaps": ["not_observed"]},
    )
    app.runtime.finish(run_id, epoch, "synthetic-worker", "partially_succeeded")
    # A terminal partial build and a self-reported task result are insufficient.
    result = app.lifecycle.reconcile_gap(env["actor"], gap)
    assert not result["resolved"]
    assert "no_valid_new_publication" in result["reason_codes"]
    assert "original_question_coverage_incomplete" in result["reason_codes"]


@pytest.mark.parametrize(
    "state,code",
    [
        ("no_publication", "GAP_REANSWER_NOT_READY"),
        ("budget", "BUDGET_EXHAUSTED"),
        ("revoked", "FUNDING_UNAVAILABLE"),
        ("revision", "REVISION_CONFLICT"),
    ],
)
def test_auto_reanswer_outbox_fences_and_replays(env, monkeypatch, state, code):
    from careerforge.models import Conversation
    from careerforge.services import gap_scheduling

    gap, origin, source = gap_fixture(env)
    no_calls(env)
    app = env["app"]
    started = schedule(env, gap, source, auto_reanswer=True)
    run_id = started["run_id"]
    epoch = app.runtime.claim(run_id, "auto-worker")
    app.runtime.finish(run_id, epoch, "auto-worker", "succeeded")
    if state != "no_publication":
        # This fake pack only makes the dispatcher reach the real chat admission
        # checks. No fact is returned, published or charged by this test.
        monkeypatch.setattr(gap_scheduling, "_published", lambda *a: ({"unused": {}}, []))
        with app.db.transaction() as db:
            fund = db.get(FundingAgreement, env["funding"])
            if state == "budget":
                fund.calls_consumed = fund.call_cap
            elif state == "revoked":
                fund.status = "revoked"
            else:
                conv = db.get(Conversation, db.get(Turn, origin).conversation_id)
                conv.revision += 1
    app.process_projections()
    app.process_projections()
    assert app.runtime.stage(run_id, gap_scheduling.AUTO_GAP_STAGE) == {
        "status": "blocked",
        "reason_code": code,
    }
    with app.db.read() as db:
        assert db.scalar(select(func.count()).select_from(Turn)) == 1
        assert db.scalar(select(func.count()).select_from(ModelReceipt)) == 0
        assert db.get(KnowledgeGap, gap).status != "resolved"


def test_auto_reanswer_rejects_cross_repository_before_budget(env):
    gap, _, source = gap_fixture(env)
    _, _, other = gap_fixture(env)
    no_calls(env)
    with env["app"].db.transaction() as db:
        # The multi-repository scope itself must prevent automatic narrowing.
        db.get(KnowledgeGap, gap).source_ids = [source, other + "-other"]
    with pytest.raises(DomainError) as error:
        schedule(env, gap, source, auto_reanswer=True)
    assert error.value.code == "GAP_SOURCE_SCOPE"
    with env["app"].db.read() as db:
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0


def test_gap_review_retry_is_explicit_bounded_and_replayable(env):
    from careerforge.services.gap_scheduling import _validated_contract

    gap, _, source = gap_fixture(env)
    app, actor = env["app"], env["actor"]
    result = app.lifecycle.schedule_gap(
        actor,
        gap,
        env["funding"],
        28,
        "retry-gap",
        source_id=source,
        max_units_per_project=1,
        duration_minutes=90,
        planning_call_seconds=120,
        auto_evidence_retry=True,
    )
    with app.db.read() as db:
        run = db.get(AgentRun, result["run_id"])
        contract = _validated_contract(app.lifecycle, db, actor, db.get(KnowledgeGap, gap), run)
        assert contract["review_retry_contract"] == "gap-bounded-review-retry-v1"
        assert run.input_manifest["execution_plan"]["evidence_retry_call_reserve"] > 0
        assert run.input_manifest["targeted_review_contract"]
    assert (
        app.lifecycle.schedule_gap(
            actor,
            gap,
            env["funding"],
            28,
            "retry-gap",
            source_id=source,
            max_units_per_project=1,
            duration_minutes=90,
            planning_call_seconds=120,
            auto_evidence_retry=True,
        )["run_id"]
        == result["run_id"]
    )
