"""Privacy control-plane tests use explicit synthetic fixtures and no external API."""

import asyncio
from datetime import timedelta

import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.contracts import ModelOutput
from careerforge.contracts_privacy_ext import RetentionHoldInput
from careerforge.db import clock
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    FundingAgreement,
    ModelReceipt,
    ObjectRow,
    SourceSet,
    Workspace,
)
from careerforge.models_privacy_ext import PrivacyDeletion, PrivacyState
from careerforge.services.privacy import PrivacyService, assert_personal_access
from careerforge.services.security import create_principal, private_namespace


def service(env):
    return PrivacyService(env["app"])


async def confirm(svc, actor, proposal):
    return await svc.confirm(
        actor, proposal["proposal_id"], proposal["confirmation_nonce"], proposal["plan_hash"], True
    )


@pytest.mark.asyncio
async def test_own_export_erasure_private_only_and_fresh_namespace(env):
    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    public_source, public_pub, _ = await publish_fixture(
        env, "public", "fixtures/public", "b" * 40, env["owner"]["public_namespace_id"]
    )
    workspace = app.coding.create_workspace(actor, {"secret.py": "PERSONAL_SECRET"})
    with app.db.transaction() as db:
        other, ns, _ = create_principal(db, "other-private-person")
    other_workspace = app.coding.create_workspace(other.id, {"private.py": "OTHER_SECRET"})
    svc = service(env)
    export = svc.export(actor)
    assert "PERSONAL_SECRET" in str(export) and "OTHER_SECRET" not in str(export)
    assert "token_hash" not in str(export)
    await app.cache.put(
        "base", "personal", {"privacy_owner_id": actor, "secret": "PERSONAL_SECRET"}
    )
    await app.cache.put("base", "other", {"privacy_owner_id": other.id, "secret": "OTHER_SECRET"})
    with app.db.transaction() as db:
        db.get(FundingAgreement, env["funding"]).allowed_source_ids = [source]
    proposal = svc.propose(actor)
    result = await confirm(svc, actor, proposal)
    assert result["state"] == "completed"
    assert (await confirm(svc, actor, proposal))["result"] == result["result"]
    with app.db.read() as db:
        assert db.get(SourceSet, source).files == {}
        assert db.get(ObjectRow, source).state == "revoked"
        assert db.get(SourceSet, public_source).files
        assert db.get(ObjectRow, public_pub).state == "active"
        assert (
            db.get(Workspace, workspace["workspace_id"]).files == {}
            and db.get(Workspace, workspace["workspace_id"]).sealed
        )
        assert db.get(Workspace, other_workspace["workspace_id"]).files == {
            "private.py": "OTHER_SECRET"
        }
        assert db.get(FundingAgreement, env["funding"]).calls_held == 0
        assert db.get(FundingAgreement, env["funding"]).allowed_source_ids == [source]
        assert db.get(FundingAgreement, env["funding"]).status == "revoked"
        assert private_namespace(db, actor).id == result["result"]["new_namespace_id"]
        assert assert_personal_access(db, actor) == 1
    assert await app.cache.get("base", "personal") is None
    assert await app.cache.get("base", "other") is not None
    app.conversations.create(actor)  # new namespace can bootstrap clean policies
    assert "PERSONAL_SECRET" not in str(svc.export(actor))


