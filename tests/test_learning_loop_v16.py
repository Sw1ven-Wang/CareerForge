"""Synthetic workflow proofs only; no claim of real learner effectiveness."""

import asyncio

import httpx
import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.api import create_app
from careerforge.contracts import ModelOutput, TurnInput
from careerforge.contracts_learning_ext import (
    ActivityInput,
    AdjustmentInput,
    AttemptInput,
    AttemptSubmission,
    LearningPreferences,
    ObservationInput,
    PersonalizedPlanInput,
    ProfileUpdate,
    RubricResult,
)
from careerforge.errors import DomainError
from careerforge.models import AgentRun, LearningPlan, ModelReceipt, ObjectRow
from careerforge.models_learning_ext import LearningActivity, PlanProposalManifest
from careerforge.services.dependencies import invalidate
from careerforge.services.security import create_principal, set_grant


def plan_input(env, publication, key="plan-loop"):
    return PersonalizedPlanInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        publication_ids=[publication],
        learning_intent="function",
        title="理解返回路径",
        expected_profile_revision=1,
        idempotency_key=key,
    )


def configure_planner(env, mutate=None):
    def produce(request):
        frozen = request["context"]["frozen_input"]
        unit = frozen["units"][0]
        data = {
            "modules": [
                {
                    "key": "focus",
                    "publication_id": unit["publication_id"],
                    "focus_fact_ids": [unit["facts"][0]["fact_atom_id"]],
                    "prerequisite_keys": [],
                    "steps": ["read_reviewed", "explain_conditions"],
                    "rubric_keys": ["evidence_and_version", "conditions"],
                    "estimated_minutes": 20,
                    "rationale_codes": ["focus_on_function", "self_reported_context"],
                }
            ],
            "uncovered_goal_requirements": [],
        }
        if mutate:
            mutate(data)
        return ModelOutput(structured=data, synthetic=True)

    env["model"].overrides["learning_planner"] = produce


async def accepted_plan(env, personalized=True):
    app, actor = env["app"], env["actor"]
    _, pub, _ = await publish_fixture(env)
    if personalized:
        configure_planner(env)
        request = app.learning.start_personalized_plan(actor, plan_input(env, pub))
        await app.drain()
        plan = app.runtime.stage(request["run_id"], "result")
        assert plan is not None
    else:
        plan = app.learning.propose_plan(actor, [pub], "function", "返回值学习")
    app.learning.accept_plan(actor, plan["plan_id"], plan["version_id"], 1)
    return pub, plan


def create_check(env, plan, kind="understanding_check", rubric_keys=None):
    return env["app"].learning.create_activity(
        env["actor"],
        ActivityInput(
            module_id=plan["module_ids"][0],
            kind=kind,
            rubric_keys=rubric_keys or ["evidence_and_version", "conditions"],
        ),
    )


def observation_for(activity, outcome="demonstrated", evaluator="self_review", supersedes=None):
    return ObservationInput(
        rubric_results=[
            RubricResult(
                rubric_item_id=r["id"],
                outcome=outcome,
                evidence_excerpt="本题解释" if outcome != "unknown" else "",
                error_type="none" if outcome == "demonstrated" else "missing_condition",
            )
            for r in activity["specification"]["rubric"]
        ],
        observed_behavior="仅对本题解释作出条件内观察，不概括为永久能力。",
        evaluator_kind=evaluator,
        supersedes_id=supersedes,
    )


@pytest.mark.asyncio
async def test_authorized_personalized_proposal_is_frozen_and_requires_accept(env):
    app, actor = env["app"], env["actor"]
    _, pub, _ = await publish_fixture(env)
    configure_planner(env)
    value = plan_input(env, pub)
    first = app.learning.start_personalized_plan(actor, value)
    repeat = app.learning.start_personalized_plan(actor, value)
    assert first["run_id"] == repeat["run_id"] and repeat["replayed"]
    await app.drain()
    result = app.runtime.stage(first["run_id"], "result")
    assert result["personalized"] and not result["accepted"]
    detail = app.learning.detail(actor, result["plan_id"])
    assert detail["active_version_id"] is None
    with app.db.read() as db:
        frozen = db.scalar(
            select(PlanProposalManifest).where(PlanProposalManifest.run_id == first["run_id"])
        )
        assert frozen.payload["input"]["units"][0]["publication_id"] == pub
        receipts = db.scalars(
            select(ModelReceipt).where(ModelReceipt.run_id == first["run_id"])
        ).all()
        assert len(receipts) == 1 and receipts[0].role == "learning_planner"
    with pytest.raises(DomainError, match="采纳"):
        create_check(env, result)
    accepted = app.learning.accept_plan(actor, result["plan_id"], result["version_id"], 1)
    assert accepted["revision"] == 2
    with pytest.raises(DomainError):
        app.learning.accept_plan(actor, result["plan_id"], result["version_id"], 1)
    assert app.learning.profile(actor)["active_plan_id"] == result["plan_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("violation", ["scope", "cycle", "time", "extra_authority"])
async def test_planner_cannot_widen_evidence_prerequisites_time_or_schema(env, violation):
    app = env["app"]
    _, pub, _ = await publish_fixture(env)

    def corrupt(data):
        if violation == "scope":
            data["modules"][0]["focus_fact_ids"] = ["invented-fact"]
        elif violation == "cycle":
            data["modules"][0]["prerequisite_keys"] = ["focus"]
        elif violation == "time":
            data["modules"][0]["estimated_minutes"] = 100
        else:
            data["modules"][0]["competence_verified"] = True

    configure_planner(env, corrupt)
    request = app.learning.start_personalized_plan(env["actor"], plan_input(env, pub))
    await app.drain()
    assert app.runtime.stage(request["run_id"], "result") is None
    with app.db.read() as db:
        assert not db.scalars(select(LearningPlan)).all()
        assert db.get(AgentRun, request["run_id"]).status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["role", "egress"])
