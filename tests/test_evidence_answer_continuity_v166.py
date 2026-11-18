"""Real API/service interruption-continuation probes, isolated synthetic DBs only."""

import inspect
import json

import httpx
import pytest
from conftest import publish_fixture
from pydantic import ValidationError
from sqlalchemy import select
from test_evolution import candidate, dataset

from careerforge.api import create_app
from careerforge.contracts import TurnInput
from careerforge.contracts_answer_completion_v166 import (
    EVIDENCE_ANSWER_STATE,
    EVIDENCE_CONTINUITY_CONTRACT,
)
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    Conversation,
    KnowledgeGap,
    ModelReceipt,
    PolicyObservation,
    Turn,
)
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services import answer_continuity, answers, conversations, rollouts
from careerforge.services.dependencies import invalidate
from careerforge.services.evolution_replay import engine_versions
from careerforge.services.rollouts import RolloutService
from careerforge.util import fingerprint


def submit(env, cid=None, *, question="解释 Tool Calling 返回值", revision=1, key="first"):
    app, actor = env["app"], env["actor"]
    cid = cid or app.conversations.create(actor)
    return cid, app.conversations.submit(
        actor, cid, TurnInput(question=question, expected_revision=revision, idempotency_key=key)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["before_finish", "after_finish"])
