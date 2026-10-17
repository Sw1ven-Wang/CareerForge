"""Arguments for read-only investigation over one frozen, already imported archive."""

from typing import Literal

from pydantic import Field

from careerforge.schemas.resources import InputModel


class TreeArgs(InputModel):
    prefix: str = Field(default="", max_length=1000)


class QueryArgs(InputModel):
    query: str = Field(min_length=1, max_length=200)


class TargetArgs(InputModel):
    target_id: str = Field(min_length=1, max_length=100)


class SpanArgs(TargetArgs):
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class FinishArgs(InputModel):
    reason: Literal["evidence_sufficient", "unsupported"]
    explanation: str = Field(min_length=1, max_length=500)