@pytest.mark.asyncio
async def test_confirmation_scope_owner_nonce_expiry_and_hold(env):
    app, actor, svc = env["app"], env["actor"], service(env)
    proposal = svc.propose(actor)
    with pytest.raises(DomainError, match="确认"):
        await svc.confirm(actor, proposal["proposal_id"], "x" * 32, proposal["plan_hash"], True)
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "privacy-other")
    with pytest.raises(DomainError) as exc:
        svc.status(other.id, proposal["proposal_id"])
    assert exc.value.code == "NOT_FOUND"
    with app.db.read() as db:
        expires = clock(db) + timedelta(days=1)
    hold = svc.create_hold(
        actor, RetentionHoldInput(principal_id=actor, reason_code="legal_hold", expires_at=expires)
    )
    with pytest.raises(DomainError) as exc:
        await confirm(svc, actor, proposal)
    assert exc.value.code == "PRIVACY_RETENTION_HOLD"
    assert svc.propose(actor)["plan"]["holds"]
    svc.release_hold(actor, hold["hold_id"])
    with app.db.transaction() as db:
        db.get(PrivacyDeletion, proposal["proposal_id"]).expires_at = clock(db) - timedelta(
            seconds=1
        )
    with pytest.raises(DomainError) as exc:
        await confirm(svc, actor, proposal)
    assert exc.value.code == "PRIVACY_PROPOSAL_EXPIRED"
    with app.db.read() as db:
        assert not db.get(PrivacyState, actor).blocked


@pytest.mark.asyncio
async def test_inflight_paid_body_cannot_reappear_after_erasure(env):
    app, actor, svc = env["app"], env["actor"], service(env)
    run_id = app.runtime.create(actor, env["ns"], env["funding"], "chat", {}, 3, "privacy-inflight")
    epoch = app.runtime.claim(run_id, "privacy-worker")
    entered, finish = asyncio.Event(), asyncio.Event()

    async def complete(request):
        entered.set()
        await finish.wait()
        return ModelOutput(text="LATE_PRIVATE_BODY", usage={"completion_tokens": 4}, synthetic=True)

    env["model"].complete = complete
    pending = asyncio.create_task(
        app.gateway.call(
            run_id,
            epoch,
            "privacy-worker",
            "private-request",
            "tutor",
            "",
            {"secret": "OLD_PRIVATE_BODY"},
            data_classes=["learning_state"],
        )
    )
    await entered.wait()
    result = await confirm(svc, actor, svc.propose(actor))
    assert result["state"] == "completed"
    finish.set()
    with pytest.raises(DomainError) as exc:
        await pending
    assert exc.value.code == "PRIVACY_GENERATION_CHANGED"
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run_id))
        assert receipt.response is None and receipt.status == "redacted"
        assert receipt.usage == {"completion_tokens": 4}
        assert db.get(AgentRun, run_id).status == "cancelled"
        funding = db.get(FundingAgreement, env["funding"])
        assert funding.calls_consumed == 1 and funding.calls_held == 0


@pytest.mark.asyncio
async def test_cache_failure_keeps_fence_and_retry_does_not_duplicate_namespace(env, monkeypatch):
    app, actor, svc = env["app"], env["actor"], service(env)
    app.coding.create_workspace(actor, {"private.py": "DELETE_ME"})
    proposal = svc.propose(actor)
    original = svc._purge_cache

    async def broken(ids):
        raise DomainError("CACHE_OFFLINE", "test failure")

    monkeypatch.setattr(svc, "_purge_cache", broken)
    with pytest.raises(DomainError):
        await confirm(svc, actor, proposal)
    with app.db.read() as db:
        state = db.get(PrivacyState, actor)
        assert state.blocked and state.generation == 1
    with pytest.raises(DomainError) as exc:
        svc.export(actor)
    assert exc.value.code == "PRIVACY_ERASURE_PENDING"
    assert svc.status(actor, proposal["proposal_id"])["state"] == "purging_cache"
    monkeypatch.setattr(svc, "_purge_cache", original)
    assert (await confirm(svc, actor, proposal))["state"] == "completed"
    with app.db.read() as db:
        assert db.get(PrivacyState, actor).generation == 1


