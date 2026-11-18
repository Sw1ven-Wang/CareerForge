"""Release safety regressions use invented policies and synthetic observations only."""

from copy import deepcopy

import pytest
from sqlalchemy import select
from test_evolution import candidate, dataset
from test_evolution_replay_v16 import replay_data, run_replay, semantic_candidate

from careerforge.errors import DomainError
from careerforge.models import (
    EvolutionCandidate,
    EvolutionEvaluation,
    Feedback,
    Outbox,
    PolicyHead,
)
from careerforge.services.evolution import paired_report
from careerforge.services.policies import ensure_policies


def samples():
    return [
        {"case_id": f"example-{i}", "group_id": f"family-{i}", "baseline": 0.2, "candidate": 0.8}
        for i in range(4)
    ]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.1, True, "0.9"])
def test_paired_report_rejects_invalid_quality_values(bad):
    values = samples()
    values[0]["candidate"] = bad
    with pytest.raises(DomainError) as error:
        paired_report(values, 4, 2)
    assert error.value.code == "EVALUATION_METRICS"


@pytest.mark.parametrize("bad", [-1, 0.5, True, "0"])
def test_safety_counts_are_nonnegative_integers_and_cannot_cancel(bad):
    values = samples()
    values[0]["safety_violations"] = 1
    values[1]["safety_violations"] = bad
    with pytest.raises(DomainError) as error:
        paired_report(values, 4, 2)
    assert error.value.code == "EVALUATION_METRICS"


def test_duplicate_case_ids_cannot_manufacture_sample_size():
    values = samples()
    values[1]["case_id"] = values[0]["case_id"]
    with pytest.raises(DomainError) as error:
        paired_report(values, 4, 2)
    assert error.value.code == "EVALUATION_METRICS"


@pytest.mark.asyncio
async def test_second_rollback_never_reactivates_the_rejected_candidate(env):
    app, actor = env["app"], env["actor"]
    identifier, base, _, _, _ = await candidate(env)
    report = await app.evolution.evaluate(actor, identifier, dataset(env))
    app.evolution.promote(actor, identifier, report["evaluation_id"], 1)
    app.evolution.rollback(actor, env["ns"], "teaching_skill", 2)
    with pytest.raises(DomainError) as error:
        app.evolution.rollback(actor, env["ns"], "teaching_skill", 3)
    assert error.value.code == "ROLLBACK_CONFLICT"
    with app.db.read() as db:
        head = db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill"))
        assert head.policy_id == base and head.revision == 3
        assert db.get(EvolutionCandidate, identifier).status == "rolled_back"


@pytest.mark.asyncio
async def test_rollback_rejects_a_valid_hash_from_the_wrong_policy_target(env):
    app, actor = env["app"], env["actor"]
    identifier, _, _, _, _ = await candidate(env)
    report = await app.evolution.evaluate(actor, identifier, dataset(env))
    promoted = app.evolution.promote(actor, identifier, report["evaluation_id"], 1)
    with app.db.transaction() as db:
        wrong = ensure_policies(db, actor, env["ns"])["routing_policy"]["policy_id"]
        head = db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill"))
        head.previous_policy_id = wrong
    with pytest.raises(DomainError) as error:
        app.evolution.rollback(actor, env["ns"], "teaching_skill", 2)
    assert error.value.code == "ROLLBACK_CONFLICT"
    with app.db.read() as db:
        head = db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill"))
        assert head.policy_id == promoted["policy_id"] and head.revision == 2
        assert not [
            r for r in db.scalars(select(Outbox)) if r.payload.get("event") == "policy.rolled_back"
        ]


@pytest.mark.asyncio
async def test_rollback_checks_live_consent_of_the_policy_it_would_restore(env):
    app, actor = env["app"], env["actor"]
    first, _, _, _, feedback = await candidate(env)
    report = await app.evolution.evaluate(actor, first, dataset(env))
    one = app.evolution.promote(actor, first, report["evaluation_id"], 1)
    second, _, _, _, _ = await candidate(env, order="comparison_first")
    report = await app.evolution.evaluate(
        actor, second, dataset(env, order="comparison_first", name="later")
    )
    two = app.evolution.promote(actor, second, report["evaluation_id"], one["head_revision"])
    with app.db.transaction() as db:
        # The durable consent fence can commit before asynchronous cleanup.
        db.get(Feedback, feedback["feedback_id"]).consent_revision = 0
    with pytest.raises(DomainError) as error:
        app.evolution.rollback(actor, env["ns"], "teaching_skill", two["head_revision"])
    assert error.value.code == "EXPERIENCE_CONSENT"
    with app.db.read() as db:
        head = db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill"))
        assert head.policy_id == two["policy_id"] and head.revision == two["head_revision"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["hard_safety", "summary", "samples", "simulation"])
