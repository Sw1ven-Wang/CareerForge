"""Synthetic ordinary conversation admission, evidence and authority regressions."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import httpx
import pytest
from conftest import publish_fixture
from pydantic import ValidationError
from sqlalchemy import select

from careerforge.api import ConversationBody, create_app
from careerforge.contracts import AnswerPart, ModelOutput, RetrievalTaskSpec, TaskType, TurnInput
from careerforge.contracts_ordinary_checker_v166 import (
    ORDINARY_CHECKER_CONTRACT,
    ORDINARY_CHECKER_KEY,
    AdoptConversationCheckerInput,
)
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    Conversation,
    FundingAgreement,
    Idempotency,
    ModelReceipt,
    Turn,
)
from careerforge.models_privacy_ext import PrivacyState
from careerforge.services.checker_evidence import CHECKER_CONTEXT_MAX_BYTES
from careerforge.services.dependencies import invalidate
from careerforge.services.evolution_replay import engine_versions
from careerforge.services.ordinary_checker import (
    bind_ordinary_selection_in,
    ordinary_checker_identity,
)
from careerforge.services.security import create_principal
from careerforge.util import fingerprint


def legacy_conversation(env):
    """Unit-test setup only; separate subprocess tests cover actual old-code state."""
    with env["app"].db.transaction() as db:
        row = Conversation(principal_id=env["actor"], namespace_id=env["ns"], preferences={})
        db.add(row)
        db.flush()
        return row.id


def adoption(revision=1, key="adopt"):
    return AdoptConversationCheckerInput(
        expected_revision=revision, contract=ORDINARY_CHECKER_CONTRACT, idempotency_key=key
    )


async def ready_ordinary(env):
    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    conv = app.conversations.create(actor)
    value = TurnInput(
        question="返回 value",
        expected_revision=1,
        funding_id=env["funding"],
        idempotency_key="ordinary",
    )
    started = app.conversations.submit(actor, conv, value)
    run = (
        started["run_id"],
        app.runtime.claim(started["run_id"], "ordinary-test"),
        "ordinary-test",
    )
    with app.db.read() as db:
        token = db.get(Turn, started["turn_id"]).focus[ORDINARY_CHECKER_KEY]
    evidence = await app.retrieval.retrieve(
        actor,
        RetrievalTaskSpec(
            turn_id=started["turn_id"],
            user_question=value.question,
            focus_revision=1,
            task_type=TaskType.IMPLEMENTATION,
            project_source_ids=[source],
        ),
        answer_checker_contract=token,
    )
    with app.db.transaction() as db:
        bind_ordinary_selection_in(
            db, actor, db.get(Turn, started["turn_id"]), evidence["retrieval_manifest_id"]
        )
    unit = evidence["knowledge_units"][0]
    unit = app.answers._fresh_unit(
        actor,
        unit,
        unit["facts"][0]["fact_atom_id"],
        selection_manifest_id=evidence["retrieval_manifest_id"],
    )
    fact = unit["facts"][0]
    draft = AnswerPart(
        kind="repository_fact",
        text=fact["text"],
        fact_atom_ids=[fact["fact_atom_id"]],
        publication_ids=[unit["publication_id"]],
        conditions=fact["conditions"],
    )
    env["model"].calls.clear()
    return conv, value, started, run, unit, draft


def test_new_conversation_defaults_new_contract_without_dto_changes(env):
    app, actor = env["app"], env["actor"]
    conv = app.conversations.create(actor)
    before = app.conversations.detail(actor, conv)
    assert before["preferences"][ORDINARY_CHECKER_KEY]["contract"] == ORDINARY_CHECKER_CONTRACT
    assert ORDINARY_CHECKER_KEY not in TurnInput.model_fields
    assert set(ConversationBody.model_fields) == {"auto_build", "build_call_cap"}
    assert engine_versions()["ordinary_checker_implementation"] == fingerprint(
        ordinary_checker_identity()
    )
    with pytest.raises(ValidationError):
        AdoptConversationCheckerInput(
            expected_revision=True, contract=ORDINARY_CHECKER_CONTRACT, idempotency_key="x"
        )
    with pytest.raises(DomainError) as error:
        app.conversations.adopt_checker_contract(actor, conv, adoption())
    assert error.value.code == "CHECKER_ALREADY_ADOPTED"


def test_upgrade_owner_revision_full_request_idempotency_and_busy(env):
    app, actor = env["app"], env["actor"]
    conv = legacy_conversation(env)
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "other")
    with pytest.raises(DomainError) as error:
        app.conversations.adopt_checker_contract(other.id, conv, adoption())
    assert error.value.code == "NOT_FOUND"
    with pytest.raises(DomainError) as error:
        app.conversations.adopt_checker_contract(actor, conv, adoption(2))
    assert error.value.code == "REVISION_CONFLICT"
    original = app.conversations.adopt_checker_contract(actor, conv, adoption())
    assert original == app.conversations.adopt_checker_contract(actor, conv, adoption())
    assert original["revision"] == 2
    with pytest.raises(DomainError) as error:
        app.conversations.adopt_checker_contract(actor, conv, adoption(2))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    other_conv = legacy_conversation(env)
    with pytest.raises(DomainError) as error:
        app.conversations.adopt_checker_contract(actor, other_conv, adoption())
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    queued = app.conversations.submit(
        actor,
        other_conv,
        TurnInput(question="value", expected_revision=1, idempotency_key="queued"),
    )
    with pytest.raises(DomainError) as error:
        app.conversations.adopt_checker_contract(actor, other_conv, adoption(2, "fresh"))
    assert error.value.code == "CONVERSATION_BUSY"
    with app.db.read() as db:
        assert ORDINARY_CHECKER_KEY not in db.get(Turn, queued["turn_id"]).focus
        assert ORDINARY_CHECKER_KEY not in db.get(Conversation, other_conv).preferences


def test_upgrade_concurrency_is_revision_serialized(env):
    app, actor = env["app"], env["actor"]
    conv = legacy_conversation(env)

    def upgrade(key):
        try:
            return app.conversations.adopt_checker_contract(actor, conv, adoption(key=key))
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(upgrade, ["a", "b"]))
    assert sum(isinstance(x, dict) for x in results) == 1
    assert "REVISION_CONFLICT" in results


@pytest.mark.asyncio
async def test_ordinary_checker_transmits_only_current_fact_question_scope_and_exact_receipt(env):
    app, actor = env["app"], env["actor"]
    _, value, started, run, unit, draft = await ready_ordinary(env)
    args = (actor, value.question, draft, unit, run, "check")
    first = await app.answers._check_relevance(*args)
    assert await app.answers._check_relevance(*args) == first
    assert len(env["model"].calls) == 1
    request = env["model"].calls[0]
    evidence = request["context"]["reviewed_evidence"]
    assert evidence["contract"] == ORDINARY_CHECKER_CONTRACT
    assert "question_scope" in evidence and "session_scope" not in evidence
    assert evidence["citations"] == unit["citations"]
    assert request["context"]["facts"] == unit["facts"]
    assert (
        not {"plan_id", "module_id", "history", "goal", "session_request"}
        & evidence["question_scope"].keys()
    )
    assert request["data_classes"] == ["learning_state", "private_source"]
    assert evidence["question_scope"]["binding_manifest_id"] in request["access_dependencies"]
    assert evidence["fact_binding"]["claim_id"] in request["access_dependencies"]
    assert all(c["citation_id"] in request["access_dependencies"] for c in unit["citations"])
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"]))
        assert receipt.stage_key.endswith(":" + ORDINARY_CHECKER_CONTRACT)
        assert (
            db.get(AgentRun, started["run_id"]).input_manifest[ORDINARY_CHECKER_KEY]
            == db.get(Turn, started["turn_id"]).focus[ORDINARY_CHECKER_KEY]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "turn_marker",
        "run_marker",
        "both_markers",
        "run_turn_id",
        "question",
        "manifest_hash",
        "parent_marker",
        "binding_deleted",
        "binding_revoked",
    ],
)
async def test_contract_stripping_tampering_or_downgrade_fails_closed(env, field):
    app, actor = env["app"], env["actor"]
    conv, value, started, run, unit, draft = await ready_ordinary(env)
    with app.db.transaction() as db:
        turn, active = db.get(Turn, started["turn_id"]), db.get(AgentRun, started["run_id"])
        token = deepcopy(turn.focus[ORDINARY_CHECKER_KEY])
        if field in {"turn_marker", "both_markers"}:
            turn.focus = {k: v for k, v in turn.focus.items() if k != ORDINARY_CHECKER_KEY}
        if field in {"run_marker", "both_markers"}:
            active.input_manifest = {
                k: v for k, v in active.input_manifest.items() if k != ORDINARY_CHECKER_KEY
            }
            active.input_hash = fingerprint(active.input_manifest)
        if field == "run_turn_id":
            active.input_manifest = {
                k: v for k, v in active.input_manifest.items() if k != "turn_id"
            }
            active.input_hash = fingerprint(active.input_manifest)
        if field == "question":
            turn.question = "unrelated private question"
        if field == "manifest_hash":
            turn.focus = {**turn.focus, ORDINARY_CHECKER_KEY: {**token, "manifest_hash": "changed"}}
        if field == "parent_marker":
            row = db.get(Conversation, conv)
            row.preferences = {
                k: v for k, v in row.preferences.items() if k != ORDINARY_CHECKER_KEY
            }
        if field == "binding_deleted":
            db.delete(
                db.scalar(
                    select(Idempotency).where(
                        Idempotency.operation == "conversation.checker_turn",
                        Idempotency.key == turn.id,
                    )
                )
            )
        if field == "binding_revoked":
            invalidate(db, actor, token["manifest_id"], "revoked", "test")
    if field == "parent_marker":
        # Existing turn depends on its own immutable admission, not today's parent marker.
        await app.answers._check_relevance(actor, value.question, draft, unit, run, "check")
        with pytest.raises(DomainError):
            app.conversations.detail(actor, conv)
    else:
        with pytest.raises(DomainError):
            await app.answers._check_relevance(actor, value.question, draft, unit, run, "check")
        assert not env["model"].calls


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after", "replay"])
@pytest.mark.parametrize("change", ["source", "privacy", "classes", "funding", "payer"])
async def test_current_egress_authority_before_after_and_paid_replay(env, phase, change):
    app, actor = env["app"], env["actor"]
    _, value, started, run, unit, draft = await ready_ordinary(env)

    def mutate():
        with app.db.transaction() as db:
            if change == "source":
                invalidate(db, actor, unit["source_id"], "revoked", "test")
            elif change == "privacy":
                db.get(PrivacyState, actor).generation += 1
            else:
                fund = db.get(FundingAgreement, env["funding"])
                if change == "classes":
                    fund.allowed_data_classes = ["learning_state", "public_source"]
                elif change == "funding":
                    fund.status = "revoked"
                else:
                    other, _, _ = create_principal(db, "wrong-payer")
                    fund.payer_id = other.id

    args = (actor, value.question, draft, unit, run, "check")
    if phase == "before":
        mutate()
    elif phase == "after":
        original = env["model"].complete

        async def changed(request):
            result = await original(request)
            mutate()
            return result

        env["model"].complete = changed
    else:
        await app.answers._check_relevance(*args)
        mutate()
    with pytest.raises(DomainError):
        await app.answers._check_relevance(*args)
    assert len(env["model"].calls) == int(phase != "before")
    if phase == "after":
        with app.db.read() as db:
            receipt = db.scalar(
                select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"])
            )
            assert receipt.status == "redacted" and receipt.response is None


@pytest.mark.asyncio
async def test_full_request_idempotency_and_budget_and_negative_verdict(env):
    app, actor = env["app"], env["actor"]
    conv, value, _, run, unit, draft = await ready_ordinary(env)
    with pytest.raises(DomainError) as error:
        app.conversations.submit(actor, conv, value.model_copy(update={"funding_id": None}))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    oversized = draft.model_copy(update={"text": "界" * CHECKER_CONTEXT_MAX_BYTES})
    with pytest.raises(DomainError) as error:
        await app.answers._check_relevance(actor, value.question, oversized, unit, run, "large")
    assert error.value.code == "MODEL_CONTEXT_LIMIT" and not env["model"].calls
    env["model"].overrides["answer_checker"] = lambda _: ModelOutput(
        structured={
            "supported": False,
            "conditions_preserved": True,
            "quantifiers_preserved": True,
            "no_new_project_facts": False,
            "evidence_relevant": True,
            "reasons": ["unproven explanation"],
        },
        synthetic=True,
    )
    proposed = draft.model_copy(
        update={"text": "An unproven explanation", "transformation": "paraphrase"}
    )
    result = await app.answers._guard_relevant_block(
        actor, value.question, proposed, [unit], {"explanation_order": "code_first"}, run, "check"
    )
    assert (
        result["blocked_reason"] == "ANSWER_UNSUPPORTED" and result["checked"] == "exact_fallback"
    )


@pytest.mark.asyncio
async def test_regular_http_feedback_flow_and_no_run_replay_remain_ordinary(env):
    app = env["app"]
    await publish_fixture(env)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        conv = (await client.post("/api/v1/conversations", json={})).json()
        identifier = conv["conversation_id"]
        body = {
            "question": "学习Tool Calling实现",
            "expected_revision": 1,
            "idempotency_key": "ordinary-http",
        }
        response = await client.post(f"/api/v1/conversations/{identifier}/turns", json=body)
        assert response.status_code == 202, response.text
        started = response.json()
        assert started["run_id"] is None
        with app.db.read() as db:
            turn = db.get(Turn, started["turn_id"])
            assert turn.status == "completed" and ORDINARY_CHECKER_KEY in turn.focus
            assert "session_tutor_binding" not in turn.focus
        before = len(env["model"].calls)
        blocks = app.answers.committed(env["actor"], started["turn_id"])
        assert blocks
        await app.conversations.process(started["turn_id"])
        assert app.answers.committed(env["actor"], started["turn_id"]) == blocks
        assert len(env["model"].calls) == before
        feedback = await client.post(
            "/api/v1/feedback",
            json={
                "turn_id": started["turn_id"],
                "signal": "explicit_feedback",
                "content": "先看代码",
                "consent": True,
            },
        )
        assert feedback.status_code == 201
        with app.db.transaction() as db:
            turn = db.get(Turn, started["turn_id"])
            turn.focus = {k: v for k, v in turn.focus.items() if k != ORDINARY_CHECKER_KEY}
        response = await client.get(f"/api/v1/turns/{started['turn_id']}")
        assert response.status_code == 200 and response.json()["status"] == "blocked"
        assert all(event["payload"].get("redacted") is True for event in response.json()["events"])


@pytest.mark.asyncio
async def test_checker_rejects_foreign_question_and_stale_or_incomplete_citations(env):
    app, actor = env["app"], env["actor"]
    _, value, _, run, unit, draft = await ready_ordinary(env)
    with pytest.raises(DomainError) as error:
        await app.answers._check_relevance(
            actor, "different private question", draft, unit, run, "check"
        )
    assert error.value.code == "CHECKER_EVIDENCE_SCOPE"
    for field in ["quote", "path", "source_id", "read_id", "quote_hash"]:
        wrong = deepcopy(unit)
        wrong["citations"][0][field] = "forged"
        with pytest.raises(DomainError):
            await app.answers._check_relevance(actor, value.question, draft, wrong, run, "check")
    wrong = deepcopy(unit)
    wrong["citations"] = []
    with pytest.raises(DomainError):
        await app.answers._check_relevance(actor, value.question, draft, wrong, run, "check")
    assert not env["model"].calls


@pytest.mark.asyncio
async def test_new_upgrade_http_requires_explicit_version_and_owner(env):
    app = env["app"]
    conv = legacy_conversation(env)
    api = create_app(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        route = f"/api/v2/conversations/{conv}/checker-contract-adoptions"
        assert (
            await client.post(route, json={"expected_revision": 1, "idempotency_key": "x"})
        ).status_code == 422
        body = adoption().model_dump(mode="json")
        response = await client.post(route, json=body)
        assert response.status_code == 200
        assert (await client.post(route, json=body)).json() == response.json()
        assert (await client.post(route, json={**body, "expected_revision": 2})).status_code == 409
