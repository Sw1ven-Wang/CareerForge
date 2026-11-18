"""Evidence-only crash/retry probes using the real API and isolated fixture DB."""

import json

import httpx
import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.api import create_app
from careerforge.contracts import TurnInput
from careerforge.contracts_answer_completion_v166 import EVIDENCE_ANSWER_STATE
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    AnswerBlock,
    Conversation,
    KnowledgeGap,
    StageArtifact,
    StreamEvent,
    Turn,
)
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.dependencies import invalidate


def submit_evidence_only(env):
    app = env["app"]
    conversation = app.conversations.create(env["actor"])
    return app.conversations.submit(
        env["actor"],
        conversation,
        TurnInput(
            question="解释 Tool Calling 返回值",
            expected_revision=1,
            idempotency_key="evidence-only-interruption",
        ),
    )


@pytest.mark.asyncio
async def test_evidence_only_http_retry_after_suffix_commit_preserves_unverified_outcome(
    env, monkeypatch
):
    await publish_fixture(env)
    app = env["app"]
    before_calls = len(env["model"].calls)
    with app.db.read() as db:
        before_runs = list(db.scalars(select(AgentRun.id)))
        before_stages = list(db.scalars(select(StageArtifact.id)))
    finish = app.answers.finish

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic crash after evidence-only suffix commit")

    monkeypatch.setattr(app.answers, "finish", crash)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app), raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        conversation = (await client.post("/api/v1/conversations", json={})).json()
        url = f"/api/v1/conversations/{conversation['conversation_id']}/turns"
        body = {
            "question": "解释 Tool Calling 返回值",
            "expected_revision": 1,
            "idempotency_key": "evidence-only-interruption",
        }
        first = await client.post(url, json=body)
        with app.db.read() as db:
            turn = db.scalar(
                select(Turn).where(Turn.conversation_id == conversation["conversation_id"])
            )
            turn_id = turn.id
            saved_blocks = list(
                db.scalars(
                    select(AnswerBlock)
                    .where(AnswerBlock.turn_id == turn.id)
                    .order_by(AnswerBlock.ordinal)
                )
            )
            print(
                "BEFORE_RETRY "
                + json.dumps(
                    {
                        "http_status": first.status_code,
                        "turn_id": turn.id,
                        "run_id": turn.run_id,
                        "status": turn.status,
                        "manifest_id": turn.manifest_id,
                        "block_ids": [row.id for row in saved_blocks],
                        "block_kinds": [row.kind for row in saved_blocks],
                        "block_hashes": [row.content_hash for row in saved_blocks],
                        "call_count_before": before_calls,
                        "call_count_after": len(env["model"].calls),
                    },
                    sort_keys=True,
                )
            )
            assert turn.run_id is None and turn.status == "running"
            assert len(saved_blocks) == 2
            assert saved_blocks[-1].content["gap_category"] == "verification"
        assert first.status_code == 500
        monkeypatch.setattr(app.answers, "finish", finish)
        retry = await client.post(url, json=body)
        packet = (await client.get(f"/api/v1/turns/{turn_id}")).json()
        print(
            "AFTER_RETRY "
            + json.dumps(
                {
                    "http_status": retry.status_code,
                    "response": retry.json(),
                    "turn_status": packet["status"],
                    "call_count": len(env["model"].calls),
                    "event_types": [event["type"] for event in packet["events"]],
                },
                sort_keys=True,
            )
        )
        assert retry.status_code == 202
        assert packet["status"] == "completed"
        final = [event for event in packet["events"] if event["type"] == "answer.completed"]
        assert len(final) == 1
        assert final[0]["payload"]["answer_coverage_status"] == "unverified"
        assert final[0]["payload"]["task_satisfied"] is None
        again = await client.post(url, json=body)
        assert again.status_code == 202
        assert (await client.get(f"/api/v1/turns/{turn_id}")).json() == packet
    assert len(env["model"].calls) == before_calls
    with app.db.read() as db:
        assert list(db.scalars(select(AgentRun.id))) == before_runs
        assert list(db.scalars(select(StageArtifact.id))) == before_stages
        assert db.scalar(select(KnowledgeGap.id).where(KnowledgeGap.turn_id == turn_id)) is None