async def test_personalized_plan_requires_explicit_role_and_data_scope(env, denial):
    app, actor = env["app"], env["actor"]
    _, pub, _ = await publish_fixture(env)
    configure_planner(env)
    funding = app.runtime.create_funding(
        actor,
        env["ns"],
        5,
        roles=["tutor"] if denial == "role" else ["learning_planner"],
        data_classes=["public_source"] if denial == "egress" else None,
    )
    value = plan_input(env, pub).model_copy(update={"funding_id": funding})
    before = len(env["model"].calls)
    request = app.learning.start_personalized_plan(actor, value)
    await app.drain()
    assert len(env["model"].calls) == before
    with app.db.read() as db:
        assert db.get(AgentRun, request["run_id"]).status == "failed"


@pytest.mark.asyncio
async def test_cancel_during_planner_cannot_create_late_plan(env):
    app, actor = env["app"], env["actor"]
    _, pub, _ = await publish_fixture(env)
    configure_planner(env)
    called, release = asyncio.Event(), asyncio.Event()
    original = env["model"].complete

    async def delayed(request):
        if request["role"] == "learning_planner":
            called.set()
            await release.wait()
        return await original(request)

    env["model"].complete = delayed
    request = app.learning.start_personalized_plan(actor, plan_input(env, pub))
    task = asyncio.create_task(app.execute(request["run_id"]))
    await asyncio.wait_for(called.wait(), timeout=5)
    with app.db.read() as db:
        revision = db.get(AgentRun, request["run_id"]).revision
    app.runtime.cancel(actor, request["run_id"], revision)
    release.set()
    await task
    with app.db.read() as db:
        assert not db.scalars(select(LearningPlan)).all()
        assert db.get(AgentRun, request["run_id"]).status == "cancelled"


@pytest.mark.asyncio
async def test_optional_checks_respect_opt_out_and_explicit_request(env):
    app, actor = env["app"], env["actor"]
    _, plan = await accepted_plan(env, personalized=False)
    profile = app.learning.profile(actor)
    app.learning.update_profile(
        actor,
        ProfileUpdate(
            expected_revision=profile["revision"],
            preferences=LearningPreferences(checks_enabled=False),
        ),
    )
    with pytest.raises(DomainError) as exc:
        create_check(env, plan)
    assert exc.value.code == "LEARNING_OPT_OUT"
    activity = app.learning.create_activity(
        actor, ActivityInput(module_id=plan["module_ids"][0], explicitly_requested=True)
    )
    assert activity["specification"]["rubric"]


@pytest.mark.asyncio
async def test_independent_comprehension_attempt_blocks_accountwide_help_and_is_not_mastery(env):
    app, actor = env["app"], env["actor"]
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan)
    attempt = app.learning.start_attempt(
        actor,
        activity["activity_id"],
        AttemptInput(
            assistance_mode="independent",
            idempotency_key="independent",
        ),
    )
    conv = app.conversations.create(actor)
    with pytest.raises(DomainError) as exc:
        app.conversations.submit(
            actor,
            conv,
            TurnInput(
                question="不给practice id也帮我", expected_revision=1, idempotency_key="bypass"
            ),
        )
    assert exc.value.code == "INDEPENDENT_ATTEMPT"
    with pytest.raises(DomainError):
        app.learning.propose_plan(
            actor, [activity["specification"]["publication_ids"][0]], "function", "绕开"
        )
    with pytest.raises(DomainError):
        app.coding.start(
            actor, attempt["workspace_id"], 1, [], "帮我完成", env["funding"], 1, "bypass-code"
        )
    submitted = app.learning.submit_attempt(
        actor,
        attempt["attempt_id"],
        AttemptSubmission(response="本题解释", external_help="none_declared"),
    )
    assert (
        submitted["assistance_mode"] == "self_declared_independent"
        and not submitted["competence_verified"]
    )
    record = app.learning.record_observation(
        actor, attempt["attempt_id"], observation_for(activity)
    )
    assert record["hypothesis_status"] == "user_reported" and not record["competence_verified"]
    app.conversations.submit(
        actor, conv, TurnInput(question="现在继续", expected_revision=1, idempotency_key="after")
    )