@pytest.mark.asyncio
async def test_legacy_body_multiple_rows_erased_without_losing_hash_audit(env):
    from careerforge.models_compat import LegacyImportBatch, LegacyImportRecord
    from careerforge.util import fingerprint

    app, actor, svc = env["app"], env["actor"], service(env)
    with app.db.transaction() as db:
        batch = LegacyImportBatch(
            principal_id=actor,
            namespace_id=env["ns"],
            baseline_sha="a" * 40,
            legacy_owner_id="legacy-personal-identity",
            bundle_hash="b" * 64,
        )
        db.add(batch)
        db.flush()
        for index in range(2):
            payload = {"body": f"PRIVATE_LEGACY_{index}"}
            db.add(
                LegacyImportRecord(
                    batch_id=batch.id,
                    table_name="study_artifacts",
                    legacy_id=f"old-{index}",
                    payload=payload,
                    content_hash=fingerprint(payload),
                )
            )
    assert "PRIVATE_LEGACY_1" in str(svc.export(actor))
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        row = db.get(LegacyImportBatch, batch.id)
        assert row.legacy_owner_id == "" and row.status == "erased"
        records = db.scalars(
            select(LegacyImportRecord).where(LegacyImportRecord.batch_id == batch.id)
        ).all()
        assert len(records) == 2 and all(
            r.payload == {} and len(r.content_hash) == 64 for r in records
        )
        assert len({r.legacy_id for r in records}) == 2


@pytest.mark.asyncio
async def test_pending_erasure_stops_other_grantee_run_in_owner_private_namespace(env):
    from careerforge.models import AccessGrant

    app, actor, svc = env["app"], env["actor"], service(env)
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "privacy-grantee")
        db.add(AccessGrant(namespace_id=env["ns"], principal_id=other.id, role="writer"))
    grant_funding = app.runtime.create_funding(other.id, env["ns"], 20)
    identifier = app.runtime.create(
        other.id,
        env["ns"],
        grant_funding,
        "research",
        {"private": "GRANTED_BODY"},
        4,
        "grantee-private-run",
    )
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        run = db.get(AgentRun, identifier)
        assert run.status == "cancelled" and run.input_manifest == {}
        assert db.get(FundingAgreement, grant_funding).calls_held == 0
        assert not db.scalar(select(AccessGrant.active).where(AccessGrant.principal_id == other.id))


def test_export_never_bypasses_other_private_dependency_or_exposes_raw_provider_tokens(env):
    from careerforge.models import AnswerBlock, Conversation, StreamEvent, Turn
    from careerforge.services.dependencies import register
    from careerforge.util import fingerprint

    app, actor, svc = env["app"], env["actor"], service(env)
    with app.db.transaction() as db:
        other, ns, _ = create_principal(db, "export-private-source-owner")
        source = register(db, other.id, ns.id, "source_set", {"secret": "OTHER_PRIVATE_BODY"})
        # An owned historical copy may survive a revoked grant. It is not a new right
        # to export another namespace's source body.
        conv = Conversation(principal_id=actor, namespace_id=env["ns"])
        db.add(conv)
        db.flush()
        turn = Turn(
            conversation_id=conv.id,
            question="mine",
            focus_revision=1,
            focus={},
            policy_versions={},
            idempotency_key="export-historical",
        )
        db.add(turn)
        db.flush()
        content = {"text": "OTHER_PRIVATE_BODY", "source_id": source.id}
        block = register(db, actor, env["ns"], "answer_block", content)
        db.add(
            AnswerBlock(
                id=block.id,
                turn_id=turn.id,
                ordinal=0,
                kind="raw_source_observation",
                content=content,
                content_hash=fingerprint(content),
            )
        )
        db.add(
            StreamEvent(
                turn_id=turn.id, seq=1, event_type="answer.block.validated", payload=content
            )
        )
    exported = svc.export(actor)
    assert "OTHER_PRIVATE_BODY" not in str(exported)
    assert "references_other_private_material" in str(exported)


