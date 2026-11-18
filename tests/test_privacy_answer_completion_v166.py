"""Cross-owner completion-state privacy probes using only isolated fixture data."""

import json
from copy import deepcopy

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_privacy_v16 import confirm, service

from careerforge.contracts import TurnInput
from careerforge.contracts_answer_completion_v166 import EVIDENCE_ANSWER_STATE
from careerforge.errors import DomainError
from careerforge.models import (
    AccessGrant,
    AgentRun,
    AnswerBlock,
    ArtifactDependency,
    Conversation,
    ModelReceipt,
    ObjectRow,
    StageArtifact,
    Turn,
)
from careerforge.services.dependencies import depend, register
from careerforge.services.security import create_principal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    ["completed", "after_plan", "after_fact", "after_assessment", "after_suffix", "interrupted"],
)
async def test_cross_owner_evidence_completion_state_does_not_retain_erased_facts(
    env, monkeypatch, phase
):
    app, owner = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "completion-copy-consumer")
        db.add(AccessGrant(namespace_id=env["ns"], principal_id=other.id, role="reader"))
        fact = app.knowledge.publication_pack(db, owner, publication)["facts"][0]
    conversation = app.conversations.create(other.id)
    with app.db.transaction() as db:
        db.get(Conversation, conversation).focus = {"independent_note": "OTHER_NOTE_KEEP"}
    turn_id = app.conversations.submit(
        other.id,
        conversation,
        TurnInput(
            question="解释 Tool Calling 返回值",
            expected_revision=1,
            idempotency_key="completion-private-copy",
        ),
    )["turn_id"]
    calls_before = len(env["model"].calls)
    if phase == "completed":
        await app.conversations.process(turn_id)
    else:
        method = {
            "after_plan": "freeze_evidence_plan",
            "after_fact": "commit_block",
            "after_assessment": "commit",
            "after_suffix": "finish",
            "interrupted": "finish",
        }[phase]
        original = getattr(app.answers, method)

        def crash(*args, **kwargs):
            if phase in {"after_plan", "after_fact"}:
                original(*args, **kwargs)
            raise RuntimeError("synthetic privacy interruption")

        with monkeypatch.context() as patch:
            patch.setattr(app.answers, method, crash)
            with pytest.raises(RuntimeError, match="synthetic privacy interruption"):
                await app.conversations.process(turn_id)
        if phase == "interrupted":
            app.conversations.mark_failed(turn_id, "SYNTHETIC_PRIVACY_INTERRUPTION")
    with app.db.read() as db:
        turn = db.get(Turn, turn_id)
        assert turn.run_id is None and (turn.status == "completed") is (phase == "completed")
        status_before = turn.status
        focus_before = deepcopy(turn.focus)
        state = turn.focus[EVIDENCE_ANSWER_STATE]
        assert fact["text"] in str(state["plan"])
        if phase in {"completed", "after_assessment", "after_suffix", "interrupted"}:
            assert fact["text"] in str(state["assessment"])
            assert state["assessment"]["task_satisfied"] is None
        else:
            assert "assessment" not in state
        manifest_id = state["plan"]["retrieval_manifest_id"]
        blocks_before = list(
            db.scalars(select(AnswerBlock.id).where(AnswerBlock.turn_id == turn_id))
        )
        assert bool(blocks_before) is (phase != "after_plan")
        runs_before = list(db.scalars(select(AgentRun.id)))
    svc = service(env)
    proposal = svc.propose(owner)
    await confirm(svc, owner, proposal)
    with app.db.read() as db:
        turn = db.get(Turn, turn_id)
        assert fact["text"] not in str(turn.focus)
        assert turn.focus[EVIDENCE_ANSWER_STATE] == {}
        assert turn.focus == {**focus_before, EVIDENCE_ANSWER_STATE: {}}
        assert turn.status == status_before
        assert turn.focus["independent_note"] == "OTHER_NOTE_KEEP"
        assert turn.question == "解释 Tool Calling 返回值"
        assert db.get(Conversation, conversation).focus["independent_note"] == "OTHER_NOTE_KEEP"
        assert db.get(ObjectRow, manifest_id).payload == {}
        assert db.get(ObjectRow, manifest_id).state == "revoked"
        facts = list(
            db.scalars(
                select(AnswerBlock).where(
                    AnswerBlock.turn_id == turn_id, AnswerBlock.kind == "repository_fact"
                )
            )
        )
        assert bool(facts) is (phase != "after_plan")
        assert all(block.content == {} for block in facts)
    packet = app.answers.events(other.id, turn_id)
    assert any(e["type"] == "answer.completed" for e in packet["events"]) is (phase == "completed")
    for _ in range(2):
        assert (await confirm(svc, owner, proposal))["state"] == "completed"
        if phase == "completed":
            await app.conversations.process(turn_id)  # Terminal retry stays a no-op.
        else:
            with pytest.raises(DomainError) as raised:
                await app.conversations.process(turn_id)
            assert raised.value.code in {"ANSWER_COMPLETION_BINDING", "ANSWER_INTERRUPTED"}
        with pytest.raises(DomainError) as raised:
            app.answers.finish(other.id, turn_id, manifest_id)
        assert raised.value.code in {"ANSWER_COMPLETION_BINDING", "ANSWER_INTERRUPTED"}
        assert app.answers.events(other.id, turn_id) == packet
    with app.db.read() as db:
        assert list(db.scalars(select(AgentRun.id))) == runs_before
    assert len(env["model"].calls) == calls_before


