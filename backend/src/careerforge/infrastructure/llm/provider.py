from typing import Any, Literal, Protocol

from careerforge.schemas.model_io import ModelRequest, ModelResult, Usage

ErrorCode = Literal[
    "authentication",
    "rate_limited",
    "timeout",
    "unavailable",
    "invalid_response",
    "rejected",
    "output_limit",
    "invalid_structured_json",
]


class ModelError(Exception):
    """Safe classifications and numeric audit data; never raw messages or requests."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        retryable: bool,
        usage_unknown: bool = True,
        usage: Usage | None = None,
        diagnostics: dict[str, Any] | None = None,
    ):
        self.usage = usage
        self.diagnostics = diagnostics
        self.code = code
        self.retryable = retryable
        self.usage_unknown = usage_unknown
        super().__init__(f"Model call failed: {code}")


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResult: ...