@pytest.mark.asyncio
async def test_evidence_only_delivery_atomically_records_real_evidence_gap(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    turn = submit_evidence_only(env)
    commit = app.answers.commit

    def crash(*args, **kwargs):
        commit(*args, **kwargs)
        raise RuntimeError("synthetic crash after no-run delivery before bookkeeping")

    monkeypatch.setattr(app.answers, "commit", crash)
    with pytest.raises(RuntimeError):
        await app.conversations.process(turn["turn_id"])
    packet = app.answers.events(actor, turn["turn_id"])
    with app.db.read() as db:
        gaps = list(db.scalars(select(KnowledgeGap).where(KnowledgeGap.turn_id == turn["turn_id"])))
        print(
            "AFTER_NO_RUN_DELIVERY "
            + json.dumps(
                {
                    "turn_status": packet["status"],
                    "final_payload": packet["events"][-1]["payload"],
                    "knowledge_gap_count": len(gaps),
                    "call_count": len(env["model"].calls),
                    "run_count": len(list(db.scalars(select(AgentRun.id)))),
                },
                sort_keys=True,
            )
        )
        assert len(gaps) == 1
    assert packet["status"] == "completed"
    assert packet["events"][-1]["payload"]["answer_coverage_status"] == "unknown"
    assert packet["events"][-1]["payload"]["task_satisfied"] is False
    monkeypatch.setattr(app.answers, "commit", commit)
    await app.conversations.process(turn["turn_id"])
    assert app.answers.events(actor, turn["turn_id"]) == packet
    assert env["model"].calls == []
    with app.db.read() as db:
        assert len(list(db.scalars(select(KnowledgeGap.id)))) == 1
        assert list(db.scalars(select(StageArtifact.id))) == []


@pytest.mark.asyncio
async def test_evidence_only_retry_before_suffix_keeps_frozen_manifest(env, monkeypatch):
    await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    turn = submit_evidence_only(env)
    calls = len(env["model"].calls)
    commit = app.answers.commit

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic crash after no-run assessment before suffix")

    monkeypatch.setattr(app.answers, "commit", crash)
    with pytest.raises(RuntimeError):
        await app.conversations.process(turn["turn_id"])
    with app.db.read() as db:
        before_manifest = db.get(Turn, turn["turn_id"]).manifest_id
        before_ids = list(
            db.scalars(select(AnswerBlock.id).where(AnswerBlock.turn_id == turn["turn_id"]))
        )
    monkeypatch.setattr(app.answers, "commit", commit)
    await app.conversations.process(turn["turn_id"])
    packet = app.answers.events(actor, turn["turn_id"])
    assert packet["status"] == "completed"
    assert packet["events"][-1]["payload"]["task_satisfied"] is None
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        assert db.get(Turn, turn["turn_id"]).manifest_id == before_manifest
        after_ids = list(
            db.scalars(select(AnswerBlock.id).where(AnswerBlock.turn_id == turn["turn_id"]))
        )
        assert set(before_ids) < set(after_ids) and len(after_ids) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["question", "privacy", "publication", "cancelled"])
