"""Versioned Session funding defense; fixture providers never spend real funds.

Direct DB mutations are integrity probes, not a claim of a payer-changing API.
Service revocation has its own tests and preserves legacy issued-receipt policy.
"""

from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_checker_evidence_v166 import make_historical_unmarked_session, ready_checker
from test_session_tutor_binding_v166 import accepted

from careerforge.db import clock
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    AnswerBlock,
    FundingAgreement,
    Idempotency,
    ModelReceipt,
    ObjectRow,
    Turn,
)
from careerforge.services.checker_evidence import CHECKER_EVIDENCE_CONTRACT
from careerforge.services.security import create_principal
from careerforge.services.session_tutor import (
    SESSION_FUNDING_CONTRACT,
    SESSION_FUNDING_KEY,
    SESSION_TUTOR_KEY,
)
from careerforge.util import canonical, fingerprint


def funding_receipt(db, turn_id):
    return db.scalar(
        select(Idempotency).where(
            Idempotency.operation == "conversation.session_funding", Idempotency.key == turn_id
        )
    )


def model_receipt(app, run_id):
    with app.db.read() as db:
        row = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run_id))
        return (
            {
                key: deepcopy(getattr(row, key))
                for key in ("id", "status", "payer_id", "response", "request_hash", "usage")
            }
            if row
            else None
        )


def mutate_funding(env, started, change):
    app = env["app"]
    if change == "run_funding":
        replacement = app.runtime.create_funding(env["actor"], env["ns"], 40)
        with app.db.transaction() as db:
            run = db.get(AgentRun, started["run_id"])
            run.funding_id = replacement
            run.input_manifest = {**run.input_manifest, "funding_id": replacement}
            run.input_hash = fingerprint(run.input_manifest)
        return
    with app.db.transaction() as db:
        fund = db.get(FundingAgreement, env["funding"])
        if change == "status":
            fund.status = "revoked"
        elif change == "expiry":
            fund.expires_at = clock(db) - timedelta(seconds=1)
        elif change == "payer":
            other, _, _ = create_principal(db, "different-real-fixture-payer")
            fund.payer_id = other.id
        else:
            assert change == "namespace"
            _, other_ns, _ = create_principal(db, "different-real-fixture-namespace")
            fund.namespace_id = other_ns.id


@pytest.mark.asyncio
async def test_new_admission_freezes_independent_authority_and_keeps_identifiers_local(env):
    app, actor = env["app"], env["actor"]
    started, run, unit, draft = await ready_checker(env)
    with app.db.read() as db:
        turn = db.get(Turn, started["turn_id"])
        token = turn.focus[SESSION_TUTOR_KEY]
        snapshot = db.get(ObjectRow, token["manifest_id"]).payload
        authority = snapshot[SESSION_FUNDING_KEY]
        assert authority == {
            "contract": SESSION_FUNDING_CONTRACT,
            "funding_id": env["funding"],
            "payer_id": actor,
            "namespace_id": env["ns"],
        }
        assert token[SESSION_FUNDING_KEY] == {
            "contract": SESSION_FUNDING_CONTRACT,
            "authority_hash": fingerprint(authority),
        }
        assert funding_receipt(db, turn.id).result == {
            "binding": token,
            "authority": authority,
            "run_id": started["run_id"],
        }
        assert token["checker_evidence"]["contract"] == CHECKER_EVIDENCE_CONTRACT
    await app.answers._check_relevance(actor, "返回 value", draft, unit, run, "check")
    context = canonical(env["model"].calls[0]["context"])
    assert env["funding"] not in context and actor not in context
    assert SESSION_FUNDING_KEY not in context
    assert model_receipt(app, started["run_id"])["payer_id"] == actor


