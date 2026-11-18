"""Synthetic lifecycle controls, not native coverage or successful gap resolution."""

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_gap_schedule_v166 import gap_fixture, no_calls, schedule

from careerforge.contracts import ImportSourceInput, TurnInput
from careerforge.errors import DomainError
from careerforge.models import (
    AccessGrant,
    AgentRun,
    Conversation,
    KnowledgeGap,
    ModelReceipt,
    ObjectRow,
    StageArtifact,
    Turn,
)
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.gap_scheduling import GAP_RECONCILIATION_CONTRACT
from careerforge.services.security import create_principal
from careerforge.util import fingerprint


def publicationless_build(env, *, status="partially_succeeded"):
    gap, origin, source = gap_fixture(env)
    no_calls(env)
    app = env["app"]
    run_id = schedule(env, gap, source)["run_id"]
    epoch = app.runtime.claim(run_id, "synthetic-terminal-worker")
    app.runtime.save_stage(
        run_id,
        epoch,
        "synthetic-terminal-worker",
        "result",
        {"publication_ids": [], "gaps": ["runtime_not_observed"]},
    )
    app.runtime.finish(run_id, epoch, "synthetic-terminal-worker", status)
    return gap, origin, source, run_id


def answer_turn(env, source, key):
    app, actor = env["app"], env["actor"]
    conv = app.conversations.create(actor)
    with app.db.transaction() as db:
        db.get(Conversation, conv).focus = {"project_source_ids": [source]}
    return app.conversations.submit(
        actor,
        conv,
        TurnInput(
            question="Which exact behavior is missing?",
            expected_revision=1,
            idempotency_key=key,
        ),
    )["turn_id"]


def reconciliations(env, run_id):
    with env["app"].db.read() as db:
        return {
            row.stage_key: fingerprint(row.payload)
            for row in db.scalars(
                select(StageArtifact).where(
                    StageArtifact.run_id == run_id,
                    StageArtifact.stage_key.startswith("gap.reconciliation"),
                )
            )
        }


def legacy_core(payload):
    keys = {
        "contract_version",
        "gap_id",
        "run_id",
        "original_turn_id",
        "question_sha256",
        "answer_turn_id",
        "build_status",
        "status",
        "resolved",
        "publication_ids",
        "reason_codes",
        "historical_turn_rewritten",
        "additional_model_calls",
    }
    return {key: value for key, value in payload.items() if key in keys}


@pytest.mark.asyncio
async def test_pending_to_completed_recomputes_then_terminal_replay_is_idempotent(env):
    gap, _, source, run_id = publicationless_build(env)
    app, actor = env["app"], env["actor"]
    turn = answer_turn(env, source, "pending-then-completed")
    first = app.lifecycle.reconcile_gap(actor, gap, turn)
    assert first["reconciliation_status"] == "pending" and not first["final"]
    assert "answer_not_completed" in first["reason_codes"]
    assert reconciliations(env, run_id) == {}
    with app.db.read() as db:
        assert db.get(KnowledgeGap, gap).status == "scheduled"
    await app.conversations.process(turn)  # Actual no-funding service path, zero model dispatch.
    second = app.lifecycle.reconcile_gap(actor, gap, turn)
    assert second["final"] and not second.get("replayed", False)
    assert second["reconciliation_contract_version"] == GAP_RECONCILIATION_CONTRACT
    assert "answer_not_completed" not in second["reason_codes"]
    assert "no_valid_new_publication" in second["reason_codes"] and not second["resolved"]
    assert first["state_hash"] != second["state_hash"]
    recorded = reconciliations(env, run_id)
    third = app.lifecycle.reconcile_gap(actor, gap, turn)
    assert third["replayed"] and third["state_hash"] == second["state_hash"]
    assert reconciliations(env, run_id) == recorded and len(recorded) == 1
    assert len(env["model"].calls) == 0