@pytest.mark.asyncio
async def test_queued_evidence_only_turn_also_blocks_independent_attempt(env):
    app, actor = env["app"], env["actor"]
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan)
    conv = app.conversations.create(actor)
    app.conversations.submit(
        actor, conv, TurnInput(question="解释", expected_revision=1, idempotency_key="waiting")
    )
    with pytest.raises(DomainError) as exc:
        app.learning.start_attempt(
            actor,
            activity["activity_id"],
            AttemptInput(assistance_mode="independent", idempotency_key="no-overlap"),
        )
    assert exc.value.code == "ASSISTANCE_ACTIVE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "help_declaration,expected",
    [("unknown", "unknown"), ("used", "assisted"), ("none_declared", "self_declared_independent")],
)
async def test_external_assistance_is_preserved_not_silently_independent(
    env, help_declaration, expected
):
    app, actor = env["app"], env["actor"]
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan)
    attempt = app.learning.start_attempt(
        actor,
        activity["activity_id"],
        AttemptInput(assistance_mode="independent", idempotency_key="help"),
    )
    result = app.learning.submit_attempt(
        actor,
        attempt["attempt_id"],
        AttemptSubmission(response="本题解释", external_help=help_declaration),
    )
    assert result["assistance_mode"] == expected


@pytest.mark.asyncio
async def test_frozen_rubric_corrections_adjustments_and_review_suggestions(env):
    app, actor = env["app"], env["actor"]
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan)
    attempt = app.learning.start_attempt(
        actor, activity["activity_id"], AttemptInput(idempotency_key="observe")
    )
    app.learning.submit_attempt(
        actor, attempt["attempt_id"], AttemptSubmission(response="本题解释")
    )
    value = observation_for(activity, outcome="not_yet")
    invalid = value.model_copy(deep=True)
    invalid.rubric_results[0].rubric_item_id = "invented"
    with pytest.raises(DomainError) as exc:
        app.learning.record_observation(actor, attempt["attempt_id"], invalid)
    assert exc.value.code == "RUBRIC_FROZEN"
    observed = app.learning.record_observation(actor, attempt["attempt_id"], value)
    adjustment = app.learning.propose_adjustment(
        actor,
        plan["plan_id"],
        AdjustmentInput(
            expected_revision=2,
            observation_ids=[observed["observation_id"]],
            strategy="smaller_steps",
            reason="本题条件解释遗漏",
        ),
    )
    assert adjustment["active_plan_unchanged"]
    assert app.learning.detail(actor, plan["plan_id"])["active_version_id"] == plan["version_id"]
    corrected = app.learning.record_observation(
        actor,
        attempt["attempt_id"],
        observation_for(activity, supersedes=observed["observation_id"]),
    )
    assert corrected["assessment"] == "self_reported_under_declared_conditions"
    with pytest.raises(DomainError) as exc:
        app.learning.accept_plan(actor, plan["plan_id"], adjustment["version_id"], 2)
    assert exc.value.code == "ADJUSTMENT_EVIDENCE"
    new = app.learning.propose_adjustment(
        actor,
        plan["plan_id"],
        AdjustmentInput(
            expected_revision=2,
            observation_ids=[corrected["observation_id"]],
            strategy="independent_variant",
            reason="本人希望验证独立条件",
        ),
    )
    app.learning.accept_plan(actor, plan["plan_id"], new["version_id"], 2)
    suggestions = app.learning.review_suggestions(actor)
    assert (
        suggestions["suggestions"]
        and not suggestions["reminder_sent"]
        and not suggestions["automation_created"]
    )
    app.learning.withdraw_observation(actor, corrected["observation_id"], 1)
    assert app.learning.review_suggestions(actor)["suggestions"] == []