async def test_release_cannot_trust_a_passed_label_over_conflicting_evidence(env, mutation):
    app, actor = env["app"], env["actor"]
    identifier, base, _, _, _ = await candidate(env)
    report = await app.evolution.evaluate(actor, identifier, dataset(env))
    with app.db.transaction() as db:
        row = db.get(EvolutionEvaluation, report["evaluation_id"])
        value = deepcopy(row.report)
        if mutation == "hard_safety":
            value["safety_violations"] = 1
        elif mutation == "summary":
            value["candidate_score"] = 0.0
        elif mutation == "samples":
            value["samples"][0]["safety_violations"] = 1
        else:
            value["evaluation_scope"] = "synthetic_gate_invariant_not_actual_model_measurement"
        row.report = value
    with pytest.raises(DomainError) as error:
        app.evolution.promote(actor, identifier, report["evaluation_id"], 1)
    assert error.value.code == "PROMOTION_GATE"
    with app.db.read() as db:
        assert (
            db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill")).policy_id
            == base
        )


@pytest.mark.asyncio
async def test_replay_release_revalidates_receipts_not_only_trace_hashes(env):
    from careerforge.models import ModelReceipt

    app, actor = env["app"], env["actor"]
    identifier, _, _ = await semantic_candidate(env)
    data, _ = await replay_data(env)
    started, report = await run_replay(env, identifier, data)
    assert report["decision"] == "passed"
    with app.db.transaction() as db:
        receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == started["run_id"]))
        receipt.status, receipt.response = "redacted", None
    with pytest.raises(DomainError) as error:
        app.evolution.promote(actor, identifier, report["evaluation_id"], 1)
    assert error.value.code == "SEMANTIC_REPLAY_REQUIRED"


def simulated_gate():
    return {
        "candidate_id": "unseen-policy-name",
        "simulated_baseline": {
            "correct": 3,
            "total": 8,
            "security_violation": 0,
            "unsupported_facts": 0,
        },
        "simulated_candidate": {
            "correct": 6,
            "total": 8,
            "security_violation": 0,
            "unsupported_facts": 0,
        },
        "holdout_attested": True,
        "matched_setup": True,
        "authorization_valid": True,
    }


def test_simulation_pass_never_creates_actual_release_authority():
    from careerforge.services.evolution_gate import evaluate_gate_simulation

    result = evaluate_gate_simulation(simulated_gate())
    assert result["decision"] == "passed"
    assert result["simulation_only"] is True
    assert result["promotion_authorized"] is False
    assert result["actual_model_effectiveness_validated"] is False
    assert result["observed_model_calls"] == 0
    assert result["publish_or_rollback_event"] is None
    assert result["evaluation_id"] is None


@pytest.mark.parametrize(
    "field,decision,reason",
    [
        ("holdout_attested", "inconclusive", "independent_holdout_missing"),
        ("matched_setup", "inconclusive", "unmatched_setup"),
        ("authorization_valid", "failed", "authorization_invalid"),
    ],
)
def test_simulated_score_gain_cannot_replace_a_required_gate(field, decision, reason):
    from careerforge.services.evolution_gate import evaluate_gate_simulation

    value = simulated_gate()
    value[field] = False
    report = evaluate_gate_simulation(value)
    assert report["decision"] == decision and reason in report["reason_codes"]
    assert not report["promotion_authorized"]


@pytest.mark.parametrize("field", ["security_violation", "unsupported_facts"])
@pytest.mark.parametrize("arm", ["simulated_baseline", "simulated_candidate"])
def test_simulated_quality_cannot_offset_safety_violations(arm, field):
    from careerforge.services.evolution_gate import evaluate_gate_simulation

    value = simulated_gate()
    value[arm][field] = 1
    report = evaluate_gate_simulation(value)
    assert report["decision"] == "failed"
    assert report["safety_gate"] is False
    assert "hard_safety_violation" in report["reason_codes"]


