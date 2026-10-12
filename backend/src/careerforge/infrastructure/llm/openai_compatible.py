import json
from typing import Any

import httpx
from pydantic import ValidationError

from careerforge.infrastructure.llm.provider import ModelError
from careerforge.schemas.model_io import ModelRequest, ModelResult, ToolCall, Usage


def parse_response(payload: dict[str, Any], *, structured: bool = False) -> ModelResult:
    try:
        choice = payload["choices"][0]
        message = choice["message"]
        calls = []
        for call in message.get("tool_calls") or []:
            arguments = json.loads(call["function"]["arguments"])
            calls.append(
                ToolCall(id=call["id"], name=call["function"]["name"], arguments=arguments)
            )
        raw_usage = payload.get("usage") or {}
        input_tokens = raw_usage.get("prompt_tokens")
        output_tokens = raw_usage.get("completion_tokens")
        usage = Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            status="known" if input_tokens is not None and output_tokens is not None else "unknown",
        )
        text = message.get("content") or ""
        value = json.loads(text) if structured else None
        return ModelResult(
            provider="openai_compatible",
            request_id=payload.get("id"),
            text=text,
            tool_calls=calls,
            structured=value,
            finish_reason=choice["finish_reason"],
            usage=usage,
        )
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, ValidationError):
        raise ModelError("invalid_response", retryable=False) from None


def parse_stream(lines: list[str]) -> ModelResult:
    """Parse a frozen SSE fixture; a dropped stream must never look like a complete result."""
    chunks: list[str] = []
    tools: dict[int, dict[str, Any]] = {}
    finish = None
    usage = None
    request_id = None
    done = False
    try:
        for line in lines:
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                break
            event = json.loads(data)
            request_id = event.get("id", request_id)
            if event.get("usage") is not None:
                usage = event["usage"]
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                chunks.append(delta.get("content") or "")
                finish = choice.get("finish_reason") or finish
                for call in delta.get("tool_calls") or []:
                    entry = tools.setdefault(
                        call["index"],
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    entry["id"] = call.get("id") or entry["id"]
                    function = call.get("function", {})
                    entry["function"]["name"] += function.get("name", "")
                    entry["function"]["arguments"] += function.get("arguments", "")
        if not done or finish is None:
            raise ValueError("Incomplete stream")
        return parse_response(
            {
                "id": request_id,
                "usage": usage,
                "choices": [
                    {
                        "finish_reason": finish,
                        "message": {
                            "content": "".join(chunks),
                            "tool_calls": [tools[i] for i in sorted(tools)],
                        },
                    }
                ],
            }
        )
    except (KeyError, IndexError, TypeError, ValueError):
        raise ModelError("invalid_response", retryable=False) from None


class OpenAICompatibleProvider:
    """Single adapter. Construction never sends requests; no automatic paid retries."""

    def __init__(
        self, *, base_url: str, api_key: str, client: httpx.AsyncClient, timeout_seconds: float = 30
    ):
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def complete(self, request: ModelRequest) -> ModelResult:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": "user", "content": request.prompt}],
            (
                "max_completion_tokens" if request.reasoning_effort else "max_tokens"
            ): request.max_output_tokens,
        }
        if request.reasoning_effort is not None:
            body["reasoning_effort"] = request.reasoning_effort
        if request.tools:
            body["tools"] = request.tools
        if request.response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "result",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        try:
            response = await self._client.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=httpx.Timeout(self._timeout_seconds, connect=5),
            )
        except httpx.TimeoutException:
            raise ModelError("timeout", retryable=True) from None
        except httpx.RequestError:
            raise ModelError("unavailable", retryable=True) from None
        if response.status_code in (401, 403):
            raise ModelError("authentication", retryable=False) from None
        if response.status_code == 429:
            raise ModelError("rate_limited", retryable=True) from None
        if response.status_code >= 500:
            raise ModelError("unavailable", retryable=True) from None
        if response.is_error:
            raise ModelError("rejected", retryable=False) from None
        try:
            payload = response.json()
        except ValueError:
            raise ModelError("invalid_response", retryable=False) from None
        return parse_response(payload, structured=request.response_schema is not None)