async def test_http_recovered_evidence_answer_continues_like_normal_completion(
    env, monkeypatch, position
):
    await publish_fixture(env)
    app = env["app"]
    calls = len(env["model"].calls)
    finish = app.answers.finish

    def crash(*args, **kwargs):
        if position == "after_finish":
            finish(*args, **kwargs)
        raise RuntimeError("synthetic crash after suffix before delivery")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app), raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        conversations, first_turns, focuses, following = {}, {}, {}, {}
        with app.db.read() as db:
            before = {
                model.__name__: list(db.scalars(select(model.id)))
                for model in (AgentRun, ModelReceipt)
            }
        for arm in ("recovered", "normal"):
            conv = (await client.post("/api/v1/conversations", json={})).json()
            cid = conversations[arm] = conv["conversation_id"]
            url = f"/api/v1/conversations/{cid}/turns"
            body = {
                "question": "解释 Tool Calling 返回值",
                "expected_revision": 1,
                "idempotency_key": "first",
            }
            if arm == "recovered":
                monkeypatch.setattr(app.answers, "finish", crash)
                assert (await client.post(url, json=body)).status_code == 500
                monkeypatch.setattr(app.answers, "finish", finish)
            first = await client.post(url, json=body)
            assert first.status_code == 202
            first_turns[arm] = first.json()["turn_id"]
            focuses[arm] = (await client.get(f"/api/v1/conversations/{cid}")).json()["focus"]
            next_turn = await client.post(
                url,
                json={"question": "继续", "expected_revision": 2, "idempotency_key": "next"},
            )
            assert next_turn.status_code == 202
            tid = next_turn.json()["turn_id"]
            following[arm] = app.answers.committed(env["actor"], tid)
        print(
            "CONTINUATION_PROBE "
            + json.dumps(
                {
                    "focuses": focuses,
                    "following_kinds": {
                        arm: [b["kind"] for b in blocks] for arm, blocks in following.items()
                    },
                    "new_model_calls": len(env["model"].calls) - calls,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        assert all(
            any(b["kind"] == "repository_fact" for b in blocks) for blocks in following.values()
        )
        for arm in focuses:
            assert focuses[arm]["last_turn_id"] == first_turns[arm]
        assert {k: v for k, v in focuses["normal"].items() if k != "last_turn_id"} == {
            k: v for k, v in focuses["recovered"].items() if k != "last_turn_id"
        }
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        assert before == {
            model.__name__: list(db.scalars(select(model.id))) for model in (AgentRun, ModelReceipt)
        }
        assert all(db.get(Turn, tid).status == "completed" for tid in first_turns.values())


@pytest.mark.asyncio
async def test_every_continuity_tail_boundary_is_atomic_and_observation_is_once(env, monkeypatch):
    await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    identifier, _, _, _, _ = await candidate(env)
    report = await app.evolution.evaluate(
        actor, identifier, dataset(env, name="continuity-synthetic")
    )
    trial = app.rollouts.start(actor, identifier, report["evaluation_id"], 1, 50, 100, 1000)
    calls = len(env["model"].calls)
    phases = [
        "after_plan",
        "after_first_fact",
        "after_assessment",
        "before_observation",
        "after_observation",
        "after_event",
        "after_finish",
    ]
    for phase in phases:
        cid, turn = submit(env)
        tid = turn["turn_id"]
        original_plan = app.answers.freeze_evidence_plan
        original_block = app.answers.commit_block
        original_observe = RolloutService.observe_turn_in
        original_event = answers.stream_event
        original_finish = app.answers.finish

        def after_plan(*args, original_plan=original_plan, phase=phase, **kwargs):
            original_plan(*args, **kwargs)
            raise RuntimeError(phase)

        def after_block(*args, original_block=original_block, phase=phase, **kwargs):
            original_block(*args, **kwargs)
            if args[4] == 0:
                raise RuntimeError(phase)

        def after_assessment(*args, phase=phase, **kwargs):
            raise RuntimeError(phase)

        def observe(
            db, turn_id, blocks, latency, status, original_observe=original_observe, phase=phase
        ):
            if phase == "before_observation":
                raise RuntimeError(phase)
            original_observe(db, turn_id, blocks, latency, status)
            raise RuntimeError(phase)

        def event(db, turn_id, kind, payload, original_event=original_event, phase=phase):
            original_event(db, turn_id, kind, payload)
            if kind == "answer.completed":
                raise RuntimeError(phase)

        def finish(*args, original_finish=original_finish, phase=phase, **kwargs):
            original_finish(*args, **kwargs)
            raise RuntimeError(phase)

        with monkeypatch.context() as patch:
            if phase == "after_plan":
                patch.setattr(app.answers, "freeze_evidence_plan", after_plan)
            elif phase == "after_first_fact":
                patch.setattr(app.answers, "commit_block", after_block)
            elif phase == "after_assessment":
                patch.setattr(app.answers, "commit", after_assessment)
            elif phase in {"before_observation", "after_observation"}:
                patch.setattr(RolloutService, "observe_turn_in", staticmethod(observe))
            elif phase == "after_event":
                patch.setattr(answers, "stream_event", event)
            else:
                patch.setattr(app.answers, "finish", finish)
            with pytest.raises(RuntimeError, match=phase):
                await app.conversations.process(tid)
        committed = phase == "after_finish"
        with app.db.read() as db:
            row = db.get(Turn, tid)
            obs = list(
                db.scalars(select(PolicyObservation).where(PolicyObservation.turn_id == tid))
            )
            assert (row.status == "completed") is committed
            assert bool(db.get(Conversation, cid).focus) is committed
            assert len(obs) == int(committed)
            assert ("continuity_result" in row.focus[EVIDENCE_ANSWER_STATE]) is committed
        await app.conversations.process(tid)
        packet = app.answers.events(actor, tid)
        focus = app.conversations.detail(actor, cid)["focus"]
        assert packet["status"] == "completed" and focus["last_turn_id"] == tid
        assert focus["topic_keys"] == ["agent.tool_calling"]
        with app.db.read() as db:
            obs = list(
                db.scalars(select(PolicyObservation).where(PolicyObservation.turn_id == tid))
            )
            assert len(obs) == 1 and obs[0].rollout_id == trial["rollout_id"]
            assert obs[0].evidence_gaps == 1 and obs[0].guard_blocks == 0
            assert obs[0].status == "completed" and obs[0].latency_ms >= 0
            saved = db.get(Turn, tid).focus[EVIDENCE_ANSWER_STATE]
            assert saved["continuity_result"]["focus_status"] == "applied"
            assert saved["assessment"]["task_satisfied"] is None
            assert saved["assessment"]["continuity_observation"]["latency_ms"] == obs[0].latency_ms
        await app.conversations.process(tid)
        assert app.answers.events(actor, tid) == packet
        assert app.conversations.detail(actor, cid)["focus"] == focus
        with app.db.read() as db:
            assert (
                len(
                    list(
                        db.scalars(
                            select(PolicyObservation.id).where(PolicyObservation.turn_id == tid)
                        )
                    )
                )
                == 1
            )
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
async def test_evidence_gap_focus_and_delivery_roll_back_together(env, monkeypatch):
    app = env["app"]
    cid, turn = submit(env)
    original = answers.stream_event

    def crash(db, turn_id, kind, payload):
        original(db, turn_id, kind, payload)
        if kind == "answer.completed":
            raise RuntimeError("rollback complete transaction")

    with monkeypatch.context() as patch:
        patch.setattr(answers, "stream_event", crash)
        with pytest.raises(RuntimeError):
            await app.conversations.process(turn["turn_id"])
    with app.db.read() as db:
        assert db.get(Turn, turn["turn_id"]).status == "running"
        assert db.get(Conversation, cid).focus == {}
        assert list(db.scalars(select(KnowledgeGap.id))) == []
    await app.conversations.process(turn["turn_id"])
    with app.db.read() as db:
        assert db.get(Turn, turn["turn_id"]).status == "completed"
        assert db.get(Conversation, cid).focus["last_turn_id"] == turn["turn_id"]
        assert len(list(db.scalars(select(KnowledgeGap.id)))) == 1
    assert env["model"].calls == []


@pytest.mark.asyncio
async def test_completed_replay_cannot_clobber_a_newer_real_turn(env):
    await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    cid, first = submit(env)
    await app.conversations.process(first["turn_id"])
    _, second = submit(env, cid, question="什么是 RAG", revision=2, key="second")
    await app.conversations.process(second["turn_id"])
    before = app.conversations.detail(actor, cid)
    assert before["focus"]["last_turn_id"] == second["turn_id"]
    _, replay = submit(env, cid)
    assert replay["turn_id"] == first["turn_id"]
    await app.conversations.process(first["turn_id"])
    assert app.conversations.detail(actor, cid) == before


@pytest.mark.asyncio
async def test_delayed_finalization_preserves_newer_focus_revision(env, monkeypatch):
    await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    cid, turn = submit(env)

    def crash(*args, **kwargs):
        raise RuntimeError("stop before final transaction")

    with monkeypatch.context() as patch:
        patch.setattr(app.answers, "finish", crash)
        with pytest.raises(RuntimeError):
            await app.conversations.process(turn["turn_id"])
    newer = {"last_turn_id": "synthetic-newer-state", "topic_keys": ["rag.retrieval"], "kept": True}
    with app.db.transaction() as db:
        current = db.get(Conversation, cid)
        current.revision += 1
        current.focus = newer
    await app.conversations.process(turn["turn_id"])
    assert app.conversations.detail(actor, cid)["focus"] == newer
    with app.db.read() as db:
        result = db.get(Turn, turn["turn_id"]).focus[EVIDENCE_ANSWER_STATE]["continuity_result"]
        assert result["focus_status"] == "superseded"


@pytest.mark.asyncio
async def test_continuity_write_obeys_question_privacy_revocation_and_cancel_fences(
    env, monkeypatch
):
    _, publication, _ = await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    calls = len(env["model"].calls)
    for fault in ("question", "cancelled", "privacy", "publication"):
        cid, turn = submit(env)

        def crash(*args, **kwargs):
            raise RuntimeError("stop before final transaction")

        with monkeypatch.context() as patch:
            patch.setattr(app.answers, "finish", crash)
            with pytest.raises(RuntimeError):
                await app.conversations.process(turn["turn_id"])
        with app.db.transaction() as db:
            row = db.get(Turn, turn["turn_id"])
            if fault == "question":
                row.question = "Different original question"
            elif fault == "cancelled":
                row.status = "cancelled"
            elif fault == "privacy":
                db.get(PrivacyState, actor).generation += 1
            else:
                invalidate(db, actor, publication, "revoked", "synthetic continuity fence")
        with pytest.raises(DomainError):
            await app.conversations.process(turn["turn_id"])
        with app.db.read() as db:
            row = db.get(Turn, turn["turn_id"])
            assert row.status != "completed"
            assert "continuity_result" not in row.focus[EVIDENCE_ANSWER_STATE]
            assert db.get(Conversation, cid).focus == {}
            assert (
                list(
                    db.scalars(
                        select(PolicyObservation.id).where(PolicyObservation.turn_id == row.id)
                    )
                )
                == []
            )
    assert len(env["model"].calls) == calls


def test_continuity_marker_is_not_public_input_and_engine_binds_all_helpers(monkeypatch):
    for marker in (None, "legacy", "future", EVIDENCE_CONTINUITY_CONTRACT):
        with pytest.raises(ValidationError):
            TurnInput.model_validate(
                {
                    "question": "Explain",
                    "expected_revision": 1,
                    "idempotency_key": "k",
                    "evidence_continuity_contract": marker,
                }
            )
    original = inspect.getsource
    before = engine_versions()
    assert before["answer_continuity_implementation"] == fingerprint(
        original(answer_continuity) + original(conversations) + original(rollouts)
    )
    for target in (answer_continuity, conversations, rollouts):
        with monkeypatch.context() as patch:
            patch.setattr(
                inspect,
                "getsource",
                lambda obj, target=target: (
                    original(obj) + "\n# synthetic drift" if obj is target else original(obj)
                ),
            )
            assert (
                engine_versions()["answer_continuity_implementation"]
                != before["answer_continuity_implementation"]
            )


@pytest.mark.asyncio
async def test_server_owns_continuity_admission_and_preserves_historical_boundary(env):
    app, actor = env["app"], env["actor"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        cid = app.conversations.create(actor)
        for marker in (None, "legacy", "future"):
            response = await client.post(
                f"/api/v1/conversations/{cid}/turns",
                json={
                    "question": "Explain",
                    "expected_revision": 1,
                    "idempotency_key": "marker",
                    "evidence_continuity_contract": marker,
                },
            )
            assert response.status_code == 422
        with app.db.transaction() as db:
            db.get(Conversation, cid).focus = {"evidence_continuity_contract": "spoofed"}
        _, turn = submit(env, cid)
        with app.db.read() as db:
            assert (
                db.get(Turn, turn["turn_id"]).focus["evidence_continuity_contract"]
                == EVIDENCE_CONTINUITY_CONTRACT
            )
        # Explicit synthetic historical admission, not a public downgrade route.
        with app.db.transaction() as db:
            row = db.get(Turn, turn["turn_id"])
            row.focus = {k: v for k, v in row.focus.items() if k != "evidence_continuity_contract"}
        await app.conversations.process(turn["turn_id"])
        with app.db.read() as db:
            state = db.get(Turn, turn["turn_id"]).focus[EVIDENCE_ANSWER_STATE]
            assert "continuity_contract" not in state["plan"]
            assert "continuity_observation" not in state["assessment"]
            assert "continuity_result" not in state
            assert db.get(Conversation, cid).focus["last_turn_id"] == turn["turn_id"]
    assert env["model"].calls == []
