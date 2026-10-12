import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from careerforge.schemas.resources import InputModel


class RepositoryInput(InputModel):
    github_url: str = Field(max_length=500)
    requested_ref: str = Field(min_length=1, max_length=200)

    @field_validator("github_url")
    @classmethod
    def public_github(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.netloc != "github.com" or parts.query or parts.fragment:
            raise ValueError("只支持公开 GitHub HTTPS 仓库地址")
        path = parts.path.rstrip("/").removesuffix(".git")
        if not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path) or any(
            p in {".", ".."} for p in path.split("/")
        ):
            raise ValueError("仓库路径无效")
        return "https://github.com" + path


class ProjectResearchInput(InputModel):
    objective_version_ids: list[UUID] = Field(min_length=1, max_length=3)
    candidate_repository_ids: list[UUID] = Field(default_factory=list, max_length=5)
    candidate_snapshot_ids: list[UUID] = Field(default_factory=list, max_length=5)
    search_mode: Literal["curated"] = "curated"
    feedback_ids: list[UUID] = Field(default_factory=list, max_length=20)
    budget_profile: Literal["development_small"] = "development_small"

    @model_validator(mode="after")
    def candidates(self) -> "ProjectResearchInput":
        if not self.candidate_repository_ids and not self.candidate_snapshot_ids:
            raise ValueError("至少提供一个候选")
        if len(self.candidate_repository_ids) + len(self.candidate_snapshot_ids) > 5:
            raise ValueError("最多五个候选")
        return self


class SelectionInput(InputModel):
    recommendation_id: UUID
    objective_version_ids: list[UUID] = Field(min_length=1, max_length=3)
    expected_goal_version_id: UUID


class RecommendationFeedback(InputModel):
    action: Literal["rejected"]
    reason_type: Literal["difficulty", "resources", "time", "interest", "other"]
    reason: str = Field(min_length=1, max_length=4000)
