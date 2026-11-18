"""Governance integration tests; the judge is an explicitly synthetic response fixture."""

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_evolution import candidate, dataset

from careerforge.contracts import FeedbackInput, ImportSourceInput, TurnInput
from careerforge.errors import DomainError
from careerforge.models import (
    Dispute,
    Feedback,
    KnowledgeGap,
    ObjectRow,
    PolicyHead,
    Publication,
    Turn,
)


@pytest.mark.asyncio
async def test_dispute_fresh_review_required_and_history_unchanged(env):
    app = env["app"]
    actor = env["actor"]
    source, pub, run = await publish_fixture(env)
    with app.db.read() as db:
        old_review = db.get(Publication, pub).review_id
    d = app.evolution.dispute(actor, pub, "希望重新核验返回条件")
    with pytest.raises(DomainError):
        app.lifecycle.resolve(actor, d["dispute_id"], pub, "uphold", old_review, "不应重用旧审核")
    recheck = app.lifecycle.recheck(actor, d["dispute_id"], pub, env["funding"], "recheck")
    await app.drain()
    result = app.runtime.stage(recheck["run_id"], "result")
    assert result and result["review_ids"]
    resolution = app.lifecycle.resolve(
        actor, d["dispute_id"], pub, "uphold", result["review_ids"][0], "新的隔离读取仍支持原断言"
    )
    assert not resolution["historical_answers_rewritten"]
    with app.db.read() as db:
        assert db.get(Dispute, d["dispute_id"]).status == "resolved"
        assert db.get(ObjectRow, pub).state == "active"
    with pytest.raises(DomainError):
        app.lifecycle.resolve(
            actor, d["dispute_id"], pub, "uphold", result["review_ids"][0], "覆盖决议"
        )


@pytest.mark.asyncio
async def test_feedback_correction_opens_dispute_not_experience(env):
    app = env["app"]
    actor = env["actor"]
    await publish_fixture(env)
    conv = app.conversations.create(actor)
    t = app.conversations.submit(
        actor,
        conv,
        TurnInput(question="学习Tool Calling", expected_revision=1, idempotency_key="teach"),
    )
    await app.conversations.process(t["turn_id"])
    f = app.evolution.feedback(
        actor,
        FeedbackInput(
            turn_id=t["turn_id"],
            signal="correction",
            content="这个结论不准确，请复核",
            consent=True,
        ),
    )
    assert f["dispute_ids"] and f["experience_id"] is None and not f["knowledge_changed"]


@pytest.mark.asyncio
async def test_gap_record_requires_explicit_funding_to_schedule(env):
    app = env["app"]
    actor = env["actor"]
    conv = app.conversations.create(actor)
    t = app.conversations.submit(
        actor,
        conv,
        TurnInput(question="学习Tool Calling", expected_revision=1, idempotency_key="missing"),
    )
    await app.conversations.process(t["turn_id"])
    with app.db.read() as db:
        gap = db.scalar(select(KnowledgeGap).where(KnowledgeGap.turn_id == t["turn_id"]))
    assert gap and gap.status == "open" and gap.build_run_id is None
    with pytest.raises(DomainError) as unscoped:
        app.lifecycle.schedule_gap(actor, gap.id, env["funding"], 24, "gap")
    assert unscoped.value.code == "GAP_SOURCE_SCOPE"
    # New schedules require an explicitly selected existing source. This is
    # still a funding/idempotency test, not authorization for source discovery.
    source = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/gap-scope",
            commit_sha="c" * 40,
            license_id="MIT",
            files={"sample.py": "def example():\n    return None\n"},
            source_metadata={"synthetic": True},
        ),
    )
    with pytest.raises(DomainError) as unfunded:
        app.lifecycle.schedule_gap(actor, gap.id, "not-authorized", 24, "gap", source_id=source)
    assert unfunded.value.code == "FUNDING_UNAVAILABLE"
    response = app.lifecycle.schedule_gap(
        actor, gap.id, env["funding"], 24, "gap", source_id=source
    )
    assert response["run_id"] and not response["automatic_publication_approval"]
    assert app.lifecycle.schedule_gap(actor, gap.id, env["funding"], 24, "gap", source_id=source)[
        "replayed"
    ]


@pytest.mark.asyncio
async def test_changed_source_requires_review_does_not_revoke_old(env):
    app = env["app"]
    source, pub, _ = await publish_fixture(env)
    from support import FILES

    newer = app.sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/agent",
            commit_sha="b" * 40,
            license_id="MIT",
            files={**FILES, "config.toml": "enabled=false\n"},
        ),
    )
    report = app.lifecycle.preview_update(env["actor"], source, newer)
    assert report["changed_files"] == ["config.toml"]
    assert all(not u["automatic_semantic_carry_forward"] for u in report["units"])
    with app.db.read() as db:
        assert app.knowledge.publication_pack(db, env["actor"], pub)


@pytest.mark.asyncio
async def test_canary_authorized_assignment_frozen_and_regression_reverts(env):
    app = env["app"]
    actor = env["actor"]
    c, base, _, _, _ = await candidate(env)
    ds = dataset(env)
    ev = await app.evolution.evaluate(actor, c, ds)
    trial = app.rollouts.start(actor, c, ev["evaluation_id"], 1, 50, 1, 20)
    with app.db.read() as db:
        assert (
            db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill")).policy_id
            == base
        )
    assigned = {}
    # These are real persisted turns. Test feedback is injected directly into the internal monitor.
    for _i in range(30):
        conv = app.conversations.create(actor)
        t = app.conversations.submit(
            actor,
            conv,
            TurnInput(question="学习Tool Calling", expected_revision=1, idempotency_key="t"),
        )
        with app.db.read() as db:
            ref = db.get(Turn, t["turn_id"]).policy_versions["teaching_skill"]
        assert ref["policy_id"] == (
            trial["trial_policy_id"] if ref["rollout_arm"] == "candidate" else base
        )
        assigned[ref["rollout_arm"]] = t["turn_id"]
        if len(assigned) == 2:
            break
    assert set(assigned) == {"control", "candidate"}
    app.rollouts.observe_turn(assigned["control"], [], 10, "completed")
    app.rollouts.observe_turn(
        assigned["candidate"], [{"blocked_reason": "synthetic_guard_failure"}], 10, "completed"
    )
    detail = app.rollouts.detail(actor, trial["rollout_id"])
    assert detail["status"] == "reverted" and detail["reason"] == "guard_regression"
    with app.db.read() as db:
        assert (
            db.get(Turn, assigned["candidate"]).policy_versions["teaching_skill"]["policy_id"]
            == trial["trial_policy_id"]
        )
        assert (
            db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill")).policy_id
            == base
        )


@pytest.mark.asyncio
async def test_withdraw_feedback_rolls_back_future_policy_and_blocks_reuse(env):
    app = env["app"]
    actor = env["actor"]
    c, base, _, _, feedback = await candidate(env)
    ds = dataset(env)
    ev = await app.evolution.evaluate(actor, c, ds)
    app.evolution.promote(actor, c, ev["evaluation_id"], 1)
    app.evolution.revoke_feedback(actor, feedback["feedback_id"])
    with app.db.read() as db:
        assert (
            db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill")).policy_id
            == base
        )
        assert db.get(Feedback, feedback["feedback_id"]).consent_revision == 0
    with pytest.raises(DomainError):
        await app.evolution.evaluate(actor, c, ds)
