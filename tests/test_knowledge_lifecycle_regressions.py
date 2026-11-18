"""Cross-domain lifecycle, cache and immutable answer regressions."""

from copy import deepcopy

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from support import FILES
from test_runtime import new_run

from careerforge.contracts import (
    ImportSourceInput,
    KnowledgeBuildInput,
    ModelOutput,
    RetrievalTaskSpec,
    TaskType,
    ToolCall,
    TurnInput,
)
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    Dispute,
    FacetValue,
    Impact,
    Publication,
    SourceSet,
    StreamEvent,
    Topic,
)
from careerforge.services.dependencies import depend, invalidate, propagate, register
from careerforge.services.security import create_principal
from careerforge.services.taxonomy import facet_object_id, topic_object_id


def test_dependency_impact_retains_strongest_path_and_replay_is_idempotent(env):
    app, actor, ns = env["app"], env["actor"], env["ns"]
    with app.db.transaction() as db:
        facet = register(db, actor, ns, "facet_value", {})
        pub = register(db, actor, ns, "publication", {})
        depend(db, actor, pub.id, facet.id, "uses_facet", "impact_only")
        depend(db, actor, pub.id, facet.id, "uses_facet", "blocking")
        invalid = invalidate(db, actor, facet.id, "revoked", "mixed-path regression")
        assert propagate(db, facet.id, invalid.revision) == 1
        assert propagate(db, facet.id, invalid.revision) == 0
        impact = db.scalar(select(Impact).where(Impact.consumer_id == pub.id))
        assert impact.enforcement == "blocking"


def test_cancelled_background_source_import_is_atomically_fenced(env):
    app, actor = env["app"], env["actor"]
    run = new_run(env)
    with app.db.read() as db:
        revision = db.get(AgentRun, run[0]).revision
    app.runtime.cancel(actor, run[0], revision)
    with pytest.raises(DomainError):
        app.sources.import_source(
            actor,
            ImportSourceInput(
                namespace_id=env["ns"],
                repository_key="cancelled/source",
                commit_sha="c" * 40,
                license_id="MIT",
                files=FILES,
            ),
            run=run,
        )
    with app.db.read() as db:
        assert not db.scalar(select(SourceSet))