@pytest.mark.asyncio
async def test_unfunded_new_session_has_explicit_empty_funding_authority_without_run(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    env["model"].calls.clear()
    started = app.conversations.submit_session(
        actor, conv, value.model_copy(update={"funding_id": None})
    )
    assert started["run_id"] is None
    with app.db.read() as db:
        turn = db.get(Turn, started["turn_id"])
        authority = funding_receipt(db, turn.id).result["authority"]
        assert authority["funding_id"] is None and authority["payer_id"] is None
    assert app.conversations.session_binding(actor, started["turn_id"])["run_id"] is None
    assert not env["model"].calls


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after", "paid_replay"])
@pytest.mark.parametrize("change", ["status", "payer", "namespace", "expiry", "run_funding"])
async def test_direct_database_funding_mutations_fence_new_execution(env, phase, change):
    app, actor = env["app"], env["actor"]
    started, run, unit, draft = await ready_checker(env)
    args = (actor, "返回 value", draft, unit, run, "check")
    previous = None
    if phase == "before":
        mutate_funding(env, started, change)
    elif phase == "after":
        original = env["model"].complete

        async def changed(request):
            result = await original(request)
            mutate_funding(env, started, change)
            return result

        env["model"].complete = changed
    else:
        await app.answers._check_relevance(*args)
        previous = model_receipt(app, started["run_id"])
        mutate_funding(env, started, change)
    with pytest.raises(DomainError):
        await app.answers._check_relevance(*args)
    assert len(env["model"].calls) == int(phase != "before")
    receipt = model_receipt(app, started["run_id"])
    if phase == "before":
        assert receipt is None
    elif phase == "after":
        assert receipt["status"] == "redacted" and receipt["response"] is None
        assert receipt["payer_id"] == actor
    else:
        # Rejection of new work is not retrospective deletion or reassignment.
        assert receipt == previous


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["status", "payer", "expiry", "namespace"])
async def test_last_dispatch_boundary_rechecks_after_reservation(env, monkeypatch, change):
    app, actor = env["app"], env["actor"]
    started, run, unit, draft = await ready_checker(env)
    original = app.runtime.validate
    mutated = False

    def changed(*args):
        nonlocal mutated
        result = original(*args)
        if not mutated and model_receipt(app, started["run_id"]) is not None:
            mutate_funding(env, started, change)
            mutated = True
        return result

    monkeypatch.setattr(app.runtime, "validate", changed)
    with pytest.raises(DomainError):
        await app.answers._check_relevance(actor, "返回 value", draft, unit, run, "check")
    assert mutated and not env["model"].calls
    receipt = model_receipt(app, started["run_id"])
    assert receipt["response"] is None and receipt["status"] != "completed"


