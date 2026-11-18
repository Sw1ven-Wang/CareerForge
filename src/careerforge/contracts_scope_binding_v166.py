"""New-admission reviewer target shapes; legacy scoped schemas are unchanged.

These types expose the existing deterministic gate's scope/target shape rule.
They add no publication authority and never coerce or repair a model response.
"""

from typing import Final, Literal

from pydantic import Field, model_validator

from .contracts import Contract, MechanismReadTarget, ReviewOutput
from .contracts_research_v163 import ResearchReviewOutput
from .contracts_scoped_review_v166 import SCOPED_REVIEW_VERSION, ReviewIssueScope

REVIEW_SCOPE_BINDING_CONTRACT: Final = "review-scope-target-binding-1.6.6"
REVIEW_SCOPE_BINDING_FIELD: Final = "review_scope_binding_contract"


class LocalReviewIssueScope(ReviewIssueScope):
    scope: Literal["claim", "topic_link", "facet"]
    target_ids: list[str] = Field(
        min_length=1,
        max_length=30,
        description=(
            "Nonempty unique IDs from this candidate only: claim uses subject.claims[].claim_id; "
            "topic_link uses subject.topic_links[].id; facet uses subject.facets[].id."
        ),
    )


class GlobalReviewIssueScope(ReviewIssueScope):
    scope: Literal["title", "unit"]
    target_ids: list[str] = Field(
        max_length=0,
        description=(
            "Must be [] for title/unit. The candidate is already bound by "
            "scope_review.unit_version_id; do not repeat that ID here."
        ),
    )


# Disjoint literal scopes expose the conditional rule through anyOf, avoiding
# discriminator/oneOf keywords outside the provider's strict-schema subset.
BoundReviewIssueScope = LocalReviewIssueScope | GlobalReviewIssueScope


# Keep this DTO separate from the legacy mutable-list DTO. Narrowing an inherited
# list[ReviewIssueScope] is not type-safe. Field order and wire schema stay exact.
class BoundStandaloneClaimReview(Contract):
    contract_version: Literal["claim-scoped-review-1.6.6"] = SCOPED_REVIEW_VERSION
    unit_version_id: str
    content_hash: str
    render_hash: str
    standalone_claim_ids: list[str] = Field(max_length=12)
    issue_scopes: list[BoundReviewIssueScope] = Field(max_length=100)
    global_blocking_issues: list[str] = Field(max_length=20)


class BoundScopedReviewOutput(ReviewOutput):
    scope_review: BoundStandaloneClaimReview | None = None


class BoundScopedResearchReviewOutput(ResearchReviewOutput):
    scope_review: BoundStandaloneClaimReview | None = None


class EvidenceReadRequest(Contract):
    issue_index: int = Field(ge=0, le=99)
    target: MechanismReadTarget


class RetryableScopedResearchReviewOutput(BoundScopedResearchReviewOutput):
    evidence_read_requests: list[EvidenceReadRequest] = Field(max_length=4)


class EvidenceReadRange(MechanismReadTarget):
    """A proposal, not a tool read or evidence; paginated before execution."""

    @model_validator(mode="after")
    def bounded_window(self):
        if not 0 <= self.end_line - self.start_line < 120:
            raise ValueError("Evidence proposal ranges must contain 1 to 120 lines")
        return self


class EvidenceRangeRequest(Contract):
    issue_index: int = Field(ge=0, le=99)
    target: EvidenceReadRange


class RangeRetryableScopedResearchReviewOutput(BoundScopedResearchReviewOutput):
    evidence_read_requests: list[EvidenceRangeRequest] = Field(max_length=4)


def evidence_request_windows(request):
    """Preserve all proposed lines; the scheduler separately applies its budget."""
    target = request.target.model_dump(mode="json")
    return [
        {**target, "start_line": start, "end_line": min(start + 29, target["end_line"])}
        for start in range(target["start_line"], target["end_line"] + 1, 30)
    ]
