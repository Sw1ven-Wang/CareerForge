"""Caller-only explicit Evolution commands; feedback never implies spending/release."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract
from .contracts_learning_ext import ReplayInput

Target = Literal["teaching_skill", "routing_policy", "retrieval_recipe", "research_heuristic"]


class CandidateRequest(Contract):
    type: Literal["candidate"]
    target: Target
    experience_ids: list[str] = Field(min_length=1, max_length=50)
    funding_id: str | None = None


class ReplayRequest(Contract):
    type: Literal["replay"]
    candidate_id: str
    replay: ReplayInput


class PromotionRequest(Contract):
    type: Literal["promote"]
    candidate_id: str
    evaluation_id: str
    expected_revision: int = Field(ge=1)
    confirm: Literal["PROMOTE"]


class RollbackRequest(Contract):
    type: Literal["rollback"]
    target: Target
    expected_revision: int = Field(ge=1)
    from_policy_id: str
    to_policy_id: str
    confirm: Literal["ROLLBACK"]


class AuthorizeRequest(Contract):
    type: Literal["authorize_candidate"]
    request_id: str
    request_hash: str = Field(min_length=64, max_length=64)
    funding_id: str


class RejectRequest(Contract):
    type: Literal["reject"]
    reason: str = Field(min_length=1, max_length=1000)


class EvolutionSessionInput(Contract):
    contract: Literal["evolution-dialogue-v1"]
    idempotency_key: str = Field(min_length=1, max_length=120)
    workflow_id: str | None = None
    parent_workflow_id: str | None = None
    expected_workflow_revision: int | None = Field(default=None, ge=1)
    intent: Annotated[
        CandidateRequest
        | ReplayRequest
        | PromotionRequest
        | RollbackRequest
        | AuthorizeRequest
        | RejectRequest,
        Field(discriminator="type"),
    ]

    @model_validator(mode="after")
    def scope(self):
        if self.workflow_id is None and self.intent.type not in {"candidate", "rollback"}:
            raise ValueError("A continuing command requires its exact Evolution workflow")
        if self.workflow_id is not None and (
            self.expected_workflow_revision is None or self.parent_workflow_id is not None
        ):
            raise ValueError("An existing workflow requires its revision and no new parent")
        if self.workflow_id is None and self.expected_workflow_revision is not None:
            raise ValueError("A new child has no existing revision")
        return self
