"""Provisional model assessment cannot impersonate runtime or human evidence."""

from copy import deepcopy

import pytest
from sqlalchemy import select
from test_learning_loop_v16 import accepted_plan, create_check

from careerforge.contracts import ModelOutput
from careerforge.contracts_assessment_v166 import AssessmentInput
from careerforge.contracts_learning_ext import AttemptInput, AttemptSubmission
from careerforge.errors import DomainError
from careerforge.models import AgentRun, ModelReceipt
from careerforge.models_learning_ext import LearningAttempt, LearningObservationRecord
from careerforge.services.dependencies import invalidate


async def submitted(env, runtime=False):
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(
        env,
        plan,
        kind="exercise",
        rubric_keys=["conditions", "runtime"] if runtime else ["conditions"],
    )
    attempt = env["app"].learning.start_attempt(
        env["actor"],
        activity["activity_id"],
        AttemptInput(assistance_mode="assisted", idempotency_key="attempt"),
    )
    env["app"].learning.submit_attempt(
        env["actor"],
        attempt["attempt_id"],
        AttemptSubmission(
            response="函数返回传入的 value。我说已运行，但未附回执。", external_help="used"
        ),
    )
    return activity, attempt


def setup_model(env, mutate=None):
    def answer(request):
        c = request["context"]
        value = {
            "rubric_items": [
                {
                    "rubric_item_id": r["id"],
                    "outcome": "demonstrated",
                    "submission_quote": "函数返回传入的 value。",
                    "fact_atom_ids": [c["facts"][0]["fact_atom_id"]],
                    "explanation": "候选解释，待复核。",
                }
                for r in c["rubric"]
            ],
            "limitations": ["模型判断不是能力认证。"],
        }
        if mutate:
            mutate(value)
        return ModelOutput(structured=value, synthetic=True)

    env["model"].overrides["evaluation"] = answer


def start(env, attempt):
    return env["app"].assessments.start(
        env["actor"],
        AssessmentInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            attempt_id=attempt["attempt_id"],
            idempotency_key="assessment",
        ),
    )["run_id"]


async def test_assessment_is_candidate_without_human_or_mastery_claim(env):
    _, attempt = await submitted(env, runtime=True)
    setup_model(env)
    identifier = start(env, attempt)
    await env["app"].drain()
    value = env["app"].runtime.stage(identifier, "result")
    assert value["evaluator_kind"] == "model_assessment_candidate"
    assert value["help_conditions"]["assistance_mode"] == "assisted"
    assert not value["competence_verified"] and not value["human_observation_created"]
    runtime = next(r for r in value["rubric_item_decisions"] if r["rubric_key"] == "runtime")
    assert runtime["model_proposed_outcome"] == "demonstrated" and runtime["outcome"] == "unknown"
    assert runtime["guard_reason_codes"] == ["runtime_receipt_missing"]
    with env["app"].db.read() as db:
        assert db.scalar(select(LearningObservationRecord)) is None
        assert (
            env["app"].assessments.validate_result(
                db, env["actor"], db.get(AgentRun, identifier), value
            )
            == value
        )


@pytest.mark.parametrize(
    "violation", ["unknown_fact", "changed_quote", "duplicate_rubric", "missing_fact"]
)
async def test_invalid_assessment_never_commits(env, violation):
    _, attempt = await submitted(env)

    def corrupt(value):
        item = value["rubric_items"][0]
        if violation == "unknown_fact":
            item["fact_atom_ids"] = ["outside"]
        if violation == "changed_quote":
            item["submission_quote"] = "not actually submitted"
        if violation == "duplicate_rubric":
            value["rubric_items"].append(dict(item))
        if violation == "missing_fact":
            item["fact_atom_ids"] = []

    setup_model(env, corrupt)
    identifier = start(env, attempt)
    await env["app"].drain()
    assert env["app"].runtime.stage(identifier, "result") is None
    with env["app"].db.read() as db:
        assert db.get(AgentRun, identifier).status == "failed"


async def test_changed_submission_blocks_assessment_before_model(env):
    _, attempt = await submitted(env)
    setup_model(env)
    identifier = start(env, attempt)
    with env["app"].db.transaction() as db:
        db.get(LearningAttempt, attempt["attempt_id"]).response = "changed"
    await env["app"].drain()
    assert not [r for r in env["model"].calls if r["role"] == "evaluation"]
    assert env["app"].runtime.stage(identifier, "result") is None


async def test_result_requires_retained_model_receipt(env):
    _, attempt = await submitted(env)
    setup_model(env)
    identifier = start(env, attempt)
    await env["app"].drain()
    value = env["app"].runtime.stage(identifier, "result")
    with env["app"].db.transaction() as db:
        db.get(ModelReceipt, value["model_call_receipt_id"]).response = None
    with env["app"].db.read() as db, pytest.raises(DomainError) as failed:
        env["app"].assessments.validate_result(
            db, env["actor"], db.get(AgentRun, identifier), value
        )
    assert failed.value.code == "ASSESSMENT_RECEIPT"


