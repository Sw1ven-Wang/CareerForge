import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.contracts import FeedbackInput, PolicyPatch, TurnInput
from careerforge.errors import DomainError
from careerforge.models import (
    EvalDataset,
    Feedback,
    PolicyHead,
    PolicyVersion,
    Turn,
)
from careerforge.services.evolution import paired_report
from careerforge.services.policies import ensure_policies, validate_policy


async def candidate(env, order="code_first"):
    app = env["app"]
    actor = env["actor"]
    conv = app.conversations.create(actor)
    turn = app.conversations.submit(
        actor,
        conv,
        TurnInput(question="什么是Tool Calling", expected_revision=1, idempotency_key="feedback"),
    )
    await app.conversations.process(turn["turn_id"])
    f = app.evolution.feedback(
        actor,
        FeedbackInput(
            turn_id=turn["turn_id"],
            signal="explicit_feedback",
            content="太抽象，请先看代码",
            consent=True,
        ),
    )
    with app.db.transaction() as db:
        base = ensure_policies(db, actor, env["ns"])["teaching_skill"]["policy_id"]
    patch = PolicyPatch(
        target="teaching_skill",
        content={"explanation_order": order},
        experience_ids=[f["experience_id"]],
        training_group_ids=["conversation:" + conv],
    )
    c = app.evolution.create_candidate(actor, env["ns"], base, patch)
    return c, base, conv, turn["turn_id"], f


def dataset(env, order="code_first", name="holdout", groups=None):
    cases = [
        {
            "case_id": str(i),
            "group_id": (groups or ["independent-a", "independent-b"])[i % 2],
            "input": {"question": "解释工具调用"},
            "expected": {"preferred_order": order},
        }
        for i in range(4)
    ]
    return env["app"].evolution.register_dataset(
        env["actor"], env["ns"], name, "1", cases, "fixture", []
    )


@pytest.mark.asyncio
async def test_candidate_paired_eval_atomic_promote_and_rollback(env):
    app = env["app"]
    c, base, conv, _, _ = await candidate(env)
    ds = dataset(env)
    with pytest.raises(DomainError):
        app.evolution.promote(env["actor"], c, "invented", 1)
    report = await app.evolution.evaluate(env["actor"], c, ds)
    assert (
        report["decision"] == "passed"
        and report["evaluation_scope"]
        == "typed_policy_behavior_not_general_model_or_learning_quality"
    )
    # A new turn freezes the champion BEFORE promotion.
    frozen = app.conversations.submit(
        env["actor"],
        conv,
        TurnInput(question="继续解释", expected_revision=2, idempotency_key="frozen"),
    )
    promoted = app.evolution.promote(env["actor"], c, report["evaluation_id"], 1)
    with app.db.read() as db:
        assert (
            db.get(Turn, frozen["turn_id"]).policy_versions["teaching_skill"]["policy_id"] == base
        )
        assert db.get(PolicyVersion, promoted["policy_id"]).candidate_id == c
    with pytest.raises(DomainError):
        app.evolution.promote(env["actor"], c, report["evaluation_id"], 1)
    rollback = app.evolution.rollback(env["actor"], env["ns"], "teaching_skill", 2)
    assert rollback["policy_id"] == base and rollback["head_revision"] == 3


@pytest.mark.asyncio
async def test_leakage_and_modified_gold_cannot_promote(env):
    app = env["app"]
    c, base, conv, _, _ = await candidate(env)
    leaky = dataset(env, name="leaky", groups=["conversation:" + conv, "other"])
    with pytest.raises(DomainError):
        await app.evolution.evaluate(env["actor"], c, leaky)
    ds = dataset(env)
    report = await app.evolution.evaluate(env["actor"], c, ds)
    with app.db.transaction() as db:
        db.get(EvalDataset, ds).cases = []
    with pytest.raises(DomainError):
        app.evolution.promote(env["actor"], c, report["evaluation_id"], 1)


@pytest.mark.asyncio
async def test_failed_and_inconclusive_are_not_published(env):
    app = env["app"]
    c, _, _, _, _ = await candidate(env)
    bad = dataset(env, "concept_first", "bad")
    result = await app.evolution.evaluate(env["actor"], c, bad)
    assert result["decision"] == "failed"
    with pytest.raises(DomainError):
        app.evolution.promote(env["actor"], c, result["evaluation_id"], 1)
    small = app.evolution.register_dataset(
        env["actor"],
        env["ns"],
        "small",
        "1",
        [
            {
                "case_id": "1",
                "group_id": "one",
                "input": {},
                "expected": {"preferred_order": "code_first"},
            }
        ],
        "fixture",
        [],
    )
    r = await app.evolution.evaluate(env["actor"], c, small)
    assert r["decision"] == "inconclusive"
    with pytest.raises(DomainError):
        app.evolution.promote(env["actor"], c, r["evaluation_id"], 1)