@pytest.mark.asyncio
async def test_cross_owner_generated_copies_are_redacted_but_other_work_survives(env):
    from careerforge.contracts import IntentDecision, TurnInput
    from careerforge.contracts_answer_completion_v166 import EVIDENCE_ANSWER_STATE
    from careerforge.models import AccessGrant, AnswerBlock, StageArtifact, StreamEvent, Turn

    app, actor, svc = env["app"], env["actor"], service(env)
    source, pub, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        other, ns, _ = create_principal(db, "copy-consumer")
        db.add(AccessGrant(namespace_id=env["ns"], principal_id=other.id, role="reader"))
    conv = app.conversations.create(other.id)
    turn = app.conversations.submit(
        other.id,
        conv,
        TurnInput(
            question="OTHER_QUESTION_KEEP", expected_revision=1, idempotency_key="private-copy"
        ),
    )["turn_id"]
    with app.db.read() as db:
        unit = app.knowledge.publication_pack(db, other.id, pub)
    fact = unit["facts"][0]
    private = {
        "kind": "repository_fact",
        "text": fact["text"],
        "publication_ids": [pub],
        "fact_atom_ids": [fact["fact_atom_id"]],
        "conditions": fact["conditions"],
        "checked": "exact",
    }
    unrelated = {**app.answers.action("question"), "text": "OTHER_BLOCK_KEEP"}

    # Explicit synthetic routing/rendering, as in the original raw-block
    # fixture. Real orchestration now freezes and assesses this no-run answer;
    # the arbitrary question sentinel does not imply semantic completeness.
    def fixture_intent(*args, **kwargs):
        return IntentDecision(
            interpreted_intent="explain_implementation",
            topic_keys=["agent.tool_calling"],
            confidence=1,
            proposed_next_action="retrieve_and_tutor",
            rationale_codes=["synthetic_privacy_fixture"],
        )

    async def fixture_tutor(*args, **kwargs):
        return [private, unrelated]

    calls = len(env["model"].calls)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("careerforge.services.orchestration.rule_intent", fixture_intent)
        patch.setattr(app.answers, "tutor", fixture_tutor)
        await app.conversations.process(turn)
    with app.db.read() as db:
        delivered = db.get(Turn, turn)
        assert delivered.run_id is None and delivered.status == "completed"
        assert delivered.focus[EVIDENCE_ANSWER_STATE]["assessment"]["task_satisfied"] is None
    assert len(env["model"].calls) == calls
    funding = app.runtime.create_funding(other.id, ns.id, 10)
    run = app.runtime.create(
        other.id, ns.id, funding, "research", {"question": "OTHER_RUN_KEEP"}, 2, "other-receipts"
    )
    with app.db.transaction() as db:
        db.add(
            StageArtifact(
                run_id=run,
                stage_key="private-copy",
                input_hash="a" * 64,
                payload={"publication_id": pub, "body": "PRIVATE_STAGE_BODY"},
            )
        )
        db.add(
            StageArtifact(
                run_id=run,
                stage_key="unrelated",
                input_hash="b" * 64,
                payload={"body": "OTHER_STAGE_KEEP"},
            )
        )
        for key, response in (
            (
                "private-copy",
                {"text": "PRIVATE_PROVIDER_BODY", "structured": {"publication_id": pub}},
            ),
            ("unrelated", {"text": "OTHER_RECEIPT_KEEP"}),
        ):
            db.add(
                ModelReceipt(
                    run_id=run,
                    stage_key=key,
                    payer_id=other.id,
                    epoch=1,
                    role="tutor",
                    request_hash="c" * 64,
                    status="completed",
                    response=response,
                    profile_version="fixture",
                )
            )
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        blocks = db.scalars(
            select(AnswerBlock).where(AnswerBlock.turn_id == turn).order_by(AnswerBlock.ordinal)
        ).all()
        assert blocks[0].content == {} and blocks[1].content["text"] == "OTHER_BLOCK_KEEP"
        assert db.get(Turn, turn).question == "OTHER_QUESTION_KEEP"
        assert db.get(AgentRun, run).input_manifest == {"question": "OTHER_RUN_KEEP"}
        assert (
            db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == run, StageArtifact.stage_key == "private-copy"
                )
            ).payload
            == {}
        )
        assert db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run, StageArtifact.stage_key == "unrelated"
            )
        ).payload == {"body": "OTHER_STAGE_KEEP"}
        assert (
            db.scalar(
                select(ModelReceipt).where(
                    ModelReceipt.run_id == run, ModelReceipt.stage_key == "private-copy"
                )
            ).response
            is None
        )
        assert db.scalar(
            select(ModelReceipt).where(
                ModelReceipt.run_id == run, ModelReceipt.stage_key == "unrelated"
            )
        ).response == {"text": "OTHER_RECEIPT_KEEP"}
        assert all(
            fact["text"] not in str(e.payload)
            for e in db.scalars(select(StreamEvent).where(StreamEvent.turn_id == turn))
        )
    packet = app.answers.events(other.id, turn)
    assert any(e["type"] == "answer.evidence_invalidated" for e in packet["events"])
    assert any(
        e["type"] == "answer.block.validated"
        and e["payload"]["content"]["text"] == "OTHER_BLOCK_KEEP"
        for e in packet["events"]
    )