async def test_result_cannot_be_edited_to_claim_human_review(env):
    _, attempt = await submitted(env)
    setup_model(env)
    identifier = start(env, attempt)
    await env["app"].drain()
    value = deepcopy(env["app"].runtime.stage(identifier, "result"))
    value["evaluator_kind"] = "human_review"
    with env["app"].db.read() as db, pytest.raises(DomainError) as failed:
        env["app"].assessments.validate_result(
            db, env["actor"], db.get(AgentRun, identifier), value
        )
    assert failed.value.code == "ASSESSMENT_RESULT_HASH"


async def test_revocation_after_queue_prevents_model_call(env):
    activity, attempt = await submitted(env)
    setup_model(env)
    identifier = start(env, attempt)
    with env["app"].db.transaction() as db:
        invalidate(
            db, env["actor"], activity["specification"]["source_set_ids"][0], "revoked", "test"
        )
    await env["app"].drain()
    assert not [r for r in env["model"].calls if r["role"] == "evaluation"]
    assert env["app"].runtime.stage(identifier, "result") is None


async def submitted_with_fixture_receipt(env):
    from test_coding import FixtureSandbox

    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan, kind="exercise", rubric_keys=["conditions", "runtime"])
    app, actor = env["app"], env["actor"]
    attempt = app.learning.start_attempt(
        actor,
        activity["activity_id"],
        AttemptInput(assistance_mode="assisted", idempotency_key="fixture-attempt"),
    )
    wid = attempt["workspace_id"]
    app.coding.save(actor, wid, 1, "answer.py", "print('fixture only')\n")
    proposed = app.coding.propose_command(actor, wid, 2, ["python", "answer.py"])
    app.coding.confirm_command(actor, proposed["command_id"], proposed["approval_nonce"], "RUN")
    assert app.coding.process_commands(FixtureSandbox()) == 1
    sub = app.coding.submit(
        actor, wid, 2, "函数返回传入的 value。", "仅夹具。", "used", "fixture-code"
    )
    app.learning.submit_attempt(
        actor,
        attempt["attempt_id"],
        AttemptSubmission(
            response="函数返回传入的 value。",
            external_help="used",
            coding_submission_id=sub["submission_id"],
        ),
    )
    return activity, attempt, proposed, sub


async def test_synthetic_runtime_receipt_is_not_actual_execution(env):
    activity, attempt, _, _ = await submitted_with_fixture_receipt(env)
    setup_model(env)
    identifier = start(env, attempt)
    await env["app"].drain()
    value = env["app"].runtime.stage(identifier, "result")
    assert value is not None
    assert value["runtime_evidence"][0]["synthetic"] is True
    assert value["runtime_evidence"][0]["runtime_observed_not_mastery"] is False
    assert (
        next(r for r in value["rubric_item_decisions"] if r["rubric_key"] == "runtime")["outcome"]
        == "unknown"
    )
    request = next(r for r in env["model"].calls if r["role"] == "evaluation")
    assert request["context"]["prompt"] == activity["specification"]["prompt"]
    assert request["context"]["source_citations"] == activity["specification"]["citations"]


@pytest.mark.parametrize("damage", ["receipt_digest", "submission_revoked"])
async def test_changed_runtime_lineage_blocks_before_model(env, damage):
    from careerforge.models import CommandAuthorization, Submission

    _, attempt, proposed, submission = await submitted_with_fixture_receipt(env)
    setup_model(env)
    identifier = start(env, attempt)
    with env["app"].db.transaction() as db:
        if damage == "receipt_digest":
            row = db.get(CommandAuthorization, proposed["command_id"])
            row.receipt = {**row.receipt, "authorization_digest": "changed"}
        else:
            db.get(Submission, submission["submission_id"]).revoked = True
    await env["app"].drain()
    assert not [r for r in env["model"].calls if r["role"] == "evaluation"]
    assert env["app"].runtime.stage(identifier, "result") is None


async def test_pending_model_assessment_blocks_new_independent_attempt(env):
    activity, attempt = await submitted(env)
    setup_model(env)
    start(env, attempt)
    with pytest.raises(DomainError):
        env["app"].learning.start_attempt(
            env["actor"],
            activity["activity_id"],
            AttemptInput(assistance_mode="independent", idempotency_key="no-overlap"),
        )


async def test_repeated_assessment_request_is_idempotent(env):
    _, attempt = await submitted(env)
    setup_model(env)
    identifier = start(env, attempt)
    assert start(env, attempt) == identifier
    await env["app"].drain()
    assert start(env, attempt) == identifier
    assert len([r for r in env["model"].calls if r["role"] == "evaluation"]) == 1