@pytest.mark.asyncio
async def test_consent_withdrawal_prevents_evaluation(env):
    app = env["app"]
    c, _, _, _, f = await candidate(env)
    ds = dataset(env)
    with app.db.transaction() as db:
        db.get(Feedback, f["feedback_id"]).consent_revision = 0
    with pytest.raises(DomainError):
        await app.evolution.evaluate(env["actor"], c, ds)


@pytest.mark.asyncio
async def test_reflection_has_no_release_authority(env):
    app = env["app"]
    _, base, _, _, f = await candidate(env)
    run = app.evolution.start_candidate(
        env["actor"], env["ns"], env["funding"], [f["experience_id"]], "teaching_skill", "reflect"
    )
    await app.drain()
    result = app.runtime.stage(run, "result")
    assert result and result["published"] is False
    with app.db.read() as db:
        head = db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill"))
        assert head.policy_id == base


@pytest.mark.parametrize(
    "target,content",
    [
        ("reviewer", {}),
        ("teaching_skill", {"skip_review": True}),
        ("retrieval_recipe", {"authorized_namespace_refs": ["*"]}),
        ("routing_policy", {"auto_spend": True}),
        ("research_heuristic", {"max_model_calls": 9999}),
    ],
)
def test_policy_cannot_change_security(target, content):
    with pytest.raises(DomainError):
        validate_policy(target, content)


@pytest.mark.asyncio
async def test_personal_preference_changes_next_layout_not_global_policy(env):
    app = env["app"]
    _, pub, _ = await publish_fixture(env)
    c, base, conv, _, _ = await candidate(env)
    t = app.conversations.submit(
        env["actor"],
        conv,
        TurnInput(question="继续讲解", expected_revision=2, idempotency_key="next"),
    )
    await app.conversations.process(t["turn_id"])
    blocks = [
        e["payload"]["content"]
        for e in app.answers.events(env["actor"], t["turn_id"])["events"]
        if e["type"] == "answer.block.validated"
    ]
    assert any(b.get("style") == "code_first" for b in blocks)
    with app.db.read() as db:
        assert (
            db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill")).policy_id
            == base
        )


def test_bootstrap_rejects_safety_and_unjustified_gain():
    items = [{"group_id": str(i % 3), "baseline": 1.0, "candidate": 1.0} for i in range(12)]
    assert paired_report(items, 4, 2)["decision"] == "inconclusive"
    items[0]["safety_violations"] = 1
    assert paired_report(items, 4, 2)["decision"] == "failed"


@pytest.mark.asyncio
async def test_research_frontier_candidate_is_evaluated_on_same_observation_budget(env):
    app = env["app"]
    _, _, conv, _, f = await candidate(env)
    with app.db.transaction() as db:
        base = ensure_policies(db, env["actor"], env["ns"])["research_heuristic"]["policy_id"]
    patch = PolicyPatch(
        target="research_heuristic",
        content={"preferred_topic_keys": ["agent.memory"]},
        experience_ids=[f["experience_id"]],
        training_group_ids=["conversation:" + conv],
    )
    c = app.evolution.create_candidate(env["actor"], env["ns"], base, patch)
    cases = [
        {
            "case_id": str(i),
            "group_id": "repo-family-" + str(i % 2),
            "input": {
                "request_budget": 1,
                "nodes": [
                    {
                        "id": "old",
                        "parent_id": None,
                        "depth": 1,
                        "created_at": "a",
                        "topics": ["python"],
                        "repository_ids": ["irrelevant"],
                        "cost": 1,
                    },
                    {
                        "id": "memory",
                        "parent_id": None,
                        "depth": 1,
                        "created_at": "b",
                        "topics": ["agent.memory"],
                        "repository_ids": ["wanted"],
                        "cost": 1,
                    },
                ],
            },
            "expected": {"relevant_repository_ids": ["wanted"]},
        }
        for i in range(4)
    ]
    ds = app.evolution.register_dataset(
        env["actor"], env["ns"], "frontier", "1", cases, "fixture", []
    )
    result = await app.evolution.evaluate(env["actor"], c, ds)
    assert (
        result["decision"] == "passed"
        and result["baseline_score"] == 0
        and result["candidate_score"] == 1
    )
    assert "not_new_query_generation" in result["research_evaluation_scope"]
    promoted = app.evolution.promote(env["actor"], c, result["evaluation_id"], 1)
    assert promoted["active_from"] == "next_task_only"


def test_frontier_replay_rejects_cycle_and_budget_overrun():
    from careerforge.services.research_policy import replay_frontier

    result = replay_frontier(
        {},
        {
            "request_budget": 1,
            "nodes": [
                {
                    "id": "a",
                    "parent_id": None,
                    "depth": 1,
                    "topics": [],
                    "repository_ids": ["x"],
                    "cost": 2,
                }
            ],
        },
    )
    assert result["requests_used"] == 0 and result["repository_ids"] == []
    with pytest.raises(DomainError):
        replay_frontier(
            {},
            {
                "request_budget": 1,
                "nodes": [
                    {
                        "id": "a",
                        "parent_id": "a",
                        "depth": 1,
                        "topics": [],
                        "repository_ids": [],
                        "cost": 1,
                    }
                ],
            },
        )