@pytest.mark.asyncio
async def test_explicit_transitive_dependency_fences_late_other_owner_response(env):
    from careerforge.models import AccessGrant
    from careerforge.services.dependencies import depend, register

    app, actor, svc = env["app"], env["actor"], service(env)
    source, pub, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        other, ns, _ = create_principal(db, "late-source-consumer")
        db.add(AccessGrant(namespace_id=env["ns"], principal_id=other.id, role="reader"))
        db.flush()
        root = register(db, other.id, ns.id, "answer_block", {"text": "prior-owned-rendering"})
        depend(db, other.id, root.id, pub, "teaches_from")
    funding = app.runtime.create_funding(other.id, ns.id, 10)
    run = app.runtime.create(other.id, ns.id, funding, "research", {}, 2, "late-other-source")
    epoch = app.runtime.claim(run, "source-worker")
    entered, release = asyncio.Event(), asyncio.Event()

    async def complete(request):
        entered.set()
        await release.wait()
        return ModelOutput(text="OTHER_OWNER_LATE_BODY", usage={"completion_tokens": 3})

    env["model"].complete = complete
    pending = asyncio.create_task(
        app.gateway.call(
            run,
            epoch,
            "source-worker",
            "private-copy",
            "tutor",
            "",
            {},
            data_classes=["private_source"],
            access_dependencies=[root.id],
        )
    )
    await entered.wait()
    await confirm(svc, actor, svc.propose(actor))
    release.set()
    with pytest.raises(DomainError):
        await pending
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run))
        assert receipt.status == "redacted" and receipt.response is None
        assert receipt.usage == {"completion_tokens": 3}