def reseal_session_mirrors(db, turn, run, token, snapshot):
    """Adversarial fixture helper. It deliberately leaves the funding receipt intact."""
    manifest = db.get(ObjectRow, token["manifest_id"])
    manifest.payload, manifest.content_hash = snapshot, fingerprint(snapshot)
    token["snapshot_hash"] = manifest.content_hash
    turn.focus = {**turn.focus, SESSION_TUTOR_KEY: token}
    run.input_manifest = {**run.input_manifest, SESSION_TUTOR_KEY: token}
    run.input_hash = fingerprint(run.input_manifest)
    receipt = db.scalar(
        select(Idempotency).where(
            Idempotency.operation == "conversation.session_tutor", Idempotency.key == turn.id
        )
    )
    receipt.result = token
    session = db.get(ObjectRow, snapshot["learning_session_id"])
    session.payload = {**session.payload, SESSION_TUTOR_KEY: token}
    session.content_hash = fingerprint(session.payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "token_marker",
        "authority",
        "authority_receipt",
        "resealed_stripped_fields",
        "resealed_changed_authority",
        "all_session_hints",
        "run_hints",
        "run_hash",
        "run_workflow",
        "run_turn",
        "detached_turn_and_run_hints",
        "detached_turn_and_all_session_hints",
    ],
)
async def test_independent_authority_cannot_be_bypassed_by_stripped_or_resealed_hints(env, change):
    app = env["app"]
    started, run, _, _ = await ready_checker(env)
    with app.db.transaction() as db:
        turn, active = db.get(Turn, started["turn_id"]), db.get(AgentRun, started["run_id"])
        token = deepcopy(turn.focus[SESSION_TUTOR_KEY])
        snapshot = deepcopy(db.get(ObjectRow, token["manifest_id"]).payload)
        if change == "token_marker":
            token.pop(SESSION_FUNDING_KEY)
            turn.focus = {**turn.focus, SESSION_TUTOR_KEY: token}
        elif change == "authority":
            snapshot.pop(SESSION_FUNDING_KEY)
            reseal_session_mirrors(db, turn, active, token, snapshot)
        elif change == "authority_receipt":
            db.delete(funding_receipt(db, turn.id))
        elif change == "resealed_stripped_fields":
            token.pop(SESSION_FUNDING_KEY)
            snapshot.pop(SESSION_FUNDING_KEY)
            # The separate admission authority remains after every old mirror changes.
            token.pop("checker_evidence")
            snapshot.pop("checker_evidence")
            reseal_session_mirrors(db, turn, active, token, snapshot)
        elif change == "resealed_changed_authority":
            snapshot[SESSION_FUNDING_KEY]["payer_id"] = "different"
            token[SESSION_FUNDING_KEY]["authority_hash"] = fingerprint(
                snapshot[SESSION_FUNDING_KEY]
            )
            reseal_session_mirrors(db, turn, active, token, snapshot)
        elif change in {"all_session_hints", "detached_turn_and_all_session_hints"}:
            if change == "detached_turn_and_all_session_hints":
                turn.run_id = None
            turn.focus = {k: v for k, v in turn.focus.items() if k != SESSION_TUTOR_KEY}
            active.input_manifest = {
                k: v
                for k, v in active.input_manifest.items()
                if k not in {SESSION_TUTOR_KEY, "turn_id"}
            }
            active.input_hash = fingerprint(active.input_manifest)
            old = db.scalar(
                select(Idempotency).where(
                    Idempotency.operation == "conversation.session_tutor",
                    Idempotency.key == turn.id,
                )
            )
            db.delete(old)
        elif change in {"run_hints", "detached_turn_and_run_hints"}:
            if change == "detached_turn_and_run_hints":
                turn.run_id = None
            active.input_manifest = {
                k: v
                for k, v in active.input_manifest.items()
                if k not in {SESSION_TUTOR_KEY, "turn_id"}
            }
            active.input_hash = fingerprint(active.input_manifest)
        elif change == "run_hash":
            active.input_hash = "0" * 64
        elif change == "run_workflow":
            active.workflow = "research"
        else:
            active.input_manifest = {**active.input_manifest, "turn_id": "missing-turn"}
            active.input_hash = fingerprint(active.input_manifest)
    # Call the gateway directly: a service-level preflight is not the security proof.
    with pytest.raises(DomainError):
        await app.gateway.call(*run, "direct", "tutor", "test", {}, data_classes=["learning_state"])
    assert not env["model"].calls and model_receipt(app, started["run_id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("role", ["tutor", "answer_checker"])
async def test_real_revoke_service_cancels_without_new_delivery_and_preserves_legacy_policy(
    env, legacy, role
):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    started = app.conversations.submit_session(actor, conv, value)
    if legacy:
        make_historical_unmarked_session(app, started)
    provider = env["model"]
    provider.calls.clear()
    original = provider.complete
    revocations = 0

    async def revoke_after_return(request):
        nonlocal revocations
        result = await original(request)
        if request["role"] == role:
            app.runtime.revoke_funding(actor, env["funding"])
            revocations += 1
        return result

    provider.complete = revoke_after_return
    await app.execute(started["run_id"])
    count = len(provider.calls)
    assert revocations == 1
    with app.db.read() as db:
        run, turn = db.get(AgentRun, started["run_id"]), db.get(Turn, started["turn_id"])
        assert run.status == "cancelled" and run.epoch > 1
        assert turn.status != "completed"
        assert not list(db.scalars(select(AnswerBlock).where(AnswerBlock.turn_id == turn.id)))
        receipt = db.scalar(
            select(ModelReceipt).where(ModelReceipt.run_id == run.id, ModelReceipt.role == role)
        )
        assert receipt.status == ("completed" if legacy else "redacted")
        assert (receipt.response is not None) is legacy
        assert receipt.payer_id == actor
    with pytest.raises(DomainError):
        app.conversations.submit_session(actor, conv, value)
    await app.execute(started["run_id"])
    assert len(provider.calls) == count


@pytest.mark.asyncio
async def test_completed_history_survives_revoke_without_authorizing_paid_replay(env):
    app, actor = env["app"], env["actor"]
    conv, value, _ = await accepted(env)
    started = app.conversations.submit_session(actor, conv, value)
    await app.execute(started["run_id"])
    before = app.conversations.session_binding(actor, started["turn_id"])
    count = len(env["model"].calls)
    with app.db.read() as db:
        receipts = [
            (row.id, row.status, deepcopy(row.response), row.payer_id)
            for row in db.scalars(
                select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"])
            )
        ]
        assert db.get(Turn, started["turn_id"]).status == "completed"
    app.runtime.revoke_funding(actor, env["funding"])
    assert app.conversations.session_binding(actor, started["turn_id"]) == before
    assert (
        app.conversations.teaching_session(actor, before["learning_session_id"])["delivery"]
        is not None
    )
    assert app.conversations.submit_session(actor, conv, value)["turn_id"] == started["turn_id"]
    await app.execute(started["run_id"])
    assert len(env["model"].calls) == count
    with app.db.read() as db:
        assert receipts == [
            (row.id, row.status, row.response, row.payer_id)
            for row in db.scalars(
                select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"])
            )
        ]