@pytest.mark.parametrize("status", ["failed", "interrupted", "cancelled"])
def test_pending_to_unsuccessful_answer_is_a_new_terminal_observation(env, status):
    gap, _, source, run_id = publicationless_build(env)
    app, actor = env["app"], env["actor"]
    turn = answer_turn(env, source, "pending-then-" + status)
    first = app.lifecycle.reconcile_gap(actor, gap, turn)
    assert not first["final"] and not reconciliations(env, run_id)
    if status == "interrupted":
        app.conversations.mark_failed(turn, "SYNTHETIC_TEST_FAILURE")
    else:
        # Explicit synthetic terminal state: mark_failed normally interrupts a
        # turn, while reconciliation must also handle failed/cancelled history.
        with app.db.transaction() as db:
            db.get(Turn, turn).status = status
    second = app.lifecycle.reconcile_gap(actor, gap, turn)
    assert second["final"] and not second["resolved"]
    assert second["state_snapshot"]["answer"]["status"] == status
    assert first["state_hash"] != second["state_hash"]
    assert len(reconciliations(env, run_id)) == 1
    assert app.lifecycle.reconcile_gap(actor, gap, turn)["replayed"]


@pytest.mark.asyncio
async def test_different_answers_have_distinct_terminal_identity(env):
    gap, _, source, run_id = publicationless_build(env)
    app, actor = env["app"], env["actor"]
    first_turn = answer_turn(env, source, "first-answer")
    await app.conversations.process(first_turn)
    first = app.lifecycle.reconcile_gap(actor, gap, first_turn)
    second_turn = answer_turn(env, source, "second-answer")
    assert not app.lifecycle.reconcile_gap(actor, gap, second_turn)["final"]
    await app.conversations.process(second_turn)
    second = app.lifecycle.reconcile_gap(actor, gap, second_turn)
    assert first["state_hash"] != second["state_hash"]
    assert second["answer_turn_id"] == second_turn and not second.get("replayed", False)
    assert len(reconciliations(env, run_id)) == 2
    assert app.lifecycle.reconcile_gap(actor, gap, first_turn)["replayed"]


@pytest.mark.asyncio
async def test_legacy_pending_snapshot_is_retained_but_never_replayed_as_terminal(env):
    gap, _, source, run_id = publicationless_build(env)
    app, actor = env["app"], env["actor"]
    turn = answer_turn(env, source, "legacy-pending")
    observation = app.lifecycle.reconcile_gap(actor, gap, turn)
    old = legacy_core(observation)
    old["status"] = "unresolved"
    old["reason_codes"] = [r for r in old["reason_codes"] if r != "answer_not_terminal"]
    key = "gap.reconciliation:" + turn
    with app.db.transaction() as db:
        db.add(
            StageArtifact(
                run_id=run_id,
                stage_key=key,
                input_hash=db.get(AgentRun, run_id).input_hash,
                payload=old,
            )
        )
    assert not app.lifecycle.reconcile_gap(actor, gap, turn)["final"]
    await app.conversations.process(turn)
    current = app.lifecycle.reconcile_gap(actor, gap, turn)
    assert current["final"] and not current.get("replayed", False)
    assert "answer_not_completed" not in current["reason_codes"]
    rows = reconciliations(env, run_id)
    assert rows[key] == fingerprint(old) and len(rows) == 2


def test_legacy_determined_terminal_result_replays_only_after_fresh_validation(env):
    gap, _, _, run_id = publicationless_build(env, status="failed")
    app, actor = env["app"], env["actor"]
    current = app.lifecycle.reconcile_gap(actor, gap)
    old = legacy_core(current)
    with app.db.transaction() as db:
        # Construct an explicit historical synthetic fixture, never rewrite real evidence.
        row = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run_id,
                StageArtifact.stage_key.startswith("gap.reconciliation.v2:"),
            )
        )
        db.delete(row)
        db.add(
            StageArtifact(
                run_id=run_id,
                stage_key="gap.reconciliation:build_terminal",
                input_hash=db.get(AgentRun, run_id).input_hash,
                payload=old,
            )
        )
    before = reconciliations(env, run_id)
    replay = app.lifecycle.reconcile_gap(actor, gap)
    assert replay["replayed"] and replay["legacy_terminal_replay"]
    assert replay["final"] and not replay["resolved"]
    assert reconciliations(env, run_id) == before