async def test_evidence_only_saved_completion_cannot_bypass_live_fences(env, monkeypatch, mutation):
    _, publication, _ = await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    turn = submit_evidence_only(env)
    calls = len(env["model"].calls)
    finish = app.answers.finish

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic crash before no-run completion")

    monkeypatch.setattr(app.answers, "finish", crash)
    with pytest.raises(RuntimeError):
        await app.conversations.process(turn["turn_id"])
    with app.db.transaction() as db:
        row = db.get(Turn, turn["turn_id"])
        if mutation == "question":
            row.question = "Explain another function instead."
        elif mutation == "privacy":
            db.get(PrivacyState, actor).generation += 1
        elif mutation == "publication":
            invalidate(db, actor, publication, "revoked", "synthetic no-run replay revocation")
        else:
            row.status = "cancelled"
    monkeypatch.setattr(app.answers, "finish", finish)
    with pytest.raises(DomainError):
        await app.conversations.process(turn["turn_id"])
    packet = app.answers.events(actor, turn["turn_id"])
    assert not any(event["type"] == "answer.completed" for event in packet["events"])
    if mutation in {"question", "privacy"}:
        assert packet["status"] == "blocked"
        assert packet["events"][-1]["type"] == "answer.interrupted"
        assert all(event["payload"].get("redacted") is True for event in packet["events"])
        with pytest.raises(DomainError):
            app.answers.committed(actor, turn["turn_id"])
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        assert db.get(Turn, turn["turn_id"]).status != "completed"
        assert (
            db.scalar(
                select(StreamEvent.id).where(
                    StreamEvent.turn_id == turn["turn_id"],
                    StreamEvent.event_type == "answer.completed",
                )
            )
            is None
        )
        assert (
            db.scalar(select(KnowledgeGap.id).where(KnowledgeGap.turn_id == turn["turn_id"]))
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("position", [0, 1])
async def test_evidence_only_replays_after_first_or_last_fact_commit(env, monkeypatch, position):
    await publish_fixture(env, "source-a", "fixtures/a")
    await publish_fixture(env, "source-b", "fixtures/b")
    app, actor = env["app"], env["actor"]
    turn = submit_evidence_only(env)
    calls = len(env["model"].calls)
    commit_block = app.answers.commit_block

    def crash(actor, turn_id, content, manifest_id, ordinal, run=None):
        result = commit_block(actor, turn_id, content, manifest_id, ordinal, run)
        if ordinal == position:
            raise RuntimeError("synthetic crash immediately after no-run fact commit")
        return result

    monkeypatch.setattr(app.answers, "commit_block", crash)
    with pytest.raises(RuntimeError):
        await app.conversations.process(turn["turn_id"])
    with app.db.read() as db:
        before_manifest = db.get(Turn, turn["turn_id"]).manifest_id
        before_blocks = list(
            db.scalars(
                select(AnswerBlock)
                .where(AnswerBlock.turn_id == turn["turn_id"])
                .order_by(AnswerBlock.ordinal)
            )
        )
        before_ids = [row.id for row in before_blocks]
    assert len(before_blocks) == position + 1
    monkeypatch.setattr(app.answers, "commit_block", commit_block)
    try:
        await app.conversations.process(turn["turn_id"])
    except DomainError as exc:
        print(
            "NO_RUN_FACT_REPLAY_FAILURE "
            + json.dumps(
                {
                    "fact_position": position,
                    "error_code": exc.code,
                    "calls_before": calls,
                    "calls_after": len(env["model"].calls),
                    "manifest_id": before_manifest,
                    "accepted_block_ids": before_ids,
                },
                sort_keys=True,
            )
        )
        raise
    packet = app.answers.events(actor, turn["turn_id"])
    assert packet["status"] == "completed"
    assert packet["events"][-1]["payload"]["task_satisfied"] is None
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        assert db.get(Turn, turn["turn_id"]).manifest_id == before_manifest
        after_ids = list(
            db.scalars(
                select(AnswerBlock.id)
                .where(AnswerBlock.turn_id == turn["turn_id"])
                .order_by(AnswerBlock.ordinal)
            )
        )
        assert after_ids[: len(before_ids)] == before_ids and len(after_ids) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["before_plan", "after_plan", "during_assessment"])
async def test_evidence_only_replays_around_plan_and_assessment_persistence(
    env, monkeypatch, position
):
    await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    turn = submit_evidence_only(env)
    calls = len(env["model"].calls)
    method = (
        "_save_evidence_completion_in"
        if position == "during_assessment"
        else "freeze_evidence_plan"
    )
    original = getattr(app.answers, method)

    def crash(*args, **kwargs):
        if position != "before_plan":
            original(*args, **kwargs)
        raise RuntimeError("synthetic crash at no-run persistence boundary")

    monkeypatch.setattr(app.answers, method, crash)
    with pytest.raises(RuntimeError):
        await app.conversations.process(turn["turn_id"])
    with app.db.read() as db:
        row = db.get(Turn, turn["turn_id"])
        state = row.focus.get(EVIDENCE_ANSWER_STATE)
        assert bool(state) is (position != "before_plan")
        if position == "before_plan":
            # No incomplete admission may pin the next attempt's selection.
            assert row.manifest_id is None
        if state:
            assert row.manifest_id == state["plan"]["retrieval_manifest_id"]
            assert "assessment" not in state  # During-assessment write rolled back.
            frozen_manifest = state["plan"]["retrieval_manifest_id"]
        assert len(
            list(db.scalars(select(AnswerBlock.id).where(AnswerBlock.turn_id == row.id)))
        ) == (1 if position == "during_assessment" else 0)
    monkeypatch.setattr(app.answers, method, original)
    await app.conversations.process(turn["turn_id"])
    packet = app.answers.events(actor, turn["turn_id"])
    assert packet["status"] == "completed"
    assert packet["events"][-1]["payload"]["task_satisfied"] is None
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        row = db.get(Turn, turn["turn_id"])
        assert row.focus[EVIDENCE_ANSWER_STATE]["assessment"]["status"] == "unverified"
        if position != "before_plan":
            assert row.manifest_id == frozen_manifest


