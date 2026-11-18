"""Additive scoped-review contract. Historical review/coverage schemas stay unchanged.

The nullable additions preserve the strict full-unit path for existing providers.
Missing additions NEVER authorize partial publication. New admissions select this
schema server-side; only actual independent role outputs can supply the additions.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field

from .contracts import Contract, CoverageOutput, ReviewOutput
from .contracts_research_v163 import ResearchReviewOutput

SCOPED_REVIEW_VERSION: Final = "claim-scoped-review-1.6.6"
SCOPED_PUBLICATION_VERSION = "claim-scoped-publication-1.6.6"
REVIEW_READ_COMPLETION = {"version": "review-requested-read-completion-1.6.6", "max_pages": 2}
REVIEW_READ_COMPLETION_V2 = {"version": "review-requested-read-completion-v2", "max_pages": 20}
TARGETED_REVIEW_CONTRACT: Final = "targeted-mechanism-review-v1"
MECHANISM_PUBLICATION_CONTRACT: Final = "reviewed-mechanism-edges-v1"
PARTIAL_TITLE = "已独立核验的部分事实"
PARTIAL_LIMITATIONS = (
    "仅限本固定来源提交与实际引用片段；没有执行验证。",
    "仅保留原版本中独立核验的部分事实；原标题、主题、维度和整体描述未随子集发布，不代表原任务已完成。",
)


class ReviewIssueScope(Contract):
    issue_index: int = Field(ge=0, le=99)
    scope: Literal["claim", "topic_link", "facet", "title", "unit"]
    target_ids: list[str] = Field(max_length=30)


class StandaloneClaimReview(Contract):
    contract_version: Literal["claim-scoped-review-1.6.6"] = SCOPED_REVIEW_VERSION
    unit_version_id: str
    content_hash: str
    render_hash: str
    standalone_claim_ids: list[str] = Field(max_length=12)
    issue_scopes: list[ReviewIssueScope] = Field(max_length=100)
    global_blocking_issues: list[str] = Field(max_length=20)


class ScopedReviewOutput(ReviewOutput):
    scope_review: StandaloneClaimReview | None = None


class ScopedResearchReviewOutput(ResearchReviewOutput):
    scope_review: StandaloneClaimReview | None = None


class ScopedElementCheck(Contract):
    element_id: str
    claim_id: str
    fact_atom_id: str
    fact_bound: bool
    conditions_preserved: bool
    standalone: bool
    reason: str = Field(min_length=1, max_length=2000)


class StandaloneCoverageAudit(Contract):
    contract_version: Literal["claim-scoped-review-1.6.6"] = SCOPED_REVIEW_VERSION
    unit_version_id: str
    content_hash: str
    render_hash: str
    reviewer_output_hash: str
    elements: list[ScopedElementCheck] = Field(min_length=1, max_length=12)
    global_blocking_issues: list[str] = Field(max_length=20)


class ScopedCoverageOutput(CoverageOutput):
    scope_audit: StandaloneCoverageAudit | None = None


class AnnotationApproval(Contract):
    annotation_id: str
    claim_ids: list[str] = Field(min_length=1, max_length=12)
    independent_read_ids: list[str] = Field(min_length=1, max_length=9)


class RelationAudit(Contract):
    # Indices only refer to the immutable Reviewer's typed local issues.
    confirmed_local_issue_indices: list[int] = Field(max_length=100)
    topic_links: list[AnnotationApproval] = Field(max_length=15)
    facets: list[AnnotationApproval] = Field(max_length=30)


class RelationScopedCoverageOutput(ScopedCoverageOutput):
    relation_audit: RelationAudit


class MechanismApproval(Contract):
    edge_key: str
    claim_id: str


class MechanismCoverageOutput(RelationScopedCoverageOutput):
    approved_mechanism_edges: list[MechanismApproval] = Field(max_length=3)