@pytest.mark.parametrize("revoke", ["source", "privacy"])
def test_cached_terminal_result_revalidates_current_access(env, revoke):
    gap, _, source, run_id = publicationless_build(env)
    app, actor = env["app"], env["actor"]
    app.lifecycle.reconcile_gap(actor, gap)
    before = reconciliations(env, run_id)
    with app.db.transaction() as db:
        if revoke == "source":
            db.get(ObjectRow, source).state = "revoked"
            db.get(ObjectRow, source).revision += 1
        else:
            db.get(PrivacyState, actor).blocked = True
    with pytest.raises(DomainError) as error:
        app.lifecycle.reconcile_gap(actor, gap)
    assert error.value.code in {"EVIDENCE_UNAVAILABLE", "PRIVACY_ERASURE_PENDING"}
    assert reconciliations(env, run_id) == before


def test_cached_terminal_result_revalidates_revoked_source_grant(env):
    app, actor = env["app"], env["actor"]
    gap, _, _ = gap_fixture(env, source=False)
    with app.db.transaction() as db:
        owner, namespace, _ = create_principal(db, "synthetic-source-owner")
        grant = AccessGrant(namespace_id=namespace.id, principal_id=actor, role="reader")
        db.add(grant)
        db.flush()
        owner_id, namespace_id, grant_id = owner.id, namespace.id, grant.id
    source = app.sources.import_source(
        owner_id,
        ImportSourceInput(
            namespace_id=namespace_id,
            repository_key="fixtures/foreign-gap-source",
            commit_sha="d" * 40,
            license_id="MIT",
            files={"sample.py": "def sample():\n    return None\n"},
            source_metadata={"synthetic": True},
        ),
    )
    no_calls(env)
    run_id = schedule(env, gap, source)["run_id"]
    epoch = app.runtime.claim(run_id, "synthetic-terminal-worker")
    app.runtime.finish(run_id, epoch, "synthetic-terminal-worker", "failed")
    app.lifecycle.reconcile_gap(actor, gap)
    before = reconciliations(env, run_id)
    with app.db.transaction() as db:
        grant = db.get(AccessGrant, grant_id)
        grant.active = False
        grant.revision += 1
    with pytest.raises(DomainError) as error:
        app.lifecycle.reconcile_gap(actor, gap)
    assert error.value.code == "NOT_FOUND"
    assert reconciliations(env, run_id) == before
    assert len(env["model"].calls) == 0


def test_active_dependency_version_change_creates_a_new_terminal_identity(env):
    gap, _, source, run_id = publicationless_build(env)
    app, actor = env["app"], env["actor"]
    first = app.lifecycle.reconcile_gap(actor, gap)
    with app.db.transaction() as db:
        db.get(ObjectRow, source).revision += 1
    second = app.lifecycle.reconcile_gap(actor, gap)
    assert second["state_hash"] != first["state_hash"]
    assert (
        second["state_snapshot"]["dependency_closure"]
        != first["state_snapshot"]["dependency_closure"]
    )
    assert not second.get("replayed", False) and not second["resolved"]
    assert len(reconciliations(env, run_id)) == 2
    assert app.lifecycle.reconcile_gap(actor, gap)["replayed"]


