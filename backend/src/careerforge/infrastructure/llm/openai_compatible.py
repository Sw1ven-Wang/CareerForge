import hashlib
import json
import re
from typing import Any

import httpx
from pydantic import ValidationError

from careerforge.infrastructure.llm.provider import ErrorCode, ModelError
from careerforge.schemas.model_io import ModelRequest, ModelResult, ToolCall, Usage

# Numeric allowlist only: never retain arbitrary provider payloads or header values.
TOKEN_FIELDS = {
    "prompt_tokens",
    "completion_tokens",
    "input_tokens",
    "output_tokens",
    "total_tokens",
}
DETAIL_FIELDS = {
    "reasoning_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "accepted_prediction_tokens",
    "rejected_prediction_tokens",
    "audio_tokens",
    "text_tokens",
    "image_tokens",
}
DETAIL_GROUPS = {
    "prompt_tokens_details",
    "completion_tokens_details",
    "input_tokens_details",
    "output_tokens_details",
}


def safe_identity(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", value)
        else None
    )


def usage_diagnostics(payload: dict[str, Any]) -> tuple[Usage, dict[str, Any]]:
    raw = payload.get("usage")
    safe: dict[str, Any] = {}
    warnings = []
    if raw is not None and not isinstance(raw, dict):
        warnings.append("invalid_usage_object")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in TOKEN_FIELDS:
                if type(value) is int and value >= 0:
                    safe[key] = value
                else:
                    warnings.append("invalid_usage:" + key)
            elif key in DETAIL_GROUPS and isinstance(value, dict):
                safe[key] = {
                    k: v
                    for k, v in value.items()
                    if k in DETAIL_FIELDS and type(v) is int and v >= 0
                }
    input_tokens = safe.get("prompt_tokens")
    output_tokens = safe.get("completion_tokens")
    if input_tokens is None or output_tokens is None:
        warnings.append("chat_usage_incomplete")
    for canonical, alias in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
    ):
        if canonical in safe and alias in safe and safe[canonical] != safe[alias]:
            warnings.append("conflicting_usage_alias:" + alias)
    if input_tokens is not None and output_tokens is not None:
        total = safe.get("total_tokens")
        if total is not None and total != input_tokens + output_tokens:
            warnings.append("usage_total_mismatch")
    reasoning = safe.get("completion_tokens_details", {}).get("reasoning_tokens")
    if reasoning is not None and output_tokens is not None and reasoning > output_tokens:
        warnings.append("reasoning_exceeds_completion")
    usage = Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        status="known" if input_tokens is not None and output_tokens is not None else "unknown",
    )
    return usage, {
        "response": {
            "response_id": safe_identity(payload.get("id")),
            "model": safe_identity(payload.get("model")),
            "object": safe_identity(payload.get("object")),
        },
        "metering": {
            "basis": "chat_completion_total_including_reasoning_and_nonvisible",
            "input_field": "prompt_tokens",
            "output_field": "completion_tokens",
            "provider_usage": safe,
            "warnings": warnings,
        },
    }


