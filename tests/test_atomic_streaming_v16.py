"""Real gateway/Conversation path with an explicit test provider, never paid calls."""

import asyncio

import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.contracts import TurnInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, AnswerBlock, ArtifactDependency, ModelReceipt


async def two_sources(env):
    await publish_fixture(env, "stream-a", "fixtures/a", "a" * 40)
    await publish_fixture(env, "stream-b", "fixtures/b", "b" * 40)


def submit(env):
    app = env["app"]
    conversation = app.conversations.create(env["actor"])
    return app.conversations.submit(
        env["actor"],
        conversation,
        TurnInput(
            question="解释 Tool Calling 返回值",
            expected_revision=1,
            funding_id=env["funding"],
            idempotency_key="atomic-stream",
        ),
    )


@pytest.mark.asyncio
async def test_first_block_committed_before_second_provider_request_and_replay_free(env):
    await two_sources(env)
    app, actor = env["app"], env["actor"]
    turn = submit(env)
    original = env["model"].complete
    second_started, allow_second = asyncio.Event(), asyncio.Event()
    calls = 0

    async def complete(request):
        nonlocal calls
        if request["role"] == "tutor":
            calls += 1
            assert request["context"]["block_contract"]["max_blocks"] == 1
            assert request["schema"]["properties"]["blocks"]["maxItems"] == 1
            if calls == 2:
                second_started.set()
                await allow_second.wait()
        return await original(request)

    env["model"].complete = complete
    task = asyncio.create_task(app.execute(turn["run_id"], "stream-worker"))
    await asyncio.wait_for(second_started.wait(), 10)
    before = app.answers.events(actor, turn["turn_id"])
    assert before["status"] == "running"
    types = [e["type"] for e in before["events"]]
    assert "answer.completed" not in types
    assert (
        types.index("answer.block.validated")
        < types.index("answer.delta")
        < types.index("answer.block.completed")
    )
    validated = [e for e in before["events"] if e["type"] == "answer.block.validated"]
    assert len(validated) == 1
    assert all(
        k in validated[0]
        for k in ("event_id", "stream_id", "occurred_at", "payload_ref", "correlation_id")
    )
    with app.db.read() as db:
        charges = db.get(AgentRun, turn["run_id"]).calls_consumed
    for _ in range(3):
        assert app.answers.events(actor, turn["turn_id"])["events"] == before["events"]
    with app.db.read() as db:
        assert db.get(AgentRun, turn["run_id"]).calls_consumed == charges
    allow_second.set()
    await task
    after = app.answers.events(actor, turn["turn_id"])
    assert after["status"] == "completed"
    assert sum(e["type"] == "answer.block.validated" for e in after["events"]) == 2
    with app.db.read() as db:
        rows = db.scalars(select(AnswerBlock).where(AnswerBlock.turn_id == turn["turn_id"])).all()
        for row in rows:
            assert db.scalar(
                select(ArtifactDependency.id).where(
                    ArtifactDependency.dependent_id == row.id,
                    ArtifactDependency.kind == "states_fact",
                )
            )
        receipts = db.scalars(
            select(ModelReceipt).where(
                ModelReceipt.run_id == turn["run_id"], ModelReceipt.role == "tutor"
            )
        ).all()
        assert {r.stage_key for r in receipts} == {"tutor.v2.block:0", "tutor.v2.block:1"}


@pytest.mark.asyncio
async def test_late_invalid_block_never_leaks_and_partial_answer_interrupts(env):
    await two_sources(env)
    app, actor = env["app"], env["actor"]
    turn = submit(env)
    original = env["model"].complete

    async def complete(request):
        if request["role"] == "tutor" and request["context"]["block_contract"]["ordinal"] == 1:
            raise DomainError("MODEL_RESULT_UNCERTAIN", "not a complete second result")
        return await original(request)

    env["model"].complete = complete
    await app.execute(turn["run_id"], "stream-worker")
    packet = app.answers.events(actor, turn["turn_id"])
    assert packet["status"] == "interrupted"
    assert sum(e["type"] == "answer.block.validated" for e in packet["events"]) == 1
    assert not any(e["type"] == "answer.completed" for e in packet["events"])
    assert any(e["type"] == "answer.interrupted" for e in packet["events"])