def test_gate_decision_does_not_depend_on_dataset_or_candidate_identifiers():
    from careerforge.services.evolution_gate import evaluate_gate_simulation

    first, second = simulated_gate(), simulated_gate()
    first.update(case_id="arbitrary-one", candidate_id="one")
    second.update(case_id="not-in-any-dataset", candidate_id="unrelated-name")
    left, right = evaluate_gate_simulation(first), evaluate_gate_simulation(second)
    assert left["decision"] == right["decision"]
    assert left["reason_codes"] == right["reason_codes"]


@pytest.mark.parametrize("value", ["true", 1, None])
def test_simulation_attestation_flags_require_real_booleans(value):
    from pydantic import ValidationError

    from careerforge.services.evolution_gate import evaluate_gate_simulation

    inputs = simulated_gate()
    inputs["authorization_valid"] = value
    with pytest.raises(ValidationError):
        evaluate_gate_simulation(inputs)


def test_unequal_case_counts_cannot_claim_a_matched_simulated_setup():
    from careerforge.services.evolution_gate import evaluate_gate_simulation

    value = simulated_gate()
    value["simulated_candidate"]["total"] = 9
    result = evaluate_gate_simulation(value)
    assert result["decision"] == "inconclusive" and not result["same_setup"]


@pytest.mark.asyncio
async def test_rollback_requires_write_authority_and_keeps_pointer_on_failure(env):
    from careerforge.services.security import create_principal, set_grant

    app, actor = env["app"], env["actor"]
    identifier, _, _, _, _ = await candidate(env)
    report = await app.evolution.evaluate(actor, identifier, dataset(env))
    promoted = app.evolution.promote(actor, identifier, report["evaluation_id"], 1)
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "read-only observer")
        set_grant(db, actor, env["ns"], other.id, "reader", True)
    with pytest.raises(DomainError):
        app.evolution.rollback(other.id, env["ns"], "teaching_skill", 2)
    with app.db.read() as db:
        head = db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill"))
        assert head.policy_id == promoted["policy_id"] and head.revision == 2


@pytest.mark.asyncio
async def test_rollback_walks_verified_ancestry_and_preserves_frozen_turns(env):
    from careerforge.contracts import TurnInput
    from careerforge.models import Turn

    app, actor = env["app"], env["actor"]
    first, root, _, _, _ = await candidate(env)
    report = await app.evolution.evaluate(actor, first, dataset(env))
    one = app.evolution.promote(actor, first, report["evaluation_id"], 1)
    second, _, conversation, _, _ = await candidate(env, order="comparison_first")
    report = await app.evolution.evaluate(
        actor, second, dataset(env, order="comparison_first", name="later")
    )
    two = app.evolution.promote(actor, second, report["evaluation_id"], one["head_revision"])
    turn = app.conversations.submit(
        actor,
        conversation,
        TurnInput(
            question="Explain this path", expected_revision=2, idempotency_key="rollback-frozen"
        ),
    )
    assert (
        app.evolution.rollback(actor, env["ns"], "teaching_skill", two["head_revision"])[
            "policy_id"
        ]
        == one["policy_id"]
    )
    assert app.evolution.rollback(actor, env["ns"], "teaching_skill", 4)["policy_id"] == root
    with pytest.raises(DomainError):
        app.evolution.rollback(actor, env["ns"], "teaching_skill", 5)
    with app.db.read() as db:
        frozen = db.get(Turn, turn["turn_id"]).policy_versions["teaching_skill"]
        assert frozen["policy_id"] == two["policy_id"]
        assert db.get(EvolutionCandidate, first).status == "rolled_back"
        assert db.get(EvolutionCandidate, second).status == "rolled_back"


@pytest.mark.asyncio
async def test_competing_rollback_calls_have_one_atomic_pointer_change(env):
    from concurrent.futures import ThreadPoolExecutor

    app, actor = env["app"], env["actor"]
    identifier, base, _, _, _ = await candidate(env)
    report = await app.evolution.evaluate(actor, identifier, dataset(env))
    app.evolution.promote(actor, identifier, report["evaluation_id"], 1)

    def rollback(_):
        try:
            return app.evolution.rollback(actor, env["ns"], "teaching_skill", 2)["policy_id"]
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=3) as workers:
        results = list(workers.map(rollback, range(3)))
    assert results.count(base) == 1 and results.count("ROLLBACK_CONFLICT") == 2
    with app.db.read() as db:
        head = db.scalar(select(PolicyHead).where(PolicyHead.target == "teaching_skill"))
        assert head.policy_id == base and head.revision == 3
        events = [
            row
            for row in db.scalars(select(Outbox))
            if row.payload.get("event") == "policy.rolled_back"
        ]
        assert len(events) == 1
