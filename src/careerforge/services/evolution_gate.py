"""Pure engineering gate simulation. This module cannot write a policy pointer.

Aggregate fixture counts do not establish paired statistical improvement, an
independent validation cohort, an actor's real authorization or a frozen model
candidate. Only EvolutionService's server-generated replay can supply those.
"""

from typing import Any

from ..contracts_evolution_v166 import GateSimulationInput
from ..util import fingerprint

GATE_SIMULATION_VERSION = "promotion-gate-simulation-1.6.6"


def evaluate_gate_simulation(value: GateSimulationInput | dict[str, Any]) -> dict[str, Any]:
    value = GateSimulationInput.model_validate(value)
    baseline, candidate = value.simulated_baseline, value.simulated_candidate
    safety = not any(
        (arm.security_violation or arm.unsupported_facts) for arm in (baseline, candidate)
    )
    setup = value.matched_setup and baseline.total == candidate.total
    before, after = baseline.correct / baseline.total, candidate.correct / candidate.total
    reasons = []
    if not value.authorization_valid:
        reasons.append("authorization_invalid")
    if not safety:
        reasons.append("hard_safety_violation")
    if not setup:
        reasons.append("unmatched_setup")
    if not value.holdout_attested:
        reasons.append("independent_holdout_missing")
    if not value.authorization_valid or not safety:
        decision = "failed"
    elif not setup or not value.holdout_attested:
        decision = "inconclusive"
    elif after < before:
        decision = "failed"
        reasons.append("quality_regression")
    elif after == before:
        decision = "inconclusive"
        reasons.append("improvement_not_established")
    else:
        decision = "passed"
        reasons.append("synthetic_gate_invariants_satisfied")
    return {
        "candidate_id": value.candidate_id,
        "baseline_metrics": baseline.model_dump(mode="json"),
        "candidate_metrics": candidate.model_dump(mode="json"),
        "same_setup": setup,
        "heldout_attestation": value.holdout_attested,
        "safety_gate": safety,
        "authorization_valid": value.authorization_valid,
        "decision": decision,
        "reason_codes": reasons,
        "simulation_only": True,
        "evaluation_scope": value.fixture_kind,
        "evaluator_version": GATE_SIMULATION_VERSION,
        "simulation_input_hash": fingerprint(value.model_dump(mode="json")),
        "observed_model_calls": 0,
        "actual_model_effectiveness_validated": False,
        "promotion_authorized": False,
        "evaluation_id": None,
        "publish_or_rollback_event": None,
    }
