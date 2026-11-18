"""Synthetic privacy/provenance mechanics, never model quality or native output."""

from copy import deepcopy

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_privacy_public_authority_v166 import source_fixture
from test_privacy_v16 import confirm, service

from careerforge.contracts import (
    KnowledgeBuildInput,
    ModelOutput,
    RetrievalTaskSpec,
    TaskType,
    ToolCall,
)
from careerforge.contracts_learning_ext import ActivityInput
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    Evidence,
    FundingAgreement,
    ModelReceipt,
    ObjectRow,
    Principal,
    Publication,
    ReviewDecision,
    SourceRead,
    StageArtifact,
    UnitVersion,
)
from careerforge.models_privacy_ext import PrivacyDeletion, PrivacyState
from careerforge.services.consumer_context import CONSUMER_CONTEXT_CONTRACT
from careerforge.services.public_read_authority import (
    PUBLIC_READ_ERASURE_CONTRACT,
    PUBLIC_READ_ERASURE_STAGE,
)
from careerforge.services.security import create_principal
from careerforge.util import canonical, fingerprint, uid


async def publication_case(env, *, public=True, key="build", repository="fixtures/agent"):
    app, actor = env["app"], env["actor"]
    source, publication, root = await publish_fixture(
        env,
        key=key,
        repository=repository,
        namespace=env["owner"]["public_namespace_id"] if public else None,
    )
    with app.db.read() as db:
        pub = db.get(Publication, publication)
        review = db.get(ReviewDecision, pub.review_id)
        run = db.get(AgentRun, review.reviewer_run_id)
        bundle = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run.id,
                StageArtifact.stage_key == "independent:" + pub.unit_version_id,
            )
        )
        read_id = bundle.payload["reads"][0]["read_id"]
        receipt = db.scalar(
            select(ModelReceipt).where(
                ModelReceipt.run_id == run.id,
                ModelReceipt.stage_key == "review.final:" + pub.unit_version_id,
            )
        )
        before = app.knowledge.publication_pack(
            db, actor, publication, consumer_context_contract=CONSUMER_CONTEXT_CONTRACT
        )
        return {
            "source": source,
            "publication": publication,
            "review": review.id,
            "version": pub.unit_version_id,
            "run": run.id,
            "root": root,
            "read": read_id,
            "receipt": receipt.id,
            "before": deepcopy(before),
        }


def witness_in(db, case):
    return db.scalar(
        select(StageArtifact).where(
            StageArtifact.run_id == case["run"],
            StageArtifact.stage_key == PUBLIC_READ_ERASURE_STAGE + case["read"],
        )
    )


def context_pack(app, actor, case):
    with app.db.read() as db:
        return app.knowledge.publication_pack(
            db,
            actor,
            case["publication"],
            consumer_context_contract=CONSUMER_CONTEXT_CONTRACT,
        )


