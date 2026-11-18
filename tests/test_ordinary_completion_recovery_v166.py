"""Persistence boundaries and body-free ordinary invalidation delivery.

Legacy persisted-state fixtures here are synthetic schema contracts, not evidence
of an executable pre-completion release. Separate subprocess tests use real C3.
"""

from copy import deepcopy

import httpx
import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_answer_completion_no_run_v166 import submit_evidence_only
from test_answer_completion_v166 import completion_model

from careerforge.api import create_app
from careerforge.cli.stream import parse_events
from careerforge.contracts import RetrievalTaskSpec, TaskType
from careerforge.contracts_answer_completion_v166 import (
    ANSWER_COMPLETION_STAGE,
    EVIDENCE_ANSWER_STATE,
)
from careerforge.contracts_ordinary_checker_v166 import ORDINARY_CHECKER_KEY
from careerforge.errors import DomainError
from careerforge.models import (
    AnswerBlock,
    Conversation,
    Idempotency,
    KnowledgeGap,
    ModelReceipt,
    StreamEvent,
    Turn,
)
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services import ordinary_checker
from careerforge.services.policies import ensure_policies
from careerforge.services.security import create_principal


def snapshot(env, identifier):
    with env["app"].db.read() as db:
        turn = db.get(Turn, identifier)
        return {
            "status": turn.status,
            "focus": deepcopy(turn.focus),
            "manifest_id": turn.manifest_id,
            "blocks": [
                (row.id, row.content_hash, deepcopy(row.content))
                for row in db.scalars(select(AnswerBlock).where(AnswerBlock.turn_id == identifier))
            ],
            "completed": list(
                db.scalars(
                    select(StreamEvent.id).where(
                        StreamEvent.turn_id == identifier,
                        StreamEvent.event_type == "answer.completed",
                    )
                )
            ),
            "gaps": list(
                db.scalars(select(KnowledgeGap.id).where(KnowledgeGap.turn_id == identifier))
            ),
            "receipts": [
                (row.id, row.request_hash, row.status, deepcopy(row.response))
                for row in db.scalars(select(ModelReceipt))
            ],
        }


def mutate(env, identifier, kind):
    with env["app"].db.transaction() as db:
        turn = db.get(Turn, identifier)
        if kind == "question":
            turn.question = "QUESTION_CHANGED_DO_NOT_SEND"
        elif kind == "privacy":
            db.get(PrivacyState, env["actor"]).generation += 1
        else:
            turn.focus = {k: v for k, v in turn.focus.items() if k != "answer_completion_contract"}


def assert_redacted(packet):
    assert packet["status"] == "blocked" and packet["events"]
    for event in packet["events"]:
        assert event["type"] in {"answer.interrupted", "answer.evidence_invalidated"}
        assert set(event["payload"]) <= {"code", "redacted", "block_id"}
        assert event["payload"]["redacted"] is True
        assert event["payload"]["code"] in {
            "ORDINARY_CHECKER_BINDING",
            "PRIVACY_GENERATION_CHANGED",
        }
    assert packet["events"][-1]["type"] == "answer.interrupted"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["question", "privacy", "completion"])
async def test_http_get_and_sse_deliver_persistent_redaction_without_business_changes(env, kind):
    app, actor = env["app"], env["actor"]
    await publish_fixture(env)
    turn = submit_evidence_only(env)
    identifier = turn["turn_id"]
    await app.conversations.process(identifier)
    original = app.answers.events(actor, identifier)
    old_cursor = original["events"][-1]["seq"]
    mutate(env, identifier, kind)
    before = snapshot(env, identifier)
    calls = len(env["model"].calls)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        response = await client.get(f"/api/v1/turns/{identifier}")
        assert response.status_code == 200
        packet = response.json()
        assert_redacted(packet)
        assert all(event["seq"] > old_cursor for event in packet["events"])
        assert "QUESTION_CHANGED_DO_NOT_SEND" not in response.text
        assert all(block[2]["text"] not in response.text for block in before["blocks"])
        assert (await client.get(f"/api/v1/turns/{identifier}")).json() == packet
        stream = await client.get(
            f"/api/v1/turns/{identifier}/events", headers={"Last-Event-ID": str(old_cursor)}
        )
        assert stream.status_code == 200
        decoded = list(parse_events(stream.text.splitlines()))
        assert [event.name for event in decoded] == [e["type"] for e in packet["events"]]
        assert all(event.payload.get("redacted") is True for event in decoded)
        end = packet["events"][-1]["seq"]
        resumed = await client.get(
            f"/api/v1/turns/{identifier}/events", headers={"Last-Event-ID": str(end)}
        )
        assert resumed.status_code == 200 and not list(parse_events(resumed.text.splitlines()))
    assert (
        snapshot(env, identifier) == before
    )  # Safety notices do not alter these business records.
    assert len(env["model"].calls) == calls
    with pytest.raises(DomainError):
        app.answers.committed(actor, identifier)


