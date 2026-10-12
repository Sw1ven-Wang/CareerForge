from copy import deepcopy
from typing import Any

from careerforge.infrastructure.llm.openai_compatible import parse_response
from careerforge.schemas.model_io import ModelRequest, ModelResult


class FakeModel:
    """Explicit synthetic provider: reuses the real adapter's response contract."""

    def __init__(self, response: dict[str, Any]):
        self._response = deepcopy(response)

    async def complete(self, request: ModelRequest) -> ModelResult:
        result = parse_response(self._response, structured=request.response_schema is not None)
        return result.model_copy(update={"provider": "fake", "synthetic": True})