@pytest.mark.asyncio
async def test_public_review_proof_survives_erasure_without_retaining_personal_session(env):
    app, actor = env["app"], env["actor"]
    case = await publication_case(env)
    unused = app.sources.read_span(
        actor, case["source"], "src/tools.py", 1, 3, "reviewer", "PERSONAL_UNUSED_SESSION"
    )["read_id"]
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "public-proof-consumer")
        assert witness_in(db, case) is None
        original_receipts = {
            row.id: (row.request_hash, canonical(row.response))
            for row in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == case["run"]))
        }
        original_input = deepcopy(db.get(AgentRun, case["run"]).input_manifest)
    calls_before = len(env["model"].calls)
    svc = service(env)
    proposal = svc.propose(actor)
    for item in (proposal, proposal):
        await confirm(svc, actor, item)
        assert context_pack(app, other.id, case) == case["before"]
    await confirm(svc, actor, svc.propose(actor))
    assert context_pack(app, other.id, case) == case["before"]
    with app.db.read() as db:
        read = db.get(SourceRead, case["read"])
        witness = witness_in(db, case)
        assert read.session_key == "" and read.text
        assert witness.payload == {
            "contract": PUBLIC_READ_ERASURE_CONTRACT,
            "publication_id": case["publication"],
            "deletion_id": proposal["proposal_id"],
            "binding_hash": witness.payload["binding_hash"],
        }
        assert len(witness.payload["binding_hash"]) == 64
        assert read.text not in canonical(witness.payload)
        assert "reviewer:" + case["version"] not in canonical(witness.payload)
        assert db.get(SourceRead, unused).text == db.get(SourceRead, unused).session_key == ""
        assert not db.scalar(
            select(StageArtifact).where(
                StageArtifact.stage_key == PUBLIC_READ_ERASURE_STAGE + unused
            )
        )
        assert {
            row.id: (row.request_hash, canonical(row.response))
            for row in db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == case["run"]))
        } == original_receipts
        assert db.get(AgentRun, case["run"]).input_manifest == original_input
        legacy = app.knowledge.publication_pack(db, other.id, case["publication"])
        assert legacy == {k: v for k, v in case["before"].items() if k != "consumer_context"}
    result = await app.retrieval.retrieve(
        other.id,
        RetrievalTaskSpec(
            turn_id="public-after-erasure",
            task_type=TaskType.IMPLEMENTATION,
            user_question="解释 Tool Calling 返回值",
            focus_revision=1,
            project_source_ids=[case["source"]],
        ),
    )
    assert result["knowledge_units"] == [case["before"]]
    plan = app.learning.propose_plan(other.id, [case["publication"]], "function", "公开事实")
    app.learning.accept_plan(other.id, plan["plan_id"], plan["version_id"], 1)
    activity = app.learning.create_activity(
        other.id, ActivityInput(module_id=plan["module_ids"][0])
    )
    assert activity["specification"]["consumer_context_contract"] == CONSUMER_CONTEXT_CONTRACT
    assert len(env["model"].calls) == calls_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "missing_witness",
        "unknown_witness",
        "malformed_witness",
        "binding_hash",
        "wrong_publication",
        "wrong_read",
        "wrong_run",
        "wrong_version",
        "wrong_deletion",
        "uncommitted_deletion",
        "wrong_generation",
        "nonempty_session",
        "missing_receipt",
        "changed_receipt",
        "rehash_receipt_request",
        "changed_receipt_usage",
        "missing_access_witness",
        "revoked_source",
        "revoked_evidence",
        "revoked_publication",
        "unknown_review_contract",
        "non_system",
        "mixed_data_classes",
        "private_data_classes",
        "payer_mismatch",
        "nonadmin",
        "inactive",
    ],
)
async def test_retained_public_session_never_replaces_live_authority(env, mutation):
    app, actor = env["app"], env["actor"]
    case = await publication_case(env)
    with app.db.transaction() as db:
        reader, _, _ = create_principal(db, "active-public-reader")
    svc = service(env)
    proposal = svc.propose(actor)
    await confirm(svc, actor, proposal)
    assert context_pack(app, reader.id, case) == case["before"]
    calls_before = len(env["model"].calls)
    with app.db.transaction() as db:
        witness = witness_in(db, case)
        if mutation == "missing_witness":
            db.delete(witness)
        elif mutation == "unknown_witness":
            witness.payload = {**witness.payload, "contract": "unknown"}
        elif mutation == "malformed_witness":
            witness.payload = {**witness.payload, "deletion_id": []}
        elif mutation == "binding_hash":
            witness.payload = {**witness.payload, "binding_hash": "0" * 64}
        elif mutation == "wrong_publication":
            witness.payload = {**witness.payload, "publication_id": uid()}
        elif mutation == "wrong_read":
            other_read = db.scalar(
                select(SourceRead).where(
                    SourceRead.source_id == case["source"], SourceRead.id != case["read"]
                )
            )
            witness.stage_key = PUBLIC_READ_ERASURE_STAGE + other_read.id
        elif mutation == "wrong_run":
            witness.run_id = case["root"]
        elif mutation == "wrong_version":
            db.get(UnitVersion, case["version"]).render_hash = "0" * 64
        elif mutation == "wrong_deletion":
            witness.payload = {**witness.payload, "deletion_id": uid()}
        elif mutation == "uncommitted_deletion":
            db.get(PrivacyDeletion, proposal["proposal_id"]).state = "proposed"
        elif mutation == "wrong_generation":
            db.get(PrivacyState, actor).generation = db.get(
                PrivacyDeletion, proposal["proposal_id"]
            ).generation
        elif mutation == "nonempty_session":
            db.get(SourceRead, case["read"]).session_key = "arbitrary-session"
        elif mutation == "missing_receipt":
            db.delete(db.get(ModelReceipt, case["receipt"]))
        elif mutation == "changed_receipt":
            receipt = db.get(ModelReceipt, case["receipt"])
            changed = deepcopy(receipt.response)
            changed["structured"]["scope_review"]["standalone_claim_ids"] = []
            receipt.response = changed
        elif mutation == "rehash_receipt_request":
            receipt = db.get(ModelReceipt, case["receipt"])
            receipt.request_hash = "0" * 64
            access = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == case["run"],
                    StageArtifact.stage_key == "model-access:" + receipt.id,
                )
            )
            access.payload = {**access.payload, "request_hash": receipt.request_hash}
        elif mutation == "changed_receipt_usage":
            receipt = db.get(ModelReceipt, case["receipt"])
            receipt.response = {**receipt.response, "usage": {"prompt_tokens": 999}}
        elif mutation == "missing_access_witness":
            db.delete(
                db.scalar(
                    select(StageArtifact).where(
                        StageArtifact.run_id == case["run"],
                        StageArtifact.stage_key == "model-access:" + case["receipt"],
                    )
                )
            )
        elif mutation in {"revoked_source", "revoked_evidence", "revoked_publication"}:
            target = {
                "revoked_source": case["source"],
                "revoked_publication": case["publication"],
                "revoked_evidence": db.scalar(
                    select(Evidence.id).where(Evidence.read_id == case["read"])
                ),
            }[mutation]
            db.get(ObjectRow, target).state = "revoked"
        elif mutation == "unknown_review_contract":
            review = db.get(ReviewDecision, case["review"])
            review.report = {**review.report, "knowledge_review_contract_version": "unknown"}
            obj = db.get(ObjectRow, review.id)
            obj.payload, obj.content_hash = review.report, fingerprint(review.report)
        elif mutation == "non_system":
            run = db.get(AgentRun, case["run"])
            db.get(FundingAgreement, run.funding_id).kind = "private"
        elif mutation in {"mixed_data_classes", "private_data_classes", "payer_mismatch"}:
            funding = db.get(FundingAgreement, db.get(AgentRun, case["run"]).funding_id)
            if mutation == "payer_mismatch":
                funding.payer_id = reader.id
            else:
                funding.allowed_data_classes = (
                    ["public_source", "private_source"]
                    if mutation == "mixed_data_classes"
                    else ["private_source"]
                )
        elif mutation == "nonadmin":
            db.get(Principal, actor).is_admin = False
        elif mutation == "inactive":
            db.get(Principal, actor).active = False
    with pytest.raises(DomainError):
        context_pack(app, reader.id, case)
    assert len(env["model"].calls) == calls_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "private",
        "non_system",
        "empty_session",
        "arbitrary_session",
        "unknown_review_contract",
        "missing_receipt",
        "missing_access_witness",
        "revoked_publication",
        "mixed_data_classes",
        "private_data_classes",
        "payer_mismatch",
        "nonadmin",
        "inactive",
    ],
)
async def test_erasure_never_attests_unknown_or_invalid_prior_authority(env, mutation):
    app, actor = env["app"], env["actor"]
    case = await publication_case(env, public=mutation != "private")
    svc = service(env)
    proposal = svc.propose(actor)
    with app.db.transaction() as db:
        run = db.get(AgentRun, case["run"])
        if mutation == "non_system":
            db.get(FundingAgreement, run.funding_id).kind = "private"
        elif mutation in {"empty_session", "arbitrary_session"}:
            db.get(SourceRead, case["read"]).session_key = (
                "" if mutation == "empty_session" else "PERSONAL_NOT_REVIEWED"
            )
        elif mutation == "unknown_review_contract":
            review = db.get(ReviewDecision, case["review"])
            review.report = {**review.report, "knowledge_review_contract_version": "unknown"}
            obj = db.get(ObjectRow, review.id)
            obj.payload, obj.content_hash = review.report, fingerprint(review.report)
        elif mutation == "missing_receipt":
            db.delete(db.get(ModelReceipt, case["receipt"]))
        elif mutation == "missing_access_witness":
            db.delete(
                db.scalar(
                    select(StageArtifact).where(
                        StageArtifact.run_id == case["run"],
                        StageArtifact.stage_key == "model-access:" + case["receipt"],
                    )
                )
            )
        elif mutation == "revoked_publication":
            db.get(ObjectRow, case["publication"]).state = "revoked"
        elif mutation in {"mixed_data_classes", "private_data_classes", "payer_mismatch"}:
            funding = db.get(FundingAgreement, run.funding_id)
            if mutation == "payer_mismatch":
                funding.payer_id = create_principal(db, "different-payer")[0].id
            else:
                funding.allowed_data_classes = (
                    ["public_source", "private_source"]
                    if mutation == "mixed_data_classes"
                    else ["private_source"]
                )
        elif mutation == "nonadmin":
            db.get(Principal, actor).is_admin = False
        elif mutation == "inactive":
            db.get(Principal, actor).active = False
    calls_before = len(env["model"].calls)
    if mutation == "inactive":
        with pytest.raises(DomainError) as denied:
            await confirm(svc, actor, proposal)
        assert denied.value.code == "UNAUTHENTICATED"
        with app.db.read() as db:
            assert witness_in(db, case) is None
            assert db.get(SourceRead, case["read"]).session_key == "reviewer:" + case["version"]
        return
    await confirm(svc, actor, proposal)
    with app.db.read() as db:
        assert witness_in(db, case) is None
        assert db.get(SourceRead, case["read"]).session_key == ""
    with pytest.raises(DomainError):
        context_pack(app, actor, case)
    assert len(env["model"].calls) == calls_before


