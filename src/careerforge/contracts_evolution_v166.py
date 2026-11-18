"""Simulation inputs are deliberately distinct from release evidence contracts."""

from typing import Literal

from pydantic import Field, StrictBool, model_validator

from .contracts import Contract


class SimulatedPolicyMetrics(Contract):
    correct: int = Field(ge=0, strict=True)
    total: int = Field(gt=0, strict=True)
    security_violation: int = Field(default=0, ge=0, strict=True)
    unsupported_facts: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def valid_counts(self) -> "SimulatedPolicyMetrics":
        if self.correct > self.total:
            raise ValueError("Correct count cannot exceed measured cases")
        return self


class GateSimulationInput(Contract):
    """Public engineering fixtures, never authorization to publish a policy."""

    candidate_id: str = Field(min_length=1, max_length=200)
    case_id: str | None = Field(default=None, min_length=1, max_length=200)
    simulated_baseline: SimulatedPolicyMetrics
    simulated_candidate: SimulatedPolicyMetrics
    holdout_attested: StrictBool
    matched_setup: StrictBool
    authorization_valid: StrictBool
    task: Literal["test_promotion_gate"] = "test_promotion_gate"
    fixture_kind: Literal["synthetic_gate_invariant_not_actual_model_measurement"] = (
        "synthetic_gate_invariant_not_actual_model_measurement"
    )
