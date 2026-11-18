"""Synthetic domain stage/recovery tests; native graph integration is separate."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_model_assessment_v166 import setup_model, start, submitted

from careerforge.errors import DomainError
from careerforge.models import AgentRun, ModelReceipt
from careerforge.models_learning_ext import LearningObservationRecord
from careerforge.services.assessment import ASSESSMENT_STAGE
from careerforge.workflows.learning_assessment import STAGES, LearningAssessmentGraph


async def claim(env):
    _, attempt = await submitted(env)
    setup_model(env)
    identifier = start(env, attempt)
    app = env["app"]
    epoch = app.runtime.claim(identifier, "m5-domain-worker")
    assert epoch is not None
    with app.db.read() as db:
        run = db.get(AgentRun, identifier)
    return run, epoch


async def test_model_receipt_recovery_persists_one_candidate_no_extra_evaluation(env):
    app = env["app"]
    run, epoch = await claim(env)
    snapshot = app.assessments.prepare_candidate(run)
    assert snapshot["response"]
    await app.assessments.evaluate_candidate(run, epoch, "m5-domain-worker")
    assert app.runtime.stage(run.id, "result") is None
    # Crash boundary: model receipt is durable but no candidate/checkpoint exists.
    with app.db.read() as db:
        receipt = db.scalar(
            select(ModelReceipt).where(
                ModelReceipt.run_id == run.id, ModelReceipt.stage_key == ASSESSMENT_STAGE
            )
        )
        assert receipt.status == "completed"
    await app.assessments.evaluate_candidate(run, epoch, "m5-domain-worker")
    app.assessments.persist_candidate(run, epoch, "m5-domain-worker")
    result = app.runtime.stage(run.id, "result")
    assert result["evaluator_kind"] == "model_assessment_candidate"
    assert not result["competence_verified"] and not result["human_observation_created"]
    app.assessments.persist_candidate(run, epoch, "m5-domain-worker")
    await app.assessments.evaluate_candidate(run, epoch, "m5-domain-worker")
    assert app.runtime.stage(run.id, "result") == result
    assert len([c for c in env["model"].calls if c["role"] == "evaluation"]) == 1
    with app.db.read() as db:
        assert db.scalar(select(LearningObservationRecord)) is None


async def test_candidate_persistence_requires_real_completed_receipt(env):
    app = env["app"]
    run, epoch = await claim(env)
    with pytest.raises(DomainError) as failed:
        app.assessments.persist_candidate(run, epoch, "m5-domain-worker")
    assert failed.value.code == "ASSESSMENT_RECEIPT"
    assert app.runtime.stage(run.id, "result") is None


async def test_persistence_rechecks_receipt_role_after_model(env):
    app = env["app"]
    run, epoch = await claim(env)
    await app.assessments.evaluate_candidate(run, epoch, "m5-domain-worker")
    with app.db.transaction() as db:
        receipt = db.scalar(
            select(ModelReceipt).where(
                ModelReceipt.run_id == run.id, ModelReceipt.stage_key == ASSESSMENT_STAGE
            )
        )
        receipt.role = "tutor"
    with pytest.raises(DomainError) as failed:
        app.assessments.persist_candidate(run, epoch, "m5-domain-worker")
    assert failed.value.code == "ASSESSMENT_RECEIPT"
    assert app.runtime.stage(run.id, "result") is None


async def test_graph_has_actual_separate_stages_and_rejects_legacy_binding(env):
    app = env["app"]
    run, epoch = await claim(env)
    adapter = LearningAssessmentGraph(app, run, epoch, "m5-domain-worker")
    graph = adapter.graph(InMemorySaver())
    assert set(STAGES) <= set(graph.get_graph().nodes)
    assert "execute_domain" not in graph.get_graph().nodes
    with pytest.raises(DomainError) as failed:
        adapter.fence(
            {
                "workflow_run_id": run.id,
                "graph_version": "learning-assessment-v1",
                "state_schema_version": 1,
            }
        )
    assert failed.value.code == "GRAPH_STATE"


async def test_start_in_rollback_keeps_run_and_funding_atomic(env):
    from careerforge.contracts_assessment_v166 import AssessmentInput
    from careerforge.models import FundingAgreement

    app = env["app"]
    _, attempt = await submitted(env)
    value = AssessmentInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        attempt_id=attempt["attempt_id"],
        idempotency_key="atomic-assessment",
    )
    with app.db.read() as db:
        held = db.get(FundingAgreement, env["funding"]).calls_held
    with pytest.raises(RuntimeError), app.db.transaction() as db:
        app.assessments.start_in(db, env["actor"], value)
        raise RuntimeError("before assessment request link committed")
    with app.db.read() as db:
        assert db.scalar(select(AgentRun).where(AgentRun.workflow == "learning_assessment")) is None
        assert db.get(FundingAgreement, env["funding"]).calls_held == held
    identifier = app.assessments.start(env["actor"], value)["run_id"]
    with app.db.read() as db:
        run = db.get(AgentRun, identifier)
        assert run.call_cap == 1 and run.workflow_engine == "legacy"
        assert "learning_assessment_request" not in run.input_manifest


async def test_revocation_after_model_before_persistence_discards_candidate(env):
    from careerforge.services.dependencies import invalidate

    app = env["app"]
    run, epoch = await claim(env)
    await app.assessments.evaluate_candidate(run, epoch, "m5-domain-worker")
    with app.db.transaction() as db:
        invalidate(
            db, env["actor"], run.input_manifest["source_ids"][0], "revoked", "synthetic-test"
        )
    with pytest.raises(DomainError):
        app.assessments.persist_candidate(run, epoch, "m5-domain-worker")
    assert app.runtime.stage(run.id, "result") is None
    assert len([c for c in env["model"].calls if c["role"] == "evaluation"]) == 1
