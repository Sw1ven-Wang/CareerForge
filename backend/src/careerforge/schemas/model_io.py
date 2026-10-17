from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Usage(ContractModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    status: Literal["known", "unknown"] = "unknown"

    @model_validator(mode="after")
    def check_known(self) -> "Usage":
        complete = self.input_tokens is not None and self.output_tokens is not None
        if (self.status == "known") != complete:
            raise ValueError("Usage status must agree with token availability")
        return self


class ToolCall(ContractModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class ModelResult(ContractModel):
    schema_version: Literal[1] = 1
    provider: str
    request_id: str | None = None
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    structured: dict[str, Any] | None = None
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"]
    usage: Usage = Field(default_factory=Usage)
    synthetic: bool = False
    diagnostics: dict[str, Any] | None = None


class ModelRequest(ContractModel):
    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=50000)
    max_output_tokens: int = Field(default=512, ge=1, le=8192)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    response_schema: dict[str, Any] | None = None