def test_queued_build_observation_is_not_cached_before_terminal_status(env):
    gap, _, source = gap_fixture(env)
    no_calls(env)
    app, actor = env["app"], env["actor"]
    run_id = schedule(env, gap, source)["run_id"]
    first = app.lifecycle.reconcile_gap(actor, gap)
    assert not first["final"] and "build_not_terminal" in first["reason_codes"]
    assert not reconciliations(env, run_id)
    epoch = app.runtime.claim(run_id, "synthetic-terminal-worker")
    app.runtime.finish(run_id, epoch, "synthetic-terminal-worker", "failed")
    second = app.lifecycle.reconcile_gap(actor, gap)
    assert second["final"] and not second["resolved"]
    assert second["reason_codes"] == ["build_not_successful"]
    assert second["state_hash"] != first["state_hash"]
    assert len(reconciliations(env, run_id)) == 1


def test_cached_terminal_does_not_hide_changed_publication_references(env):
    gap, _, _, run_id = publicationless_build(env)
    app, actor = env["app"], env["actor"]
    app.lifecycle.reconcile_gap(actor, gap)
    before = reconciliations(env, run_id)
    with app.db.transaction() as db:
        row = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run_id, StageArtifact.stage_key == "result"
            )
        )
        row.payload = {**row.payload, "publication_ids": ["not-a-real-publication"]}
    with pytest.raises(DomainError):
        app.lifecycle.reconcile_gap(actor, gap)
    assert reconciliations(env, run_id) == before


@pytest.mark.asyncio
async def test_new_valid_but_unrelated_synthetic_publication_cannot_reuse_old_decision(env):
    _, publication, _ = await publish_fixture(env)
    gap, _, _, run_id = publicationless_build(env)
    app, actor = env["app"], env["actor"]
    first = app.lifecycle.reconcile_gap(actor, gap)
    with app.db.transaction() as db:
        row = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run_id, StageArtifact.stage_key == "result"
            )
        )
        row.payload = {**row.payload, "publication_ids": [publication]}
    changed = app.lifecycle.reconcile_gap(actor, gap)
    assert changed["state_hash"] != first["state_hash"] and not changed.get("replayed", False)
    assert "publication_not_fresh_independent_real_source" in changed["reason_codes"]
    assert not changed["resolved"] and len(reconciliations(env, run_id)) == 2
    assert app.lifecycle.reconcile_gap(actor, gap)["replayed"]


def test_unknown_receipt_is_not_retried_reset_or_cached_as_final(env):
    gap, _, _, run_id = publicationless_build(env, status="failed")
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        receipt = ModelReceipt(
            run_id=run_id,
            stage_key="synthetic.uncertain",
            payer_id=actor,
            epoch=1,
            role="analysis",
            request_hash="e" * 64,
            status="unknown",
            response=None,
            usage={},
            profile_version="fixture",
        )
        db.add(receipt)
        db.flush()
        receipt_id = receipt.id
    for _ in range(2):
        current = app.lifecycle.reconcile_gap(actor, gap)
        assert not current["final"] and not current["replayed"]
        assert "model_receipt_state_uncertain" in current["reason_codes"]
    assert not reconciliations(env, run_id)
    with app.db.read() as db:
        assert db.get(ModelReceipt, receipt_id).status == "unknown"
        assert db.get(ModelReceipt, receipt_id).response is None
    assert len(env["model"].calls) == 0


@pytest.mark.asyncio
async def test_actual_no_publication_flow_never_resolves_from_result_claim(env):
    gap, _, source, run_id = publicationless_build(env)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        row = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run_id, StageArtifact.stage_key == "result"
            )
        )
        row.payload = {**row.payload, "task_coverage": {"question": {"complete": True}}}
    turn = answer_turn(env, source, "publicationless-terminal")
    await app.conversations.process(turn)
    result = app.lifecycle.reconcile_gap(actor, gap, turn)
    assert result["final"] and not result["resolved"]
    assert "original_question_coverage_incomplete" in result["reason_codes"]
    assert "no_valid_new_publication" in result["reason_codes"]
    assert len(env["model"].calls) == 0
