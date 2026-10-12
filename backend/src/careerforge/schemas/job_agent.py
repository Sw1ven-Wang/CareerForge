from typing import Literal
from uuid import UUID

from pydantic import Field

from careerforge.schemas.job_contract import ContractModel, RequirementExpr


class ExtractionRunInput(ContractModel):
    job_snapshot_id: UUID


class AgentCandidate(ContractModel):
    section_id: str
    quote: str = Field(min_length=1, max_length=4000)
    requirement_kind: Literal[
        "technical_skill", "engineering_capability", "knowledge", "eligibility", "other"
    ]
    importance: Literal["required", "preferred", "responsibility", "unknown"]
    expression: RequirementExpr


class AgentExtraction(ContractModel):
    candidates: list[AgentCandidate] = Field(max_length=40)
    unresolved: list[str] = Field(max_length=40)
