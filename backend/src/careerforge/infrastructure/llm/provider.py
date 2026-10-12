from typing import Literal, Protocol

from careerforge.schemas.model_io import ModelRequest, ModelResult

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
    """Contains only safe classifications; never raw provider messages or requests."""

    def __init__(self, code: ErrorCode, *, retryable: bool, usage_unknown: bool = True):
        self.code = code
        self.retryable = retryable
        self.usage_unknown = usage_unknown
        super().__init__(f"Model call failed: {code}")


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResult: ...
