"""Actual HTTP data-plane fences + complete persisted SSE envelopes."""

import json

import httpx
import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.api import create_app
from careerforge.cli.stream import StreamProtocolError, parse_events
from careerforge.contracts import TurnInput
from careerforge.models import ResearchNode
from careerforge.models_privacy_ext import PrivacyState


def envelope(seq=1, kind="answer.delta"):
    return {
        "stream_id": "stream",
        "event_id": "event",
        "seq": seq,
        "type": kind,
        "occurred_at": "2026-11-02T11:00:00+00:00",
        "schema_version": "answer-event-1.6",
        "payload_ref": "block",
        "correlation_id": "run",
        "payload": {"text": "verified"},
    }


def test_sse_decoder_preserves_metadata_and_rejects_forged_identity():
    value = envelope()
    event = list(parse_events(["id: 1", "event: answer.delta", "data: " + json.dumps(value), ""]))[
        0
    ]
    assert event.payload == {"text": "verified"}
    assert event.metadata["event_id"] == "event" and event.sequence == 1
    for altered in [
        dict(value, seq=2),
        dict(value, type="other"),
        dict(value, schema_version="future"),
        dict(value, occurred_at="invalid"),
        {k: v for k, v in value.items() if k != "event_id"},
    ]:
        with pytest.raises(StreamProtocolError):
            list(parse_events(["id: 1", "event: answer.delta", "data: " + json.dumps(altered), ""]))


async def test_real_http_sse_has_full_persisted_envelope_and_replay_no_model_calls(env):
    source, _, _ = await publish_fixture(env)
    app, actor = env["app"], env["actor"]
    conversation = app.conversations.create(actor)
    turn = app.conversations.submit(
        actor,
        conversation,
        TurnInput(
            question="解释 echo value",
            expected_revision=1,
            funding_id=env["funding"],
            idempotency_key="wire-envelope",
        ),
    )
    await app.drain()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        response = await client.get("/api/v1/turns/" + turn["turn_id"] + "/events")
        assert response.status_code == 200, response.text
        events = list(parse_events(response.text.splitlines()))
        assert events and all(event.metadata for event in events)
        assert all(event.metadata["stream_id"] == turn["turn_id"] for event in events)
        assert any(event.name == "answer.delta" for event in events)
        before = len(env["model"].calls)
        replay = await client.get(
            "/api/v1/turns/" + turn["turn_id"] + "/events",
            headers={"Last-Event-ID": str(events[-1].sequence)},
        )
        assert replay.status_code == 200 and len(env["model"].calls) == before
        assert list(parse_events(replay.text.splitlines())) == []


async def test_http_personal_runs_events_tree_and_lists_block_during_phase_one(env):
    app, actor = env["app"], env["actor"]
    run = app.runtime.create(actor, env["ns"], env["funding"], "research", {}, 2, "private-window")
    epoch = app.runtime.claim(run, "worker")
    app.runtime.save_stage(run, epoch, "worker", "result", {"private_body": "MUST_NOT_LEAK"})
    with app.db.transaction() as db:
        db.add(
            ResearchNode(
                run_id=run,
                parent_id=None,
                depth=0,
                question="MUST_NOT_LEAK",
                node_key="phase-one",
                payload={"private_body": "MUST_NOT_LEAK"},
            )
        )
        db.get(PrivacyState, actor).blocked = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        for path in [
            "/api/v1/runs",
            "/api/v1/runs/" + run,
            "/api/v1/runs/" + run + "/events",
            "/api/v1/runs/" + run + "/research-tree",
            "/api/v1/knowledge/gaps",
            "/api/v1/knowledge/disputes",
        ]:
            result = await client.get(path)
            assert result.status_code == 409, (path, result.text)
            assert result.json()["error"]["code"] == "PRIVACY_ERASURE_PENDING"
            assert "MUST_NOT_LEAK" not in result.text
        assert (await client.get("/api/v1/privacy/policy")).status_code == 200


async def test_foreign_source_erasure_blocks_consumer_run_body_in_same_read_transaction(env):
    from test_build_v16_integration import foreign_investigation

    owner, source, run, _ = foreign_investigation(env)
    with env["app"].db.transaction() as db:
        db.get(PrivacyState, owner).blocked = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env["app"])),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        for suffix in ["", "/events", "/research-tree"]:
            response = await client.get("/api/v1/runs/" + run + suffix)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "PRIVACY_ERASURE_PENDING"
    with env["app"].db.read() as db:
        assert (
            db.scalar(select(PrivacyState.blocked).where(PrivacyState.principal_id == env["actor"]))
            is False
        )
