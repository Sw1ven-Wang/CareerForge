"""Authenticated, explicit module acknowledgment; never a mastery declaration."""

from typing import Literal

from pydantic import Field, StrictBool

from .contracts import Contract


class SessionModuleAckInput(Contract):
    learning_session_id: str = Field(min_length=1, max_length=180)
    turn_id: str = Field(min_length=1, max_length=180)
    namespace_id: str = Field(min_length=1, max_length=180)
    plan_id: str = Field(min_length=1, max_length=180)
    version_id: str = Field(min_length=1, max_length=180)
    module_id: str = Field(min_length=1, max_length=180)
    expected_plan_revision: int = Field(ge=1, strict=True)
    expected_profile_revision: int = Field(ge=1, strict=True)
    binding_snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    completion_assessment_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    delivery_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm: Literal["ACKNOWLEDGE_MODULE_AND_ADVANCE"]
    accept_incomplete_material: StrictBool = False
    attempt_id: str | None = Field(default=None, min_length=1, max_length=180)
    idempotency_key: str = Field(min_length=1, max_length=150)