@pytest.mark.asyncio
async def test_sse_midstream_generation_change_ends_with_redacted_notice(env, monkeypatch):
    app = env["app"]
    await publish_fixture(env)
    identifier = submit_evidence_only(env)["turn_id"]

    def crash(*args, **kwargs):
        raise RuntimeError("before delivery finish")

    with monkeypatch.context() as patch:
        patch.setattr(app.answers, "finish", crash)
        with pytest.raises(RuntimeError):
            await app.conversations.process(identifier)
    calls = len(env["model"].calls)
    events = app.answers.events
    reads = 0

    def current(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 3:
            mutate(env, identifier, "privacy")
        return events(*args, **kwargs)

    monkeypatch.setattr(app.answers, "events", current)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        response = await client.get(f"/api/v1/turns/{identifier}/events")
    decoded = list(parse_events(response.text.splitlines()))
    assert any(event.name == "answer.delta" for event in decoded)
    first_redacted = next(i for i, event in enumerate(decoded) if event.payload.get("redacted"))
    assert all(event.payload.get("redacted") is True for event in decoded[first_redacted:])
    assert decoded[-1].name == "answer.interrupted"
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        assert db.get(Turn, identifier).status == "running"  # This is only a read view.
        assert not list(
            db.scalars(
                select(StreamEvent.id).where(
                    StreamEvent.turn_id == identifier, StreamEvent.event_type == "answer.completed"
                )
            )
        )


@pytest.mark.asyncio
async def test_redaction_never_bypasses_current_owner_or_blocked_privacy(env):
    app, actor = env["app"], env["actor"]
    identifier = submit_evidence_only(env)["turn_id"]
    mutate(env, identifier, "completion")
    with app.db.transaction() as db:
        other, _, token = create_principal(db, "foreign-reader")
    with pytest.raises(DomainError) as foreign:
        app.answers.events(other.id, identifier)
    assert foreign.value.code == "NOT_FOUND"
    with app.db.transaction() as db:
        db.get(PrivacyState, actor).blocked = True
    with pytest.raises(DomainError) as blocked:
        app.answers.events(actor, identifier)
    assert blocked.value.code == "PRIVACY_ERASURE_PENDING"
    with app.db.read() as db:
        assert not list(
            db.scalars(
                select(StreamEvent.id).where(
                    StreamEvent.turn_id == identifier,
                    StreamEvent.event_type == "answer.interrupted",
                )
            )
        )


@pytest.mark.asyncio
async def test_selection_binding_rolls_back_inside_failed_plan_transaction(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    await publish_fixture(env)
    identifier = submit_evidence_only(env)["turn_id"]
    bind = ordinary_checker.bind_ordinary_selection_in

    def interrupted(*args, **kwargs):
        bind(*args, **kwargs)
        raise RuntimeError("after selection binding before plan commit")

    calls = len(env["model"].calls)
    with monkeypatch.context() as patch:
        patch.setattr(ordinary_checker, "bind_ordinary_selection_in", interrupted)
        with pytest.raises(RuntimeError):
            await app.conversations.process(identifier)
    with app.db.read() as db:
        turn = db.get(Turn, identifier)
        assert turn.manifest_id is None and EVIDENCE_ANSWER_STATE not in turn.focus
        assert not list(db.scalars(select(AnswerBlock.id).where(AnswerBlock.turn_id == identifier)))
    await app.conversations.process(identifier)
    assert app.answers.events(actor, identifier)["status"] == "completed"
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
async def test_committed_plan_cannot_rebind_another_same_question_selection(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    await publish_fixture(env)
    identifier = submit_evidence_only(env)["turn_id"]
    freeze = app.answers.freeze_evidence_plan

    def interrupted(*args, **kwargs):
        freeze(*args, **kwargs)
        raise RuntimeError("after plan commit")

    with monkeypatch.context() as patch:
        patch.setattr(app.answers, "freeze_evidence_plan", interrupted)
        with pytest.raises(RuntimeError):
            await app.conversations.process(identifier)
    before = snapshot(env, identifier)
    with app.db.read() as db:
        turn = db.get(Turn, identifier)
        spec = RetrievalTaskSpec(
            turn_id=identifier,
            task_type=TaskType.IMPLEMENTATION,
            user_question=turn.question,
            focus_revision=turn.focus_revision,
        )
        marker = deepcopy(turn.focus[ORDINARY_CHECKER_KEY])
    evidence = await app.retrieval.retrieve(actor, spec, answer_checker_contract=marker)
    with pytest.raises(DomainError):
        with app.db.transaction() as db:
            ordinary_checker.bind_ordinary_selection_in(
                db, actor, db.get(Turn, identifier), evidence["retrieval_manifest_id"]
            )
    assert snapshot(env, identifier) == before
    await app.conversations.process(identifier)
    assert snapshot(env, identifier)["manifest_id"] == before["manifest_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["answer_contract", "answer_completion_contract"])
@pytest.mark.parametrize("funded", [False, True])
async def test_each_answer_subcontract_is_frozen_with_or_without_run(env, field, funded):
    from careerforge.contracts import TurnInput

    app, actor = env["app"], env["actor"]
    conv = app.conversations.create(actor)
    turn = app.conversations.submit(
        actor,
        conv,
        TurnInput(
            question="value",
            expected_revision=1,
            idempotency_key="subcontract",
            funding_id=env["funding"] if funded else None,
        ),
    )
    with app.db.transaction() as db:
        row = db.get(Turn, turn["turn_id"])
        row.focus = {k: v for k, v in row.focus.items() if k != field}
    calls = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        with app.db.read() as db:
            ordinary_checker.validate_ordinary_turn_in(db, actor, db.get(Turn, turn["turn_id"]))
    assert error.value.code == "ORDINARY_CHECKER_BINDING"
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
async def test_synthetic_precompletion_persisted_state_keeps_legacy_wire(env):
    """Schema fixture from inception; not a claim of recovered historical code."""
    app, actor = env["app"], env["actor"]
    await publish_fixture(env)
    observed = completion_model(env)
    with app.db.transaction() as db:
        policies = ensure_policies(db, actor, env["ns"])
        conv = Conversation(principal_id=actor, namespace_id=env["ns"], revision=2, preferences={})
        db.add(conv)
        db.flush()
        turn = Turn(
            conversation_id=conv.id,
            question="解释 Tool Calling 返回值",
            focus_revision=1,
            focus={"answer_contract": "atomic-answer-1.6.1", "preferences": {}},
            policy_versions=policies,
            idempotency_key="legacy-persisted-state",
        )
        db.add(turn)
        db.flush()
        run = app.runtime.create_in(
            db,
            actor,
            env["ns"],
            env["funding"],
            "chat",
            {
                "turn_id": turn.id,
                "funding_id": env["funding"],
                "model_profile": app.provider.profile_version,
                "answer_contract": "atomic-answer-1.6.1",
                "policy_input_contract": "policy-lineage-1.6.1",
            },
            call_cap=8,
            key="legacy-state:" + turn.id,
        )
        turn.run_id = run.id
        identifier, run_id = turn.id, run.id
        assert not db.scalar(
            select(Idempotency.id).where(
                Idempotency.operation == "conversation.checker_turn", Idempotency.key == identifier
            )
        )
    await app.execute(run_id, "legacy-persisted-worker")
    assert not observed and app.runtime.stage(run_id, ANSWER_COMPLETION_STAGE) is None
    with app.db.read() as db:
        assert db.get(Turn, identifier).status == "completed"
    checker = [c for c in env["model"].calls if c["role"] == "answer_checker"]
    assert checker and all(
        set(call["context"]) == {"question", "proposed_block", "facts"} for call in checker
    )
    before = snapshot(env, identifier)
    calls = len(env["model"].calls)
    await app.conversations.process(identifier)
    assert snapshot(env, identifier) == before and len(env["model"].calls) == calls