@pytest.mark.asyncio
async def test_public_namespace_without_reviewed_publication_has_no_session_exception(env):
    app, actor = env["app"], env["actor"]
    source = source_fixture(env)
    read = app.sources.read_span(actor, source, "src/tools.py", 1, 3, "reviewer", "PERSONAL")
    with app.db.transaction() as db:
        app.sources.evidence_from_read(
            db,
            actor,
            env["owner"]["public_namespace_id"],
            source,
            read["read_id"],
            "return value",
            "reviewer",
            "PERSONAL",
        )
    svc = service(env)
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        assert db.get(SourceRead, read["read_id"]).session_key == ""
        assert not list(
            db.scalars(
                select(StageArtifact).where(
                    StageArtifact.stage_key.startswith(PUBLIC_READ_ERASURE_STAGE)
                )
            )
        )
    assert env["model"].calls == []


@pytest.mark.asyncio
async def test_counterevidence_cannot_create_public_erasure_authority(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    original = env["model"].complete

    async def complete(request):
        output = await original(request)
        if request["role"] == "reviewer" and request.get("schema"):
            changed = deepcopy(output.structured)
            changed["decision"] = "needs_evidence"
            changed["claim_decisions"][0]["counterevidence"] = deepcopy(
                changed["claim_decisions"][0]["independent_reads"]
            )
            output = output.model_copy(update={"structured": changed})
        return output

    monkeypatch.setattr(env["model"], "complete", complete)
    source = source_fixture(env)
    namespace = env["owner"]["public_namespace_id"]
    run = app.build.start(
        actor,
        KnowledgeBuildInput(
            namespace_id=namespace,
            funding_id=app.runtime.create_funding(actor, namespace, 100),
            question="Tool Calling 返回值示例",
            source_id=source,
            topic_keys=["agent.tool_calling"],
            call_cap=10,
            max_units_per_project=1,
            idempotency_key="counterevidence",
        ),
    )
    await app.drain()
    assert app.runtime.stage(run, "result")["publication_ids"] == []
    calls_before = len(env["model"].calls)
    svc = service(env)
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        assert not list(
            db.scalars(
                select(StageArtifact).where(
                    StageArtifact.stage_key.startswith(PUBLIC_READ_ERASURE_STAGE)
                )
            )
        )
    assert len(env["model"].calls) == calls_before


@pytest.mark.asyncio
async def test_erased_uncited_read_inside_original_bundle_remains_a_gap(env, monkeypatch):
    """No reconstruction of discarded reads, even from a still-public source."""
    app, actor = env["app"], env["actor"]
    original = env["model"].complete

    async def complete(request):
        output = await original(request)
        if request["role"] == "reviewer" and not request.get("schema"):
            return ModelOutput(
                synthetic=True,
                tool_calls=[
                    ToolCall(
                        id="uncited-exploration",
                        name="read_span",
                        arguments={"path": "README.md", "start_line": 1, "end_line": 2},
                    ),
                    ToolCall(
                        id="finish",
                        name="finish_investigation",
                        arguments={"reason": "evidence_sufficient"},
                    ),
                ],
                finish_reason="tool_calls",
            )
        return output

    monkeypatch.setattr(env["model"], "complete", complete)
    case = await publication_case(env)
    with app.db.read() as db:
        bundle = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == case["run"],
                StageArtifact.stage_key == "independent:" + case["version"],
            )
        )
        extra = next(
            item["read_id"] for item in bundle.payload["reads"] if item["path"] == "README.md"
        )
        assert not db.scalar(select(Evidence).where(Evidence.read_id == extra))
        assert db.get(SourceRead, extra).text
        before_reads = set(db.scalars(select(SourceRead.id)))
        before_evidence = set(db.scalars(select(Evidence.id)))
        before_reviews = set(db.scalars(select(ReviewDecision.id)))
    calls_before = len(env["model"].calls)
    svc = service(env)
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        read = db.get(SourceRead, extra)
        assert read.path == read.text == read.session_key == ""
        assert not db.scalar(
            select(StageArtifact).where(
                StageArtifact.stage_key == PUBLIC_READ_ERASURE_STAGE + extra
            )
        )
        assert witness_in(db, case) is not None
        assert app.knowledge.publication_pack(db, actor, case["publication"])["facts"]
        assert set(db.scalars(select(SourceRead.id))) == before_reads
        assert set(db.scalars(select(Evidence.id))) == before_evidence
        assert set(db.scalars(select(ReviewDecision.id))) == before_reviews
    with pytest.raises(DomainError):
        context_pack(app, actor, case)
    assert len(env["model"].calls) == calls_before