@pytest.mark.asyncio
async def test_evidence_only_plan_generation_fence_is_in_block_commit_transaction(env, monkeypatch):
    await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    turn = submit_evidence_only(env)
    calls = len(env["model"].calls)
    original = app.answers.commit_block

    def change_generation(*args, **kwargs):
        with app.db.transaction() as db:
            db.get(PrivacyState, actor).generation += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(app.answers, "commit_block", change_generation)
    with pytest.raises(DomainError) as raised:
        await app.conversations.process(turn["turn_id"])
    assert raised.value.code == "PRIVACY_GENERATION_CHANGED"
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        assert (
            list(db.scalars(select(AnswerBlock.id).where(AnswerBlock.turn_id == turn["turn_id"])))
            == []
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_evidence_only_admission_does_not_inherit_state_or_allow_completion_downgrade(
    env, legacy
):
    await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    conversation = app.conversations.create(actor)
    with app.db.transaction() as db:
        db.get(Conversation, conversation).focus = {
            EVIDENCE_ANSWER_STATE: {"plan": {"spoof": True}}
        }
    turn = app.conversations.submit(
        actor,
        conversation,
        TurnInput(
            question="解释 Tool Calling 返回值", expected_revision=1, idempotency_key="scope"
        ),
    )
    with app.db.transaction() as db:
        row = db.get(Turn, turn["turn_id"])
        assert EVIDENCE_ANSWER_STATE not in row.focus
        if legacy:
            row.focus = {k: v for k, v in row.focus.items() if k != "answer_completion_contract"}
    calls = len(env["model"].calls)
    if legacy:
        with pytest.raises(DomainError) as downgrade:
            await app.conversations.process(turn["turn_id"])
        assert downgrade.value.code == "ORDINARY_CHECKER_BINDING"
        packet = app.answers.events(actor, turn["turn_id"])
        assert packet["status"] == "blocked"
        assert [event["type"] for event in packet["events"]] == ["answer.interrupted"]
        with app.db.read() as db:
            assert db.get(Turn, turn["turn_id"]).status != "completed"
            assert (
                db.scalar(select(AnswerBlock.id).where(AnswerBlock.turn_id == turn["turn_id"]))
                is None
            )
    else:
        await app.conversations.process(turn["turn_id"])
        packet = app.answers.events(actor, turn["turn_id"])
        assert packet["status"] == "completed"
        final = packet["events"][-1]["payload"]
        assert "answer_completion_contract" in final
        actual = [event for event in packet["events"] if event["type"] == "answer.block.validated"]
        assert len(actual) == 2
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("with_fact", [True, False])
async def test_evidence_only_direct_finish_requires_durable_assessment(env, monkeypatch, with_fact):
    if with_fact:
        await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    turn = submit_evidence_only(env)
    calls = len(env["model"].calls)
    original = app.answers._save_evidence_completion_in

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic crash before assessment persistence")

    monkeypatch.setattr(app.answers, "_save_evidence_completion_in", crash)
    with pytest.raises(RuntimeError):
        await app.conversations.process(turn["turn_id"])
    with app.db.read() as db:
        manifest_id = db.get(Turn, turn["turn_id"]).manifest_id
    with pytest.raises(DomainError) as raised:
        app.answers.finish(actor, turn["turn_id"], manifest_id)
    assert raised.value.code == "ANSWER_COMPLETION_REQUIRED"
    assert not any(
        event["type"] == "answer.completed"
        for event in app.answers.events(actor, turn["turn_id"])["events"]
    )
    monkeypatch.setattr(app.answers, "_save_evidence_completion_in", original)
    await app.conversations.process(turn["turn_id"])
    assert app.answers.events(actor, turn["turn_id"])["status"] == "completed"
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, {"assessment": {"status": "complete"}}])
async def test_http_cannot_supply_evidence_only_server_state(env, state):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env["app"])),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        conversation = (await client.post("/api/v1/conversations", json={})).json()
        response = await client.post(
            f"/api/v1/conversations/{conversation['conversation_id']}/turns",
            json={
                "question": "Explain",
                "expected_revision": 1,
                "idempotency_key": "spoof",
                EVIDENCE_ANSWER_STATE: state,
            },
        )
        assert response.status_code == 422
    assert env["model"].calls == []
