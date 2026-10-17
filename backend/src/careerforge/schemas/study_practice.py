"""Explicit user choices for a frozen module exercise and its assistance provenance."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from careerforge.schemas.resources import InputModel

HelpMode = Literal["agent_assisted", "independent"]


class ModuleAttemptInput(InputModel):
    expected_revision: int = Field(ge=1)
    plan_id: UUID
    module_key: str = Field(min_length=1, max_length=40)
    instruction: str = Field(min_length=1, max_length=4000)
    acceptance: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        min_length=1, max_length=6
    )
    exercise_kind: Literal["practice", "transfer"] = "practice"
    variation: str = Field(default="", max_length=2000)
    help_mode: HelpMode = "agent_assisted"
    scope_acknowledged: Literal[True]

    @model_validator(mode="after")
    def meaningful(self) -> Self:
        if not self.instruction.strip() or any(not x.strip() for x in self.acceptance):
            raise ValueError("练习与验收条件不能为空")
        if len("；".join(self.acceptance)) > 4000:
            raise ValueError("验收条件合计最多4000字符")
        if self.exercise_kind == "transfer" and not self.variation.strip():
            raise ValueError("迁移练习须描述与参考实现不同的输入、接口或失败条件")
        return self


class AttemptCloseInput(InputModel):
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
