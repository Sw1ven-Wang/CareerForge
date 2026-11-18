"""Legacy context compatibility: safe new requests, exact receipts, no new leaks."""

import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.contracts import AnswerPart, ModelOutput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, FundingAgreement, ModelReceipt


async def legacy_inputs(env, focus):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    with app.db.read() as db:
        pack = app.knowledge.publication_pack(db, actor, publication)
    run_id = app.runtime.create(actor, env["ns"], env["funding"], "chat", {}, 4, "legacy-context")
    epoch = app.runtime.claim(run_id, "legacy-worker")
    return (
        actor,
        "Explain the function",
        {"knowledge_units": [pack]},
        {"teaching_skill": {"content": {"explanation_order": "concept_first"}}},
        focus,
        (run_id, epoch, "legacy-worker"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "focus",
    [
        {},
        {"preferences": {}},
        {
            "preferences": {
                "requested_style": "code_first",
                "check_understanding": False,
                "practice": True,
            }
        },
    ],
)
async def test_bounded_legacy_focus_allows_request_and_exact_receipt_replay(env, focus):
    app = env["app"]
    args = await legacy_inputs(env, focus)
    before = len(env["model"].calls)
    first = await app.answers.tutor(*args)
    assert len(env["model"].calls) == before + 1
    request = env["model"].calls[-1]
    assert request["context"]["focus"] == focus
    assert "replay_only" not in request
    second = await app.answers.tutor(*args)
    assert second == first
    assert len(env["model"].calls) == before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "focus",
    [
        {"history": [{"question": "UNBOUND_HISTORY_BODY"}]},
        {"code_focus": {"path": "UNBOUND_PRIVATE_PATH", "symbol": "UNBOUND_SYMBOL"}},
        {"project_source_ids": ["UNBOUND_SOURCE_ID"]},
        {"arbitrary": "UNBOUND_BODY"},
        {"preferences": {"requested_style": "UNBOUND_STYLE_BODY"}},
        {"preferences": {"requested_style": ["UNBOUND_STYLE_LIST"]}},
        {"preferences": {"practice": "UNBOUND_BOOL_BODY"}},
        {"preferences": {"arbitrary": "UNBOUND_PREFERENCE_BODY"}},
        {"preferences": "UNBOUND_PREFERENCE_OBJECT"},
    ],
)
async def test_unproven_legacy_focus_cannot_create_receipt_charge_or_egress(env, focus):
    app = env["app"]
    args = await legacy_inputs(env, focus)
    run_id = args[-1][0]
    before = len(env["model"].calls)
    with app.db.read() as db:
        consumed = db.get(FundingAgreement, env["funding"]).calls_consumed
    with pytest.raises(DomainError) as error:
        await app.answers.tutor(*args)
    assert error.value.code == "MODEL_REPLAY_ONLY"
    assert len(env["model"].calls) == before
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).calls_consumed == 0
        assert db.get(FundingAgreement, env["funding"]).calls_consumed == consumed
        assert not db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run_id)).all()


@pytest.mark.asyncio
async def test_replay_only_keeps_exact_hash_and_prior_mismatch_uncertain_errors(env):
    app, actor = env["app"], env["actor"]
    run_id = app.runtime.create(actor, env["ns"], env["funding"], "chat", {}, 4, "replay-only")
    epoch = app.runtime.claim(run_id, "receipt-worker")
    env["model"].overrides["tutor"] = lambda _: ModelOutput(
        text="synthetic paid result", synthetic=True
    )

    async def call(context=None, key="paid", replay_only=False):
        return await app.gateway.call(
            run_id,
            epoch,
            "receipt-worker",
            key,
            "tutor",
            "unchanged system",
            context or {"context": "unchanged"},
            data_classes=["learning_state"],
            replay_only=replay_only,
        )

    first = await call()
    with app.db.read() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run_id))
        original_hash = receipt.request_hash
    assert await call(replay_only=True) == first
    assert len(env["model"].calls) == 1
    with pytest.raises(DomainError) as error:
        await call({"context": "different"}, replay_only=True)
    assert error.value.code == "MODEL_REPLAY_CHANGED"
    with app.db.transaction() as db:
        db.get(ModelReceipt, receipt.id).status = "unknown"
    with pytest.raises(DomainError) as error:
        await call(replay_only=True)
    assert error.value.code == "MODEL_RESULT_UNCERTAIN"
    with pytest.raises(DomainError) as error:
        await call(key="never-reserved", replay_only=True)
    assert error.value.code == "MODEL_REPLAY_ONLY"
    with app.db.read() as db:
        assert db.get(ModelReceipt, receipt.id).request_hash == original_hash
        assert db.get(AgentRun, run_id).calls_consumed == 1
        assert len(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run_id)).all()) == 1
    assert len(env["model"].calls) == 1
    assert "replay_only" not in env["model"].calls[0]