@pytest.mark.asyncio
async def test_revoke_during_next_block_stops_new_output(env):
    await two_sources(env)
    app, actor = env["app"], env["actor"]
    turn = submit(env)
    original = env["model"].complete
    from careerforge.services.dependencies import invalidate

    async def complete(request):
        if request["role"] == "tutor" and request["context"]["block_contract"]["ordinal"] == 1:
            publication = request["context"]["evidence_pack"]["knowledge_units"][0][
                "publication_id"
            ]
            with app.db.transaction() as db:
                invalidate(db, actor, publication, "revoked", "stream-test")
        return await original(request)

    env["model"].complete = complete
    await app.execute(turn["run_id"], "stream-worker")
    packet = app.answers.events(actor, turn["turn_id"])
    assert packet["status"] == "interrupted"
    assert not any(e["type"] == "answer.completed" for e in packet["events"])
    with app.db.read() as db:
        assert (
            len(db.scalars(select(AnswerBlock).where(AnswerBlock.turn_id == turn["turn_id"])).all())
            == 1
        )


@pytest.mark.asyncio
async def test_paid_uncommitted_block_is_replayed_without_second_charge(env):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    with app.db.read() as db:
        pack = app.knowledge.publication_pack(db, actor, publication)
    run_id = app.runtime.create(
        actor, env["ns"], env["funding"], "chat", {}, 4, "block-receipt-recovery"
    )
    epoch = app.runtime.claim(run_id, "recover-worker")
    args = (
        actor,
        "解释返回值",
        {"knowledge_units": [pack]},
        {"teaching_skill": {"content": {"explanation_order": "concept_first"}}},
        {},
        (run_id, epoch, "recover-worker"),
    )
    generator = app.answers.iter_tutor(*args)
    first = await anext(generator)
    await generator.aclose()  # Simulated worker death before the caller commits the yield.
    with app.db.read() as db:
        consumed = db.get(AgentRun, run_id).calls_consumed
    generator = app.answers.iter_tutor(*args)
    second = await anext(generator)
    await generator.aclose()
    assert first == second
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).calls_consumed == consumed == 1
        assert len(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run_id)).all()) == 1


@pytest.mark.asyncio
async def test_guard_reloads_fact_text_instead_of_trusting_frozen_payload(env):
    from copy import deepcopy

    from careerforge.contracts import AnswerPart

    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    with app.db.read() as db:
        pack = app.knowledge.publication_pack(db, actor, publication)
    tampered = deepcopy(pack)
    tampered["facts"][0]["text"] = "UNREVIEWED_FROZEN_BODY"
    fact = tampered["facts"][0]
    content = await app.answers.guard_block(
        actor,
        AnswerPart(
            kind="repository_fact",
            text=fact["text"],
            fact_atom_ids=[fact["fact_atom_id"]],
            publication_ids=[publication],
        ),
        {"knowledge_units": [tampered]},
        {"explanation_order": "concept_first"},
        None,
        stage_key="frozen-tamper",
    )
    assert content["text"] == pack["facts"][0]["text"]
    assert content["checked"] == "exact_fallback"


@pytest.mark.asyncio
async def test_revocation_after_last_cursor_gets_new_durable_notice_without_model_calls(env):
    from careerforge.services.dependencies import invalidate

    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    turn = submit(env)
    await app.execute(turn["run_id"], "cursor-worker")
    before = app.answers.events(actor, turn["turn_id"])
    cursor = max(event["seq"] for event in before["events"])
    calls = len(env["model"].calls)
    with app.db.transaction() as db:
        invalidate(db, actor, publication, "revoked", "test late revocation")
    after = app.answers.events(actor, turn["turn_id"], cursor)
    assert after["events"] and all(
        e["type"] == "answer.evidence_invalidated" for e in after["events"]
    )
    assert all(
        e["seq"] > cursor and e["schema_version"] == "answer-event-1.6" for e in after["events"]
    )
    assert app.answers.events(actor, turn["turn_id"], cursor) == after
    assert len(env["model"].calls) == calls