@pytest.mark.asyncio
async def test_content_dependencies_redact_copies_without_following_shared_provenance(
    env, monkeypatch
):
    app, owner = env["app"], env["actor"]
    source, publication, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        other, namespace, _ = create_principal(db, "mixed-provenance-consumer")
        db.add(AccessGrant(namespace_id=env["ns"], principal_id=other.id, role="reader"))
    conversation = app.conversations.create(other.id)
    turn_id = app.conversations.submit(
        other.id,
        conversation,
        TurnInput(
            question="解释 Tool Calling 返回值", expected_revision=1, idempotency_key="mixed"
        ),
    )["turn_id"]
    independent = {**app.answers.action("question"), "text": "INDEPENDENT_BLOCK_KEEP"}
    tutor = app.answers.tutor

    async def mixed_tutor(*args, **kwargs):
        return [*await tutor(*args, **kwargs), independent]

    calls_before = len(env["model"].calls)
    with monkeypatch.context() as patch:
        patch.setattr(app.answers, "tutor", mixed_tutor)
        await app.conversations.process(turn_id)
    funding = app.runtime.create_funding(other.id, namespace.id, 2)
    run_id = app.runtime.create(
        other.id, namespace.id, funding, "research", {}, 2, "mixed-artifacts"
    )
    with app.db.transaction() as db:
        turn = db.get(Turn, turn_id)
        manifest_id = turn.manifest_id
        blocks = list(
            db.scalars(
                select(AnswerBlock)
                .where(AnswerBlock.turn_id == turn_id)
                .order_by(AnswerBlock.ordinal)
            )
        )
        private_id, independent_id = blocks[0].id, blocks[1].id
        assert blocks[0].content["publication_ids"] == [publication]
        assert blocks[1].content == independent
        dependencies = list(db.scalars(select(ArtifactDependency)))
        edges = {
            (row.dependent_id, row.source_id, row.kind, row.enforcement) for row in dependencies
        }
        assert (private_id, publication, "teaches_from", "blocking") in edges
        assert (private_id, manifest_id, "generated_under", "provenance_only") in edges
        assert (independent_id, manifest_id, "generated_under", "provenance_only") in edges
        assert (manifest_id, publication, "selects_source", "blocking") in edges
        frontier, reached = [publication], {publication}
        while frontier:
            target = frontier.pop()
            for dependent, ancestor, _, enforcement in edges:
                if (
                    dependent == target
                    and enforcement != "provenance_only"
                    and ancestor not in reached
                ):
                    reached.add(ancestor)
                    frontier.append(ancestor)
        assert source in reached  # Publication has a true transitive private-content path.
        print(
            "PRIVACY_GRAPH "
            + json.dumps(
                {
                    "source_id": source,
                    "publication_id": publication,
                    "manifest_id": manifest_id,
                    "private_block_id": private_id,
                    "independent_block_id": independent_id,
                    "edges": [
                        list(edge)
                        for edge in sorted(edges)
                        if edge[0] in reached | {private_id, independent_id, manifest_id}
                    ],
                },
                sort_keys=True,
            )
        )
        for name, block_id, body in (
            ("private", private_id, "PRIVATE_TRANSITIVE_COPY"),
            ("independent", independent_id, "INDEPENDENT_ARTIFACT_KEEP"),
        ):
            payload = {"binding": {"blocks": [{"id": block_id}]}, "body": body}
            db.add(
                StageArtifact(run_id=run_id, stage_key=name, input_hash="d" * 64, payload=payload)
            )
            db.add(
                ModelReceipt(
                    run_id=run_id,
                    stage_key=name,
                    payer_id=other.id,
                    epoch=1,
                    role="tutor",
                    request_hash="e" * 64,
                    status="completed",
                    response=payload,
                    profile_version="fixture",
                )
            )
    svc = service(env)
    await confirm(svc, owner, svc.propose(owner))
    with app.db.read() as db:
        assert db.get(ObjectRow, manifest_id).payload == {}
        assert db.get(AnswerBlock, private_id).content == {}
        assert db.get(AnswerBlock, independent_id).content == independent
        assert db.get(ObjectRow, independent_id).state == "active"
        assert db.get(Turn, turn_id).focus[EVIDENCE_ANSWER_STATE] == {}
        stages = {
            row.stage_key: row.payload
            for row in db.scalars(select(StageArtifact).where(StageArtifact.run_id == run_id))
        }
        receipts = {
            row.stage_key: row.response
            for row in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run_id))
        }
        assert stages["private"] == {} and receipts["private"] is None
        expected = {
            "binding": {"blocks": [{"id": independent_id}]},
            "body": "INDEPENDENT_ARTIFACT_KEEP",
        }
        assert stages["independent"] == expected and receipts["independent"] == expected
    packet = app.answers.events(other.id, turn_id)
    assert any(
        e["type"] == "answer.block.validated" and e["payload"]["content"] == independent
        for e in packet["events"]
    )
    assert len(env["model"].calls) == calls_before


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["plan", "assessment", "continuity_result"])
async def test_nested_completion_state_references_are_scrubbed_without_erasing_other_state(
    env, field
):
    app, owner = env["app"], env["actor"]
    with app.db.transaction() as db:
        source = register(db, owner, env["ns"], "source_set", {"body": "PRIVATE_NESTED_COPY"})
        other, namespace, _ = create_principal(db, "nested-completion-consumer")
        db.add(AccessGrant(namespace_id=env["ns"], principal_id=other.id, role="reader"))
        db.flush()
        manifest = register(
            db, other.id, namespace.id, "selection_manifest", {"source_id": source.id}
        )
        depend(db, other.id, manifest.id, source.id, "constrains_source_path")
    turns = {}
    for label in ("derived", "independent"):
        conversation = app.conversations.create(other.id)
        turns[label] = app.conversations.submit(
            other.id,
            conversation,
            TurnInput(question=label, expected_revision=1, idempotency_key=label),
        )["turn_id"]
    with app.db.transaction() as db:
        derived = db.get(Turn, turns["derived"])
        derived.focus = {
            **derived.focus,
            "other_note": "OTHER_NOTE_KEEP",
            EVIDENCE_ANSWER_STATE: {
                field: {
                    "nested": [
                        {"reference": "manifest=" + manifest.id, "body": "PRIVATE_NESTED_COPY"}
                    ]
                }
            },
        }
        independent = db.get(Turn, turns["independent"])
        independent.focus = {
            **independent.focus,
            EVIDENCE_ANSWER_STATE: {field: {"body": "OTHER_STATE_KEEP"}},
        }
        independent_focus = deepcopy(independent.focus)
    svc = service(env)
    proposal = svc.propose(owner)
    await confirm(svc, owner, proposal)
    await confirm(svc, owner, proposal)
    with app.db.read() as db:
        derived = db.get(Turn, turns["derived"])
        assert derived.focus[EVIDENCE_ANSWER_STATE] == {}
        assert derived.focus["other_note"] == "OTHER_NOTE_KEEP"
        assert derived.question == "derived"
        assert db.get(Turn, turns["independent"]).focus == independent_focus
    with pytest.raises(DomainError) as raised:
        await app.conversations.process(turns["derived"])
    assert raised.value.code == "ANSWER_COMPLETION_BINDING"
    assert env["model"].calls == []