@pytest.mark.asyncio
async def test_private_taxonomy_projection_and_maintenance_are_erased(env):
    from test_taxonomy_governance_v16 import approve, definition, evidence_fixture, propose

    from careerforge.models import Topic
    from careerforge.models_governance import (
        MaintenanceOccurrence,
        MaintenanceSchedule,
        TaxonomyProposal,
        TaxonomyRelease,
        TopicGovernance,
    )

    app, actor, svc = env["app"], env["actor"], service(env)
    source, evidence = await evidence_fixture(env)
    governance, proposal = propose(
        env, evidence, [definition("erase.private", alias="PRIVATE_ALIAS_ERASE")]
    )
    approve(env, governance, proposal)
    with app.db.transaction() as db:
        topic_id = db.scalar(
            select(Topic.id).where(Topic.key == proposal["payload"]["definitions"][0]["key"])
        )
        schedule = MaintenanceSchedule(
            namespace_id=env["ns"],
            principal_id=actor,
            source_id=source,
            interval_seconds=3600,
            enabled=True,
            allow_model_calls=False,
            next_due_at=clock(db),
        )
        db.add(schedule)
        db.flush()
        occurrence = MaintenanceOccurrence(
            schedule_id=schedule.id, due_at=clock(db), status="planned", reason="PRIVATE_REASON"
        )
        db.add(occurrence)
        db.flush()
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        topic = db.get(Topic, topic_id)
        assert topic.title == "" and topic.aliases == [] and topic.facet_schema == {}
        assert db.get(TopicGovernance, topic_id).status == "retired"
        assert db.get(TaxonomyProposal, proposal["proposal_id"]).payload == {}
        release = db.scalar(
            select(TaxonomyRelease).where(TaxonomyRelease.proposal_id == proposal["proposal_id"])
        )
        assert release.manifest == {}
        assert not db.get(MaintenanceSchedule, schedule.id).enabled
        assert db.get(MaintenanceOccurrence, occurrence.id).status == "cancelled"
    assert "PRIVATE_ALIAS_ERASE" not in str(svc.export(actor))


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["inflight", "completed"])
async def test_optional_private_topic_body_is_frozen_and_receipt_witness_is_erasable(env, timing):
    from support import FILES
    from test_taxonomy_governance_v16 import (
        approve,
        build,
        customize_analysis,
        definition,
        evidence_fixture,
        propose,
    )

    from careerforge.contracts import ImportSourceInput
    from careerforge.models import AccessGrant, StageArtifact

    app, actor = env["app"], env["actor"]
    _, evidence = await evidence_fixture(env)
    governance, proposal = propose(
        env, evidence, [definition("optional.secret", alias="PRIVATE_OPTIONAL_ALIAS")]
    )
    approve(env, governance, proposal)
    topic_key = proposal["payload"]["definitions"][0]["key"]
    with app.db.transaction() as db:
        other, namespace, _ = create_principal(db, "optional-topic-consumer")
        db.add(AccessGrant(namespace_id=env["ns"], principal_id=other.id, role="reader"))
    funding = app.runtime.create_funding(other.id, namespace.id, 100)
    other_env = {**env, "actor": other.id, "ns": namespace.id, "funding": funding}
    source = app.sources.import_source(
        other.id,
        ImportSourceInput(
            namespace_id=namespace.id,
            repository_key="fixtures/consumer",
            commit_sha="b" * 40,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )
    customize_analysis(env, topic_key, facet_key=None)
    build_run = build(other_env, source, topic_key, key="private-topic-consumer-build")
    await app.drain()
    publication = app.runtime.stage(build_run, "result")["publication_ids"][0]
    with app.db.read() as db:
        pack = app.knowledge.publication_pack(db, other.id, publication)
    assert pack["topic_links"] and pack["topic_links"][0]["topic_key"] == topic_key
    run = app.runtime.create(other.id, namespace.id, funding, "chat", {}, 4, "optional-tutor")
    epoch = app.runtime.claim(run, "optional-worker")
    original = env["model"].complete
    entered, release = asyncio.Event(), asyncio.Event()

    async def complete(request):
        result = await original(request)
        if request["role"] == "tutor":
            assert pack["topic_links"][0]["id"] in request["access_dependencies"]
            assert pack["topic_links"][0]["topic_version_id"] in request["access_dependencies"]
            entered.set()
            if timing == "inflight":
                await release.wait()
            return result.model_copy(update={"text": "PRIVATE_OPTIONAL_ALIAS_COPY_WITHOUT_IDS"})
        return result

    env["model"].complete = complete
    pending = asyncio.create_task(
        app.answers.tutor(
            other.id,
            "Explain the function",
            {"knowledge_units": [pack]},
            {"teaching_skill": {"content": {"explanation_order": "concept_first"}}},
            {},
            (run, epoch, "optional-worker"),
        )
    )
    await entered.wait()
    if timing == "completed":
        await pending
    await confirm(app.privacy, actor, app.privacy.propose(actor))
    release.set()
    if timing == "inflight":
        with pytest.raises(DomainError):
            await pending
    with app.db.read() as db:
        receipt = db.scalar(
            select(ModelReceipt).where(ModelReceipt.run_id == run, ModelReceipt.role == "tutor")
        )
        assert receipt.response is None and receipt.status == "redacted"
        witness = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run, StageArtifact.stage_key == "model-access:" + receipt.id
            )
        )
        assert witness is not None and witness.payload == {}
