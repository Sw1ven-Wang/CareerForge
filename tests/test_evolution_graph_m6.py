"""Synthetic engineering graph tests. Never native/holdout quality acceptance."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from test_evolution import candidate
from test_evolution_replay_v16 import replay_data

from careerforge.contracts_evolution_graph import EvolutionSessionInput
from careerforge.models import (
    AgentRun,
    Conversation,
    EvolutionEvaluation,
    ModelReceipt,
    StageArtifact,
)
from careerforge.services.evolution_stages import replay_step_limit


async def candidate_session(env, *, funding=True):
    app, actor = env["app"], env["actor"]
    app.settings = replace(app.settings, evolution_graph_enabled=True)
    app.db.settings = app.settings
    _, _, conv, _, feedback = await candidate(env)
    value = EvolutionSessionInput(
        contract="evolution-dialogue-v1",
        idempotency_key="candidate-session",
        intent={
            "type": "candidate",
            "target": "teaching_skill",
            "experience_ids": [feedback["experience_id"]],
            "funding_id": env["funding"] if funding else None,
        },
    )
    receipt = app.evolution_workflows.submit(actor, conv, value)
    return conv, feedback, value, receipt


@pytest.mark.asyncio
async def test_candidate_stages_and_explicit_wait_without_focus_or_release(env, monkeypatch):
    app = env["app"]
    conv, feedback, value, receipt = await candidate_session(env)

    async def forbidden(*args, **kwargs):
        raise AssertionError("The graph must not wrap legacy execute")

    monkeypatch.setattr(app.evolution, "execute", forbidden)
    with app.db.read() as db:
        focus = db.get(Conversation, conv).focus
    assert app.evolution_workflows.submit(env["actor"], conv, value) == receipt
    await app.drain()
    detail = app.evolution_workflows.detail(env["actor"], conv, receipt["workflow_id"])
    assert detail["status"] == "waiting" and detail["wait_kind"] == "replay_authorization"
    with app.db.read() as db:
        run = db.get(AgentRun, detail["run_id"])
        assert run.status == "succeeded" and run.graph_version == "evolution-candidate-v1"
        assert run.calls_consumed == 1 and run.parent_id is None
        assert db.get(Conversation, conv).focus == focus
        assert len(list(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id)))) == 1
    async with app.graph_runtime.checkpointers.open() as saver:
        saved = await saver.aget_tuple({"configurable": {"thread_id": run.id}})
        state = saved.checkpoint["channel_values"]
        assert set(state) <= {
            "workflow_run_id",
            "graph_version",
            "state_schema_version",
            "current_stage",
            "result_stage_key",
            "__start__",
        }
        assert feedback["experience_id"] not in str(state)


@pytest.mark.asyncio
async def test_candidate_without_funding_waits_zero_run_and_explicit_authorization(env):
    app = env["app"]
    conv, _, _, receipt = await candidate_session(env, funding=False)
    await app.drain()
    detail = app.evolution_workflows.detail(env["actor"], conv, receipt["workflow_id"])
    assert detail["wait_kind"] == "authorization" and detail["run_id"] is None
    value = EvolutionSessionInput(
        contract="evolution-dialogue-v1",
        idempotency_key="authorize",
        workflow_id=detail["workflow_id"],
        expected_workflow_revision=detail["workflow_revision"],
        intent={
            "type": "authorize_candidate",
            "request_id": detail["request_id"],
            "request_hash": detail["request_hash"],
            "funding_id": env["funding"],
        },
    )
    app.evolution_workflows.submit(env["actor"], conv, value)
    await app.drain()
    assert (
        app.evolution_workflows.detail(env["actor"], conv, receipt["workflow_id"])["wait_kind"]
        == "replay_authorization"
    )


async def replay_session(env, cases=4, cap=2):
    app = env["app"]
    conv, _, _, admission = await candidate_session(env)
    await app.drain()
    detail = app.evolution_workflows.detail(env["actor"], conv, admission["workflow_id"])
    _, value = await replay_data(env)
    # Explicit public synthetic fixture expansion, not an independent test set.
    value = value.model_copy(
        update={
            "name": "m6-fixture-" + str(cases),
            "cases": [
                value.cases[i % len(value.cases)].model_copy(
                    update={
                        "case_id": f"m6-synthetic-{i}",
                        "question": f"合成工程样例 {i}：固定返回路径",
                    }
                )
                for i in range(cases)
            ],
        }
    )
    ds = app.evolution.register_replay_dataset(env["actor"], value)
    command = EvolutionSessionInput(
        contract="evolution-dialogue-v1",
        idempotency_key="replay-session",
        workflow_id=detail["workflow_id"],
        expected_workflow_revision=detail["workflow_revision"],
        intent={
            "type": "replay",
            "candidate_id": detail["result"]["candidate_id"],
            "replay": {
                "dataset_id": ds["dataset_id"],
                "funding_id": env["funding"],
                "per_arm_call_cap": cap,
                "idempotency_key": "frozen-caller-replay",
            },
        },
    )
    receipt = app.evolution_workflows.submit(env["actor"], conv, command)
    return conv, receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("cases,cap", [(12, 2), (50, 1)])
async def test_real_replay_topology_exceeds_64_and_maximum_admission(env, monkeypatch, cases, cap):
    app = env["app"]
    conv, receipt = await replay_session(env, cases=cases, cap=cap)

    async def forbidden(*args, **kwargs):
        raise AssertionError("No wrapping legacy replay orchestration")

    monkeypatch.setattr(app.evolution, "execute_replay", forbidden)
    await app.drain(limit=120)
    detail = app.evolution_workflows.detail(env["actor"], conv, receipt["workflow_id"])
    with app.db.read() as db:
        run = db.get(AgentRun, detail["run_id"])
        assert run.status == "succeeded", (run.status, detail)
        assert run.workflow == "evolution_replay" and run.parent_id is None
        assert run.call_cap == cases * 2 * cap and replay_step_limit(run) == 6 + 6 * cases > 64
        assert (
            len(
                list(
                    db.scalars(
                        select(StageArtifact).where(
                            StageArtifact.run_id == run.id,
                            StageArtifact.stage_key.like("%:execution.v1"),
                        )
                    )
                )
            )
            == cases * 2
        )
        assert (
            len(
                list(
                    db.scalars(
                        select(EvolutionEvaluation).where(EvolutionEvaluation.run_id == run.id)
                    )
                )
            )
            == 1
        )
        assert run.calls_consumed <= run.call_cap
        # Teaching requires a two-call preflight per arm. At cap1 the legal
        # 100-call reservation exercises 100 arms, not 100 successful calls.
        assert run.calls_consumed == (cases * 4 if cap == 2 else 0)
        result = app.runtime.stage(run.id, "result")
        assert result["human_effectiveness_validated"] is False
    async with app.graph_runtime.checkpointers.open() as saver:
        saved = await saver.aget_tuple({"configurable": {"thread_id": run.id}})
        state = saved.checkpoint["channel_values"]
        assert not any(
            k in str(state) for k in ("required_fact_ids", "blocks", "episodes", "expected")
        )


class EngineeredCrash(BaseException):
    pass


def expire_owned_run(app, run_id):
    from datetime import timedelta

    from careerforge.db import clock

    with app.db.transaction() as db:
        run = db.get(AgentRun, run_id)
        run.lease_until = clock(db) - timedelta(seconds=1)
    app.runtime.reap()


@pytest.mark.asyncio
async def test_candidate_receipt_commit_gap_and_strict_result_input_hash(env, monkeypatch):
    from careerforge.errors import DomainError
    from careerforge.services.evolution_stages import reconcile

    app = env["app"]
    conv, _, _, receipt = await candidate_session(env)
    original = app.evolution.commit_reflection

    def crash(*args, **kwargs):
        raise EngineeredCrash()

    monkeypatch.setattr(app.evolution, "commit_reflection", crash)
    with pytest.raises(EngineeredCrash):
        await app.drain()
    detail = app.evolution_workflows.detail(env["actor"], conv, receipt["workflow_id"])
    with app.db.read() as db:
        assert (
            len(
                list(
                    db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == detail["run_id"]))
                )
            )
            == 1
        )
    monkeypatch.setattr(app.evolution, "commit_reflection", original)
    expire_owned_run(app, detail["run_id"])
    await app.drain()
    with app.db.transaction() as db:
        artifact = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == detail["run_id"], StageArtifact.stage_key == "result"
            )
        )
        artifact.input_hash = "f" * 64
    with app.db.read() as db:
        run = db.get(AgentRun, detail["run_id"])
        assert run.calls_consumed == 1
    with pytest.raises(DomainError, match="演化阶段"):
        reconcile(app, run, terminal=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["receipt_before_execution", "execution_before_score"])
async def test_last_arm_recovery_reuses_receipts_and_preserves_truthful_latency(
    env, monkeypatch, window
):
    app = env["app"]
    conv, receipt = await replay_session(env)
    execute_original, score_original = (
        app.evolution.execute_replay_arm,
        app.evolution.score_replay_arm,
    )
    crashed = False

    async def execute(run, epoch, worker, index, arm):
        nonlocal crashed
        output = await execute_original(run, epoch, worker, index, arm)
        if window == "receipt_before_execution" and (index, arm) == (3, "baseline") and not crashed:
            crashed = True
            raise EngineeredCrash()
        return output

    def score(run, index, arm, output):
        nonlocal crashed
        if window == "execution_before_score" and (index, arm) == (3, "baseline") and not crashed:
            crashed = True
            raise EngineeredCrash()
        return score_original(run, index, arm, output)

    monkeypatch.setattr(app.evolution, "execute_replay_arm", execute)
    monkeypatch.setattr(app.evolution, "score_replay_arm", score)
    with pytest.raises(EngineeredCrash):
        await app.drain()
    detail = app.evolution_workflows.detail(env["actor"], conv, receipt["workflow_id"])
    with app.db.read() as db:
        run = db.get(AgentRun, detail["run_id"])
        before = run.calls_consumed
        artifact = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id,
                StageArtifact.stage_key == "replay:3:baseline:execution.v1",
            )
        )
        original_time = artifact.payload["execution"]["elapsed"] if artifact else None
    monkeypatch.setattr(app.evolution, "execute_replay_arm", execute_original)
    monkeypatch.setattr(app.evolution, "score_replay_arm", score_original)
    expire_owned_run(app, run.id)
    await app.drain()
    with app.db.read() as db:
        current = db.get(AgentRun, run.id)
        assert current.status == "succeeded" and current.calls_consumed == before
        assert (
            len(
                list(
                    db.scalars(
                        select(EvolutionEvaluation).where(EvolutionEvaluation.run_id == run.id)
                    )
                )
            )
            == 1
        )
        artifact = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id,
                StageArtifact.stage_key == "replay:3:baseline:execution.v1",
            )
        )
        output = artifact.payload["execution"]
        assert output["latency_complete"] is (window == "execution_before_score")
        if original_time is not None:
            assert output["elapsed"] == original_time


@pytest.mark.asyncio
async def test_replay_no_progress_stops_at_derived_bound_without_paid_retry(env, monkeypatch):
    from langgraph.graph import START, StateGraph

    from careerforge.models import RunEvent
    from careerforge.workflows.evolution_paid import EvolutionPaidGraph, EvolutionState

    app = env["app"]
    conv, admission = await replay_session(env, cases=4, cap=1)

    def looping(self, saver):
        async def no_progress(state):
            return {"current_stage": "select_arm"}

        builder = StateGraph(EvolutionState)
        builder.add_node("select_arm", no_progress)
        builder.add_edge(START, "select_arm")
        builder.add_edge("select_arm", "select_arm")
        return builder.compile(checkpointer=saver)

    monkeypatch.setattr(EvolutionPaidGraph, "graph", looping)
    await app.drain()
    detail = app.evolution_workflows.detail(env["actor"], conv, admission["workflow_id"])
    with app.db.read() as db:
        run = db.get(AgentRun, detail["run_id"])
        assert run.status == "failed" and run.calls_consumed == 0 and run.budget_settled
        assert not list(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id)))
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run.id)))
        assert any("GRAPH_STEP_LIMIT" in str(e.payload) for e in events)
        assert (
            len(list(db.scalars(select(AgentRun).where(AgentRun.workflow == "evolution_replay"))))
            == 1
        )
    await app.drain()
    with app.db.read() as db:
        assert db.get(AgentRun, run.id).calls_consumed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", ["candidate", "replay"])
@pytest.mark.parametrize("fault", ["missing", "request_hash", "rehashed_result", "false_as_zero"])
async def test_result_requires_original_atomic_effect_without_repair(
    env, monkeypatch, workflow, fault
):
    from copy import deepcopy

    from careerforge.errors import DomainError
    from careerforge.models import Idempotency
    from careerforge.services import evolution_stages as stages
    from careerforge.util import fingerprint

    app = env["app"]
    if workflow == "candidate":
        conv, _, _, admission = await candidate_session(env)
    else:
        conv, admission = await replay_session(env, cases=1)
    await app.drain()
    detail = app.evolution_workflows.detail(env["actor"], conv, admission["workflow_id"])
    with app.db.read() as db:
        run = db.get(AgentRun, detail["run_id"])
        assert run.status == "succeeded"
    assert stages.reconcile(app, run, terminal=True)
    calls = len(env["model"].calls)
    with app.db.transaction() as db:
        receipt = db.scalar(
            select(Idempotency).where(
                Idempotency.operation == "evolution.paid.effect.v1", Idempotency.key == run.id
            )
        )
        if fault == "missing":
            db.delete(receipt)
        elif fault == "request_hash":
            receipt.request_hash = "f" * 64
        else:
            payload = deepcopy(receipt.result)
            if fault == "rehashed_result":
                payload["effect"]["result"]["base_hash"] = "f" * 64
            else:
                payload["effect"]["result"]["published"] = 0
            payload["effect_hash"] = fingerprint(payload["effect"])
            receipt.result = payload

    def forbidden(*args, **kwargs):
        raise AssertionError("A reader must never recreate or repair the original effect")

    monkeypatch.setattr(stages, "save_effect_in", forbidden)
    with pytest.raises(DomainError, match="原始事务效果回执"):
        stages.reconcile(app, run, terminal=True)
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        assert db.get(AgentRun, run.id).status == "succeeded"
        if fault == "missing":
            assert (
                db.scalar(
                    select(Idempotency).where(
                        Idempotency.operation == "evolution.paid.effect.v1",
                        Idempotency.key == run.id,
                    )
                )
                is None
            )