@pytest.mark.asyncio
async def test_unrelated_valid_completion_state_and_public_knowledge_survive_erasure(env):
    app, owner = env["app"], env["actor"]
    private_source, _, _ = await publish_fixture(env)
    public_source, public_publication, _ = await publish_fixture(
        env,
        "public-completion",
        "fixtures/public-completion",
        namespace=env["owner"]["public_namespace_id"],
    )
    with app.db.transaction() as db:
        other, namespace, _ = create_principal(db, "independent-completion-consumer")
        db.add(AccessGrant(namespace_id=env["ns"], principal_id=other.id, role="reader"))
    other_env = {
        **env,
        "actor": other.id,
        "ns": namespace.id,
        "funding": app.runtime.create_funding(other.id, namespace.id, 100),
    }
    own_source, own_publication, _ = await publish_fixture(
        other_env, "independent-completion", "fixtures/independent-completion"
    )
    snapshots = {}
    calls_before = len(env["model"].calls)
    for label, source in (
        ("derived", private_source),
        ("own", own_source),
        ("public", public_source),
    ):
        conversation = app.conversations.create(other.id)
        with app.db.transaction() as db:
            db.get(Conversation, conversation).focus = {
                "project_source_ids": [source],
                "independent_note": "OTHER_NOTE_KEEP",
            }
        turn_id = app.conversations.submit(
            other.id,
            conversation,
            TurnInput(
                question="解释 Tool Calling 返回值", expected_revision=1, idempotency_key=label
            ),
        )["turn_id"]
        await app.conversations.process(turn_id)
        with app.db.read() as db:
            focus = deepcopy(db.get(Turn, turn_id).focus)
            assert focus[EVIDENCE_ANSWER_STATE]["assessment"]["task_satisfied"] is None
        snapshots[label] = (turn_id, focus, app.answers.events(other.id, turn_id))
    svc = service(env)
    await confirm(svc, owner, svc.propose(owner))
    with app.db.read() as db:
        assert db.get(Turn, snapshots["derived"][0]).focus[EVIDENCE_ANSWER_STATE] == {}
        for label in ("own", "public"):
            turn_id, focus, _ = snapshots[label]
            assert db.get(Turn, turn_id).focus == focus
        for publication in (own_publication, public_publication):
            assert app.knowledge.publication_pack(db, other.id, publication)["facts"]
    for label in ("own", "public"):
        turn_id, _, packet = snapshots[label]
        await app.conversations.process(turn_id)
        assert app.answers.events(other.id, turn_id) == packet
    assert len(env["model"].calls) == calls_before