@pytest.mark.asyncio
async def test_only_separate_authorized_human_can_make_scoped_observation(env):
    app, actor = env["app"], env["actor"]
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan, kind="transfer")
    attempt = app.learning.start_attempt(
        actor, activity["activity_id"], AttemptInput(idempotency_key="human")
    )
    app.learning.submit_attempt(
        actor,
        attempt["attempt_id"],
        AttemptSubmission(response="本题解释", transfer_notes="变体需要重新核验"),
    )
    with pytest.raises(DomainError):
        app.learning.record_observation(
            actor, attempt["attempt_id"], observation_for(activity, evaluator="human_review")
        )
    with app.db.transaction() as db:
        reviewer, _, _ = create_principal(db, "learning-reviewer")
        reviewer_id = reviewer.id
    with pytest.raises(DomainError):
        app.learning.record_observation(
            reviewer_id, attempt["attempt_id"], observation_for(activity, evaluator="human_review")
        )
    with app.db.transaction() as db:
        set_grant(db, actor, env["ns"], reviewer_id, "writer", True)
    result = app.learning.record_observation(
        reviewer_id, attempt["attempt_id"], observation_for(activity, evaluator="human_review")
    )
    assert result["assessment"] == "transfer_passed_under_conditions"
    assert result["hypothesis_status"] == "observed" and not result["competence_verified"]


@pytest.mark.asyncio
async def test_runtime_success_and_self_report_do_not_fill_frozen_runtime_rubric(env):
    app, actor = env["app"], env["actor"]
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan, kind="exercise", rubric_keys=["runtime", "conditions"])
    attempt = app.learning.start_attempt(
        actor, activity["activity_id"], AttemptInput(idempotency_key="runtime")
    )
    app.learning.submit_attempt(
        actor, attempt["attempt_id"], AttemptSubmission(response="本题解释：我说 exit_code=0")
    )
    with pytest.raises(DomainError) as exc:
        app.learning.record_observation(actor, attempt["attempt_id"], observation_for(activity))
    assert exc.value.code == "RUNTIME_EVIDENCE"


@pytest.mark.asyncio
async def test_activity_revocation_and_spec_tamper_fail_closed(env):
    app, actor = env["app"], env["actor"]
    pub, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan)
    with app.db.transaction() as db:
        row = db.get(LearningActivity, activity["activity_id"])
        row.specification = {**row.specification, "competence_verified": True}
    with pytest.raises(DomainError) as exc:
        app.learning.activity_detail(actor, activity["activity_id"])
    assert exc.value.code == "ACTIVITY_HASH"
    with app.db.transaction() as db:
        row = db.get(LearningActivity, activity["activity_id"])
        row.specification = db.get(ObjectRow, row.id).payload
        invalidate(db, actor, pub, "revoked", "source withdrawn")
    with pytest.raises(DomainError):
        app.learning.start_attempt(
            actor, activity["activity_id"], AttemptInput(idempotency_key="revoked")
        )


@pytest.mark.asyncio
async def test_learning_routes_end_to_end_and_other_actor_isolation(env):
    app = env["app"]
    _, plan = await accepted_plan(env, personalized=False)
    api = create_app(app)
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        profile = await client.get("/api/v1/learning/profile")
        assert profile.status_code == 200
        created = await client.post(
            "/api/v1/learning/activities", json={"module_id": plan["module_ids"][0]}
        )
        assert created.status_code == 201, created.text
        activity_id = created.json()["activity_id"]
        response = await client.post(
            f"/api/v1/learning/activities/{activity_id}/attempts",
            json={"idempotency_key": "api-attempt"},
        )
        assert response.status_code == 201
        attempt_id = response.json()["attempt_id"]
        submitted = await client.post(
            f"/api/v1/learning/attempts/{attempt_id}/submit", json={"response": "本题解释"}
        )
        assert submitted.status_code == 200 and not submitted.json()["competence_verified"]
        detail = await client.get(f"/api/v1/learning/attempts/{attempt_id}")
        assert detail.status_code == 200
        with app.db.transaction() as db:
            _, _, other_token = create_principal(db, "other-learner")
        hidden = await client.get(
            f"/api/v1/learning/attempts/{attempt_id}",
            headers={"Authorization": "Bearer " + other_token},
        )
        assert hidden.status_code == 404


@pytest.mark.asyncio
async def test_planner_egress_binds_complete_pack_including_impact_only_classifications(
    env, monkeypatch
):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    configure_planner(env)
    request = app.learning.start_personalized_plan(actor, plan_input(env, publication))
    with app.db.read() as db:
        frozen = db.get(AgentRun, request["run_id"]).input_manifest
    captured = []
    original = app.gateway.call

    async def capture(*args, **kwargs):
        if args[4] == "learning_planner":
            captured.extend(kwargs["access_dependencies"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.gateway, "call", capture)
    await app.drain()
    assert app.runtime.stage(request["run_id"], "result") is not None
    expected = {frozen["selection_manifest_id"]}
    for pack in frozen["units"]:
        expected.update(app.knowledge.pack_dependencies(pack))
        assert {link["id"] for link in pack["topic_links"]} <= expected
        assert {facet["id"] for facet in pack["facets"]} <= expected
    assert set(captured) == expected