def parse_response(payload: dict[str, Any], *, structured: bool = False) -> ModelResult:
    if not isinstance(payload, dict):
        raise ModelError("invalid_response", retryable=False)
    usage, diagnostics = usage_diagnostics(payload)
    try:
        if any(
            w == "invalid_usage_object"
            or w
            in {
                "invalid_usage:prompt_tokens",
                "invalid_usage:completion_tokens",
            }
            for w in diagnostics["metering"]["warnings"]
        ):
            raise ModelError("invalid_response", retryable=False)
        choice = payload["choices"][0]
        if structured and choice.get("finish_reason") == "length":
            raise ModelError("output_limit", retryable=False)
        message = choice["message"]
        calls = []
        for call in message.get("tool_calls") or []:
            arguments = json.loads(call["function"]["arguments"])
            calls.append(
                ToolCall(id=call["id"], name=call["function"]["name"], arguments=arguments)
            )
        text = message.get("content") or ""
        try:
            value = json.loads(text) if structured else None
        except (TypeError, ValueError):
            raise ModelError("invalid_structured_json", retryable=False) from None
        return ModelResult(
            provider="openai_compatible",
            request_id=payload.get("id"),
            text=text,
            tool_calls=calls,
            structured=value,
            finish_reason=choice["finish_reason"],
            usage=usage,
            diagnostics=diagnostics,
        )
    except ModelError as error:
        error.usage = usage
        error.usage_unknown = usage.status != "known"
        error.diagnostics = diagnostics
        raise
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, ValidationError):
        raise ModelError(
            "invalid_response",
            retryable=False,
            usage=usage,
            usage_unknown=usage.status != "known",
            diagnostics=diagnostics,
        ) from None


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
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._client = client
        self._timeout_seconds = timeout_seconds

    def _build_request(self, request: ModelRequest) -> httpx.Request:
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
        return self._client.build_request(
            "POST",
            self._url,
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(self._timeout_seconds, connect=5),
        )

    def _request_audit(self, wire: httpx.Request, request: ModelRequest) -> dict[str, Any]:
        body = json.loads(wire.content)
        cap_field = "max_completion_tokens" if request.reasoning_effort else "max_tokens"
        return {
            "request": {
                "protocol": "chat_completions",
                "endpoint_path": wire.url.path,
                "model": body["model"],
                "reasoning_effort": body.get("reasoning_effort"),
                "output_limit_field": cap_field,
                "output_limit": body[cap_field],
                "body_sha256": hashlib.sha256(wire.content).hexdigest(),
                "body_bytes": len(wire.content),
                "body_fields": sorted(body),
                "tool_count": len(body.get("tools", [])),
                "structured": "response_format" in body,
            }
        }

    def request_diagnostics(self, request: ModelRequest) -> dict[str, Any]:
        # The same HTTPX serialization is used before reservation and before send.
        # No request is sent, and neither Authorization nor message content is retained.
        return self._request_audit(self._build_request(request), request)

    async def complete(self, request: ModelRequest) -> ModelResult:
        wire = self._build_request(request)
        diagnostics = self._request_audit(wire, request)
        try:
            response = await self._client.send(wire, follow_redirects=False)
        except httpx.TimeoutException:
            raise ModelError("timeout", retryable=True, diagnostics=diagnostics) from None
        except httpx.RequestError:
            raise ModelError("unavailable", retryable=True, diagnostics=diagnostics) from None
        diagnostics["http"] = {
            "status_code": response.status_code,
            "request_id": safe_identity(response.headers.get("x-request-id")),
            "client_request_id": safe_identity(response.headers.get("x-client-request-id")),
        }
        try:
            payload = response.json()
        except ValueError:
            payload = None
        usage = None
        if isinstance(payload, dict):
            usage, response_diagnostics = usage_diagnostics(payload)
            diagnostics.update(response_diagnostics)
            output = usage.output_tokens
            diagnostics["output_budget"] = {
                "requested": request.max_output_tokens,
                "reported": output,
                "status": "unknown"
                if output is None
                else ("over_limit" if output > request.max_output_tokens else "within_limit"),
                "provider_enforcement_verified": False,
            }
        error_code: ErrorCode | None = None
        retryable = False
        if response.status_code in (401, 403):
            error_code = "authentication"
        elif response.status_code == 429:
            error_code, retryable = "rate_limited", True
        elif response.status_code >= 500:
            error_code, retryable = "unavailable", True
        elif response.is_error:
            error_code = "rejected"
        elif not isinstance(payload, dict):
            error_code = "invalid_response"
        if error_code is not None:
            raise ModelError(
                error_code,
                retryable=retryable,
                usage=usage,
                usage_unknown=usage is None or usage.status != "known",
                diagnostics=diagnostics,
            ) from None
        try:
            result = parse_response(payload, structured=request.response_schema is not None)
        except ModelError as error:
            error.diagnostics = diagnostics
            raise
        result.diagnostics = diagnostics
        return result