@pytest.mark.asyncio
async def test_unsafe_legacy_draft_cannot_start_new_checker(env):
    app = env["app"]
    args = await legacy_inputs(env, {})
    actor, _, evidence, policies, _, run = args
    unit = evidence["knowledge_units"][0]
    fact = unit["facts"][0]
    draft = AnswerPart(
        kind="repository_fact",
        text="A paraphrase carrying UNBOUND_LEGACY_PRIVATE_BODY.",
        fact_atom_ids=[fact["fact_atom_id"]],
        publication_ids=[unit["publication_id"]],
        conditions=fact["conditions"],
        transformation="paraphrase",
    )
    before = len(env["model"].calls)
    with pytest.raises(DomainError) as error:
        await app.answers.guard_block(
            actor,
            draft,
            evidence,
            policies["teaching_skill"]["content"],
            run,
            stage_key="legacy-new-checker",
            replay_only=True,
        )
    assert error.value.code == "MODEL_REPLAY_ONLY"
    assert len(env["model"].calls) == before
    with app.db.read() as db:
        assert not db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run[0])).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["role", "data_class", "receipt_removed"])
async def test_replay_only_does_not_bypass_current_authority_or_mint_missing_receipt(env, change):
    app, actor = env["app"], env["actor"]
    funding = app.runtime.create_funding(
        actor, env["ns"], 4, roles=["tutor"], data_classes=["learning_state"]
    )
    run_id = app.runtime.create(
        actor, env["ns"], funding, "chat", {}, 2, "replay-current-authority"
    )
    epoch = app.runtime.claim(run_id, "receipt-worker")
    env["model"].overrides["tutor"] = lambda _: ModelOutput(text="synthetic", synthetic=True)
    args = (run_id, epoch, "receipt-worker", "paid", "tutor", "", {"context": "same"})
    await app.gateway.call(*args, data_classes=["learning_state"])
    with app.db.transaction() as db:
        if change == "receipt_removed":
            receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run_id))
            db.delete(receipt)
        elif change == "role":
            db.get(FundingAgreement, funding).allowed_roles = []
        else:
            db.get(FundingAgreement, funding).allowed_data_classes = []
    with pytest.raises(DomainError) as error:
        await app.gateway.call(*args, data_classes=["learning_state"], replay_only=True)
    assert error.value.code == (
        "MODEL_REPLAY_ONLY" if change == "receipt_removed" else "EGRESS_OR_ROLE_DENIED"
    )
    assert len(env["model"].calls) == 1
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).calls_consumed == 1
        assert db.get(FundingAgreement, funding).calls_consumed == 1
        count = len(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run_id)).all())
        assert count == int(change != "receipt_removed")


@pytest.mark.asyncio
async def test_budgeted_replay_forwards_receipt_only_flag_without_changing_arm_cap(env):
    from careerforge.services.evolution_replay import BudgetedReplayGateway

    app, actor = env["app"], env["actor"]
    run_id = app.runtime.create(
        actor, env["ns"], env["funding"], "research", {}, 4, "wrapper-replay"
    )
    epoch = app.runtime.claim(run_id, "wrapper-worker")
    gateway = BudgetedReplayGateway(app.gateway, run_id, "replay:0:baseline:", 1, "teaching_skill")
    env["model"].overrides["tutor"] = lambda _: ModelOutput(
        text="synthetic wrapper result", synthetic=True
    )

    async def call(key="paid", replay_only=False):
        return await gateway.call(
            run_id,
            epoch,
            "wrapper-worker",
            "replay:0:baseline:" + key,
            "tutor",
            "unchanged",
            {},
            data_classes=["learning_state"],
            replay_only=replay_only,
        )

    with pytest.raises(DomainError) as error:
        await call(replay_only=True)
    assert error.value.code == "MODEL_REPLAY_ONLY"
    assert not env["model"].calls
    first = await call(replay_only=False)
    assert await call(replay_only=True) == first
    assert await call(replay_only=False) == first
    with pytest.raises(DomainError) as error:
        await call(key="new", replay_only=False)
    assert error.value.code == "REPLAY_ARM_BUDGET"
    assert len(env["model"].calls) == 1
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).calls_consumed == 1
        assert len(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run_id)).all()) == 1