@pytest.mark.asyncio
async def test_whole_publication_without_standalone_approval_is_not_upgraded(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    original = env["model"].complete

    async def complete(request):
        output = await original(request)
        if request.get("schema"):
            changed = deepcopy(output.structured)
            if request["role"] == "reviewer":
                changed["scope_review"]["standalone_claim_ids"] = []
            elif request["role"] == "coverage" and "scope_binding" in request["context"]:
                for element in changed["scope_audit"]["elements"]:
                    element["standalone"] = False
            output = output.model_copy(update={"structured": changed})
        return output

    monkeypatch.setattr(env["model"], "complete", complete)
    case = await publication_case(env)
    assert all(
        row["standalone"] is False for row in case["before"]["consumer_context"]["facts"].values()
    )
    calls_before = len(env["model"].calls)
    svc = service(env)
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        assert witness_in(db, case) is None
        assert app.knowledge.publication_pack(db, actor, case["publication"]) == {
            key: value for key, value in case["before"].items() if key != "consumer_context"
        }
    with pytest.raises(DomainError):
        context_pack(app, actor, case)
    assert len(env["model"].calls) == calls_before


@pytest.mark.asyncio
async def test_partial_publication_keeps_quarantine_after_public_erasure(env, monkeypatch):
    from test_scoped_knowledge_review_v166 import partial_fixture

    app, actor = env["app"], env["actor"]
    namespace = env["owner"]["public_namespace_id"]
    public_env = {
        **env,
        "ns": namespace,
        "funding": app.runtime.create_funding(actor, namespace, 100),
    }
    _, publication, _ = await partial_fixture(public_env, monkeypatch)
    with app.db.read() as db:
        before = app.knowledge.publication_pack(
            db, actor, publication, consumer_context_contract=CONSUMER_CONTEXT_CONTRACT
        )
        assert len(before["facts"]) == 1
        assert "UNSUPPORTED_UNIT_NARRATIVE" not in canonical(before)
        assert "总能验证" not in canonical(before)
    calls_before = len(env["model"].calls)
    svc = service(env)
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        assert (
            app.knowledge.publication_pack(
                db, actor, publication, consumer_context_contract=CONSUMER_CONTEXT_CONTRACT
            )
            == before
        )
    assert len(env["model"].calls) == calls_before


@pytest.mark.asyncio
async def test_public_erasure_witness_cannot_move_between_real_units(env):
    app, actor = env["app"], env["actor"]
    first = await publication_case(env)
    second = await publication_case(env, key="second", repository="fixtures/second-public")
    svc = service(env)
    await confirm(svc, actor, svc.propose(actor))
    assert context_pack(app, actor, first) == first["before"]
    assert context_pack(app, actor, second) == second["before"]
    with app.db.transaction() as db:
        # Keep the target's valid run/input key; copying another real public
        # proof still must not grant this unit/read independent provenance.
        witness_in(db, second).payload = deepcopy(witness_in(db, first).payload)
    with pytest.raises(DomainError):
        context_pack(app, actor, second)
    assert context_pack(app, actor, first) == first["before"]


@pytest.mark.asyncio
async def test_mixed_private_read_cannot_gain_a_public_bundle_witness(env):
    app, actor = env["app"], env["actor"]
    case = await publication_case(env)
    private = source_fixture(env, private=True, key="mixed-private")
    observed = app.sources.read_span(
        actor, private, "src/tools.py", 1, 3, "reviewer", "reviewer:" + case["version"]
    )
    with app.db.transaction() as db:
        db.get(SourceRead, observed["read_id"]).run_id = case["run"]
        bundle = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == case["run"],
                StageArtifact.stage_key == "independent:" + case["version"],
            )
        )
        bundle.payload = {**bundle.payload, "reads": [*bundle.payload["reads"], observed]}
        review = db.get(ReviewDecision, case["review"])
        review.independent_bundle_hash = fingerprint(bundle.payload)
        review.report = {
            **review.report,
            "independent_bundle_hash": review.independent_bundle_hash,
        }
        obj = db.get(ObjectRow, review.id)
        obj.payload, obj.content_hash = review.report, fingerprint(review.report)
    with pytest.raises(DomainError):
        context_pack(app, actor, case)
    svc = service(env)
    await confirm(svc, actor, svc.propose(actor))
    with app.db.read() as db:
        assert witness_in(db, case) is None
        read = db.get(SourceRead, observed["read_id"])
        assert read.path == read.text == read.session_key == ""


def test_public_read_guard_source_is_part_of_replay_engine_identity(monkeypatch):
    from careerforge.services import evolution_replay, public_read_authority

    before = evolution_replay.engine_versions()
    original = evolution_replay.inspect.getsource

    def changed(module):
        source = original(module)
        return (
            source + "\n# Synthetic implementation-only change.\n"
            if module is public_read_authority
            else source
        )

    monkeypatch.setattr(evolution_replay.inspect, "getsource", changed)
    after = evolution_replay.engine_versions()
    assert (
        after["retrieval_authority_implementation"] != before["retrieval_authority_implementation"]
    )
    assert {k: v for k, v in after.items() if k != "retrieval_authority_implementation"} == {
        k: v for k, v in before.items() if k != "retrieval_authority_implementation"
    }
