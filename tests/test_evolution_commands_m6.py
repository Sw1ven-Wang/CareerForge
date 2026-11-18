"""Synthetic engineering tests only. No real human feedback or heldout acceptance."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from sqlalchemy import select
from test_evolution import candidate, dataset

from careerforge.errors import DomainError
from careerforge.models import EvolutionCandidate, Idempotency, Outbox, PolicyHead, PolicyVersion
from careerforge.services.evolution_commands import EvolutionCommands
from careerforge.services.security import create_principal


async def evaluated(env, order="code_first", name="commands-fixture"):
    c, base, _, _, feedback = await candidate(env, order=order)
    report = await env["app"].evolution.evaluate(
        env["actor"], c, dataset(env, order=order, name=name)
    )
    assert report["decision"] == "passed"
    return c, base, report["evaluation_id"], feedback


def head(env):
    with env["app"].db.read() as db:
        row = db.scalar(
            select(PolicyHead).where(
                PolicyHead.namespace_id == env["ns"], PolicyHead.target == "teaching_skill"
            )
        )
        return row.policy_id, row.revision


@pytest.mark.asyncio
async def test_promote_replay_returns_original_effect_before_live_gate(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    c, base, evaluation, _ = await evaluated(env)
    commands = EvolutionCommands(app)
    first = commands.promote(actor, c, evaluation, 1, "promote")
    assert first["head_revision"] == 2 and first["current_available"] is True
    with app.db.read() as db:
        saved = deepcopy(db.get(Idempotency, first["command_receipt_id"]).result)

    def forbidden(*args, **kwargs):
        raise AssertionError("original mutation must not be called on command replay")

    monkeypatch.setattr(app.evolution, "promote_in", forbidden)
    assert commands.promote(actor, c, evaluation, 1, "promote") == first
    assert head(env) == (first["policy_id"], 2)
    with app.db.read() as db:
        assert db.get(Idempotency, first["command_receipt_id"]).result == saved
        assert db.get(PolicyVersion, first["policy_id"]).candidate_id == c
        assert len(list(db.scalars(select(Outbox).where(Outbox.kind == "policy.audit")))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["evaluation", "revision", "scope"])
async def test_promote_same_key_changed_identity_has_no_effect(env, change):
    app, actor = env["app"], env["actor"]
    c, _, evaluation, _ = await evaluated(env)
    commands = EvolutionCommands(app)
    first = commands.promote(actor, c, evaluation, 1, "promote", scope={"request_id": "one"})
    with pytest.raises(DomainError, match="相同演化命令键") as error:
        commands.promote(
            actor,
            c,
            "other" if change == "evaluation" else evaluation,
            2 if change == "revision" else 1,
            "promote",
            scope={"request_id": "two" if change == "scope" else "one"},
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert head(env) == (first["policy_id"], 2)


@pytest.mark.asyncio
async def test_rollback_same_command_never_walks_next_ancestor(env):
    app, actor = env["app"], env["actor"]
    commands = EvolutionCommands(app)
    c1, root, e1, _ = await evaluated(env)
    one = commands.promote(actor, c1, e1, 1, "p1")
    c2, _, e2, _ = await evaluated(env, "comparison_first", "second-fixture")
    two = commands.promote(actor, c2, e2, 2, "p2")
    back = commands.rollback(
        actor, env["ns"], "teaching_skill", 3, "r1", two["policy_id"], one["policy_id"]
    )
    assert head(env) == (one["policy_id"], 4)
    assert (
        commands.rollback(
            actor, env["ns"], "teaching_skill", 3, "r1", two["policy_id"], one["policy_id"]
        )
        == back
    )
    final = commands.rollback(actor, env["ns"], "teaching_skill", 4, "r2", one["policy_id"], root)
    assert head(env) == (root, 5)
    old = commands.rollback(
        actor, env["ns"], "teaching_skill", 3, "r1", two["policy_id"], one["policy_id"]
    )
    assert old["policy_id"] == one["policy_id"] and old["head_revision"] == 4
    assert old["current_available"] is False
    assert final["current_available"] is True
    replay_promotion = commands.promote(actor, c2, e2, 2, "p2")
    assert (
        replay_promotion["policy_id"] == two["policy_id"]
        and not replay_promotion["current_available"]
    )
    assert head(env) == (root, 5)


@pytest.mark.asyncio
async def test_withdrawal_preserves_history_not_live_authority(env):
    app, actor = env["app"], env["actor"]
    c, _, evaluation, feedback = await evaluated(env)
    commands = EvolutionCommands(app)
    first = commands.promote(actor, c, evaluation, 1, "p")
    app.evolution.revoke_feedback(actor, feedback["feedback_id"])
    historical = commands.promote(actor, c, evaluation, 1, "p")
    assert historical["policy_id"] == first["policy_id"]
    assert historical["head_revision"] == first["head_revision"]
    assert historical["current_available"] is False
    with app.db.read() as db:
        assert db.get(EvolutionCandidate, c).status == "consent_revoked"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["promote", "rollback"])
async def test_effect_and_receipt_commit_atomically(env, monkeypatch, action):
    app, actor = env["app"], env["actor"]
    c, base, evaluation, _ = await evaluated(env)
    commands = EvolutionCommands(app)
    prior = None
    if action == "rollback":
        prior = commands.promote(actor, c, evaluation, 1, "p")
    expected = head(env)

    def fail_receipt(*args, **kwargs):
        raise RuntimeError("crash after effect before receipt")

    monkeypatch.setattr(commands, "_save", fail_receipt)
    with pytest.raises(RuntimeError, match="after effect"):
        if action == "promote":
            commands.promote(actor, c, evaluation, 1, "crash")
        else:
            commands.rollback(
                actor, env["ns"], "teaching_skill", 2, "crash", prior["policy_id"], base
            )
    assert head(env) == expected
    with app.db.read() as db:
        assert not db.scalar(select(Idempotency).where(Idempotency.key == "crash"))
        assert len(list(db.scalars(select(Outbox).where(Outbox.kind == "policy.audit")))) == (
            1 if prior else 0
        )
        assert db.get(EvolutionCandidate, c).status == ("promoted" if prior else "evaluated")


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["effect_hash", "audit", "candidate_link"])
async def test_corrupt_historical_receipt_is_not_inferred_from_head(env, drift):
    app, actor = env["app"], env["actor"]
    c, _, evaluation, _ = await evaluated(env)
    commands = EvolutionCommands(app)
    first = commands.promote(actor, c, evaluation, 1, "p")
    with app.db.transaction() as db:
        if drift == "effect_hash":
            row = db.get(Idempotency, first["command_receipt_id"])
            row.result = {**row.result, "effect_hash": "0" * 64}
        elif drift == "audit":
            db.get(Outbox, first["audit_id"]).payload = {}
        else:
            db.get(PolicyVersion, first["policy_id"]).candidate_id = "wrong"
    with pytest.raises(DomainError) as error:
        commands.promote(actor, c, evaluation, 1, "p")
    assert error.value.code == "EVOLUTION_EFFECT_RECEIPT"
    assert head(env) == (first["policy_id"], 2)


@pytest.mark.asyncio
async def test_wrong_rollback_target_and_new_promote_command_fail_closed(env):
    app, actor = env["app"], env["actor"]
    c, base, evaluation, _ = await evaluated(env)
    commands = EvolutionCommands(app)
    first = commands.promote(actor, c, evaluation, 1, "p")
    with pytest.raises(DomainError) as error:
        commands.promote(actor, c, evaluation, 1, "different-command")
    assert error.value.code == "PROMOTION_GATE"
    with pytest.raises(DomainError) as error:
        commands.rollback(actor, env["ns"], "teaching_skill", 2, "r", first["policy_id"], "wrong")
    assert error.value.code == "ROLLBACK_CONFLICT"
    assert head(env) == (first["policy_id"], 2)
    with app.db.read() as db:
        assert not db.scalar(
            select(Idempotency).where(Idempotency.key.in_(["different-command", "r"]))
        )


@pytest.mark.asyncio
async def test_unauthorized_actor_cannot_read_effect(env):
    app, actor = env["app"], env["actor"]
    c, _, evaluation, _ = await evaluated(env)
    commands = EvolutionCommands(app)
    first = commands.promote(actor, c, evaluation, 1, "p")
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "unrelated")
        other_id = other.id
    with pytest.raises(DomainError):
        commands.promote(other_id, c, evaluation, 1, "p")
    assert head(env) == (first["policy_id"], 2)


@pytest.mark.asyncio
async def test_concurrent_same_key_returns_one_effect(env):
    app, actor = env["app"], env["actor"]
    c, _, evaluation, _ = await evaluated(env)
    commands = EvolutionCommands(app)

    def promote(_):
        return commands.promote(actor, c, evaluation, 1, "concurrent")

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(promote, range(3)))
    assert results[0] == results[1] == results[2]
    assert head(env) == (results[0]["policy_id"], 2)
    with app.db.read() as db:
        assert len(list(db.scalars(select(Outbox).where(Outbox.kind == "policy.audit")))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["from", "to", "revision", "scope"])
async def test_rollback_replay_requires_exact_original_request(env, change):
    app, actor = env["app"], env["actor"]
    c, base, evaluation, _ = await evaluated(env)
    commands = EvolutionCommands(app)
    first = commands.promote(actor, c, evaluation, 1, "p")
    commands.rollback(
        actor,
        env["ns"],
        "teaching_skill",
        2,
        "r",
        first["policy_id"],
        base,
        scope={"execution_id": "original"},
    )
    with pytest.raises(DomainError) as error:
        commands.rollback(
            actor,
            env["ns"],
            "teaching_skill",
            3 if change == "revision" else 2,
            "r",
            "changed" if change == "from" else first["policy_id"],
            "changed" if change == "to" else base,
            scope={"execution_id": "changed" if change == "scope" else "original"},
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert head(env) == (base, 3)


@pytest.mark.asyncio
async def test_in_helper_does_not_commit_callers_transaction(env):
    app, actor = env["app"], env["actor"]
    c, base, evaluation, _ = await evaluated(env)
    commands = EvolutionCommands(app)
    with pytest.raises(RuntimeError):
        with app.db.transaction() as db:
            result = commands.promote_in(db, actor, c, evaluation, 1, "outer")
            assert result["current_available"]
            raise RuntimeError("caller crashes after receipt flush")
    assert head(env) == (base, 1)
    with app.db.read() as db:
        assert not db.scalar(select(Idempotency).where(Idempotency.key == "outer"))
        assert not list(db.scalars(select(Outbox).where(Outbox.kind == "policy.audit")))


@pytest.mark.asyncio
async def test_rehashed_malformed_effect_fails_closed(env):
    from careerforge.util import fingerprint

    app, actor = env["app"], env["actor"]
    c, _, evaluation, _ = await evaluated(env)
    commands = EvolutionCommands(app)
    first = commands.promote(actor, c, evaluation, 1, "p")
    with app.db.transaction() as db:
        row = db.get(Idempotency, first["command_receipt_id"])
        effect = deepcopy(row.result["effect"])
        del effect["before_hash"]
        row.result = {"effect": effect, "effect_hash": fingerprint(effect)}
    with pytest.raises(DomainError) as error:
        commands.promote(actor, c, evaluation, 1, "p")
    assert error.value.code == "EVOLUTION_EFFECT_RECEIPT"
    assert head(env) == (first["policy_id"], 2)