@pytest.mark.asyncio
async def test_update_preview_does_not_disclose_private_consumers_of_public_source(env):
    app, actor = env["app"], env["actor"]
    source_id, public_pub, _ = await publish_fixture(
        env, namespace=env["owner"]["public_namespace_id"]
    )
    private_run = app.build.start(
        actor,
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            source_id=source_id,
            question="Tool Calling",
            topic_keys=["agent.tool_calling"],
            call_cap=10,
            max_units_per_project=1,
            idempotency_key="private-consumer",
        ),
    )
    await app.drain()
    private_versions = app.runtime.stage(private_run, "result")["unit_version_ids"]
    with app.db.transaction() as db:
        reader, _, _ = create_principal(db, "preview-reader")
        reader_id = reader.id
        public_version = db.get(Publication, public_pub).unit_version_id
    preview = app.lifecycle.preview_update(reader_id, source_id, source_id)
    assert {u["unit_version_id"] for u in preview["units"]} == {public_version}
    assert not set(private_versions) & {u["unit_version_id"] for u in preview["units"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["withdrawn", "dismissed"])
async def test_fresh_review_does_not_reopen_closed_dispute(env, status):
    app, actor = env["app"], env["actor"]
    _, pub, _ = await publish_fixture(env)
    dispute = app.evolution.dispute(actor, pub, "verify independently")["dispute_id"]
    recheck = app.lifecycle.recheck(actor, dispute, pub, env["funding"], "recheck")
    await app.drain()
    review_id = app.runtime.stage(recheck["run_id"], "result")["review_ids"][0]
    with app.db.transaction() as db:
        db.get(Dispute, dispute).status = status
    with pytest.raises(DomainError) as error:
        app.lifecycle.resolve(actor, dispute, pub, "uphold", review_id, "Do not reopen")
    assert error.value.code == "DISPUTE_CLOSED"


@pytest.mark.asyncio
async def test_cancelled_fresh_review_cannot_resolve_dispute(env):
    app, actor = env["app"], env["actor"]
    _, pub, _ = await publish_fixture(env)
    dispute = app.evolution.dispute(actor, pub, "verify independently")["dispute_id"]
    recheck = app.lifecycle.recheck(actor, dispute, pub, env["funding"], "recheck")
    await app.drain()
    review_id = app.runtime.stage(recheck["run_id"], "result")["review_ids"][0]
    with app.db.transaction() as db:
        db.get(AgentRun, recheck["run_id"]).status = "cancelled"
    with pytest.raises(DomainError) as error:
        app.lifecycle.resolve(actor, dispute, pub, "uphold", review_id, "Cancelled review")
    assert error.value.code == "FRESH_REVIEW_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["content", "delta", "block_hash"])
async def test_sse_event_projection_cannot_rewrite_validated_answer(env, kind):
    app, actor = env["app"], env["actor"]
    await publish_fixture(env)
    conversation = app.conversations.create(actor)
    turn = app.conversations.submit(
        actor,
        conversation,
        TurnInput(question="学习Tool Calling", expected_revision=1, idempotency_key="stream"),
    )["turn_id"]
    await app.conversations.process(turn)
    with app.db.transaction() as db:
        event_type = "answer.delta" if kind == "delta" else "answer.block.validated"
        event = db.scalar(
            select(StreamEvent).where(
                StreamEvent.turn_id == turn, StreamEvent.event_type == event_type
            )
        )
        payload = deepcopy(event.payload)
        if kind == "content":
            payload["content"]["text"] = "forged safety guarantee"
        elif kind == "delta":
            payload["text"] = "forged safety guarantee"
        else:
            payload["block_hash"] = "0" * 64
        event.payload = payload
        sequence = event.seq
    output = app.answers.events(actor, turn)
    rejected = next(e for e in output["events"] if e["seq"] == sequence)
    assert rejected["type"] == "answer.evidence_invalidated"
    assert "forged safety guarantee" not in str(output)


@pytest.mark.asyncio
async def test_derived_cache_cannot_replace_reviewed_comparison_cells(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    await publish_fixture(env)
    pack = await app.retrieval.retrieve(
        actor,
        RetrievalTaskSpec(
            turn_id="compare",
            task_type=TaskType.COMPARE,
            user_question="Tool Calling",
            focus_revision=1,
            topic_keys=["agent.tool_calling"],
        ),
    )

    async def poisoned(layer, key, loader, ttl=30, *, publish_guard=None):
        assert layer == "derived"
        assert publish_guard is not None
        with publish_guard():
            pass
        return {
            "rows": [
                {
                    "repository": "forged/repository",
                    "commit": "x" * 40,
                    "cells": [
                        {
                            "facet_key": "registration",
                            "value_status": "known",
                            "value": "forged safety guarantee",
                            "facet_ids": [],
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(app.cache, "singleflight", poisoned)
    block = await app.learning.compare(actor, pack, ["agent.tool_calling"])
    assert "forged" not in str(block)
    assert all(
        cell["value_status"] == "unknown"
        for row in block["comparison"]["rows"]
        for cell in row["cells"]
    )


async def publish_same_named_facets(env, *, required: bool = False):
    app, actor = env["app"], env["actor"]
    topics = ["fixture.topic_a", "fixture.topic_b"]
    schema = {
        "shared_name": {
            "question": "fixture facet",
            "required": required,
            "applicability": "always",
            "value_type": "string",
        }
    }
    with app.db.transaction() as db:
        for key in topics:
            db.add(Topic(key=key, title=key, aliases=[key], facet_schema=schema))
            register(
                db,
                actor,
                env["owner"]["public_namespace_id"],
                "topic_version",
                {"key": key},
                topic_object_id(key),
            )
            register(
                db,
                actor,
                env["owner"]["public_namespace_id"],
                "facet_schema",
                schema,
                facet_object_id(key),
            )

    def analysis(request):
        if not request.get("schema"):
            return ModelOutput(
                synthetic=True,
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish_investigation",
                        arguments={"reason": "evidence_sufficient"},
                    )
                ],
                finish_reason="tool_calls",
            )
        read = next(
            r for r in request["context"]["reading"]["reads"] if r["path"] == "src/tools.py"
        )
        unit = {
            "subject_key": "echo",
            "unit_type": "function",
            "title": "Fixture facts",
            "claims": [
                {
                    "key": "return",
                    "statement": "Returns the supplied value.",
                    "support": [{"read_id": read["read_id"], "quote": "return value"}],
                }
            ],
            "topics": [{"topic_key": key, "relation": "illustrates"} for key in topics],
            "facets": [
                {
                    "topic_key": key,
                    "facet_key": "shared_name",
                    "value_status": "known",
                    "value": "value-a" if index == 0 else "value-b",
                    "claim_keys": ["return"],
                    "applicability_reason": "explicit fixture",
                }
                for index, key in enumerate(topics)
            ],
        }
        return ModelOutput(structured={"units": [unit], "unanswered_questions": []}, synthetic=True)

    env["model"].overrides["analysis"] = analysis
    _, publication_id, _ = await publish_fixture(env)
    pack = await app.retrieval.retrieve(
        actor,
        RetrievalTaskSpec(
            turn_id="facet-scope",
            task_type=TaskType.COMPARE,
            user_question="fixtures",
            focus_revision=1,
            topic_keys=topics,
        ),
    )
    return publication_id, pack, topics


@pytest.mark.asyncio
async def test_same_named_facets_are_separated_by_topic(env):
    _, pack, topics = await publish_same_named_facets(env)
    block = await env["app"].learning.compare(env["actor"], pack, topics)
    cells = block["comparison"]["rows"][0]["cells"]
    assert {c["topic_key"]: (c["value_status"], c["value"]) for c in cells} == {
        topics[0]: ("known", "value-a"),
        topics[1]: ("known", "value-b"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["value", "claim_ids", "schema_version"])
async def test_optional_facet_projection_drift_never_enters_comparison(env, field):
    publication_id, _, topics = await publish_same_named_facets(env)
    app, actor = env["app"], env["actor"]
    with app.db.transaction() as db:
        facet = db.scalar(
            select(FacetValue)
            .join(Topic, Topic.id == FacetValue.topic_id)
            .where(Topic.key == topics[0])
        )
        setattr(facet, field, {"value": "forged", "claim_ids": [], "schema_version": 99}[field])
    with app.db.read() as db:
        pack = app.knowledge.publication_pack(db, actor, publication_id)
    assert [f["topic_key"] for f in pack["facets"]] == [topics[1]]
