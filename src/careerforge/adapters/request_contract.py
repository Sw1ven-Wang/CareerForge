"""Opt-in lossless request representation. Legacy hashes are never rewritten.

Only a generated text user message can replace duplicate raw context. The full
original message remains on the wire; this is neither truncation nor compression.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from ..errors import DomainError

LEGACY_REQUEST_CONTRACT = "legacy-v1"
CONTEXT_ONCE_CONTRACT = "context-once-v1"
MODEL_REQUEST_MAX_BYTES = 250_000
PREFIX = "UNTRUSTED_TASK_DATA\n"
BASE_FIELDS = {
    "role",
    "messages",
    "schema",
    "tools",
    "max_output_tokens",
    "profile_version",
    "data_classes",
    "access_dependencies",
}
AUTHORITY_FIELDS = {"funding_source_scope", "access_closure_hash", "privacy_generations"}


def contract_error() -> DomainError:
    return DomainError("MODEL_REQUEST_CONTRACT", "模型请求封装合同无效；没有发送请求。", 422)


def json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise contract_error() from exc


def check_bytes(data: bytes) -> None:
    if len(data) > MODEL_REQUEST_MAX_BYTES:
        raise DomainError("MODEL_CONTEXT_LIMIT", "模型上下文超过合同限制。", 413)


def pack_request(request: dict[str, Any], *, generated: bool) -> dict[str, Any]:
    """Keep every message unchanged; bind only an exact generated duplicate."""
    result = deepcopy(request)
    result["request_contract"] = CONTEXT_ONCE_CONTRACT
    if generated and request["role"] not in {"embedding", "reranker"}:
        context_bytes = json_bytes(request["context"])
        if len(request["messages"]) != 2 or request["messages"][1] != {
            "role": "user",
            "content": PREFIX + context_bytes.decode("utf-8"),
        }:
            raise contract_error()
        del result["context"]
        result["context_binding"] = {
            "kind": "generated-user-json-v1",
            "message_index": 1,
            "sha256": hashlib.sha256(context_bytes).hexdigest(),
            "utf8_bytes": len(context_bytes),
        }
    return result


def _bound_context(request: dict[str, Any]) -> dict[str, Any]:
    try:
        binding = request["context_binding"]
        messages = request["messages"]
        if (
            not isinstance(binding, dict)
            or set(binding) != {"kind", "message_index", "sha256", "utf8_bytes"}
            or binding["kind"] != "generated-user-json-v1"
            or type(binding["message_index"]) is not int
            or binding["message_index"] != 1
            or type(binding["utf8_bytes"]) is not int
            or binding["utf8_bytes"] < 0
            or request["role"] in {"embedding", "reranker"}
            or len(messages) != 2
            or set(messages[0]) != {"role", "content"}
            or messages[0]["role"] != "system"
            or not isinstance(messages[0]["content"], str)
            or set(messages[1]) != {"role", "content"}
            or messages[1]["role"] != "user"
            or not messages[1]["content"].startswith(PREFIX)
        ):
            raise contract_error()
        raw = messages[1]["content"][len(PREFIX) :].encode("utf-8")
        if (
            len(raw) != binding["utf8_bytes"]
            or hashlib.sha256(raw).hexdigest() != binding["sha256"]
        ):
            raise contract_error()
        value = json.loads(raw)
        if not isinstance(value, dict) or json_bytes(value) != raw:
            raise contract_error()
        return value
    except (KeyError, TypeError, ValueError, AttributeError, UnicodeError) as exc:
        raise contract_error() from exc


def validate_request(request: dict[str, Any], *, authority: bool = False) -> None:
    """Bound the whole envelope, including every authority/metadata field."""
    check_bytes(json_bytes(request))
    representation = {"context_binding"} if "context_binding" in request else {"context"}
    required = BASE_FIELDS | {"request_contract"} | representation
    allowed = required | AUTHORITY_FIELDS
    if (
        request.get("request_contract") != CONTEXT_ONCE_CONTRACT
        or not required <= set(request)
        or not set(request) <= allowed
        or (authority and not AUTHORITY_FIELDS <= set(request))
    ):
        raise contract_error()
    if (
        not isinstance(request["role"], str)
        or not isinstance(request["messages"], list)
        or not all(isinstance(message, dict) for message in request["messages"])
        or not (request["schema"] is None or isinstance(request["schema"], dict))
        or not isinstance(request["tools"], list)
        or type(request["max_output_tokens"]) is not int
        or not 128 <= request["max_output_tokens"] <= 8192
        or not isinstance(request["profile_version"], str)
    ):
        raise contract_error()
    for field in ("data_classes", "access_dependencies", "funding_source_scope"):
        if field in request and (
            not isinstance(request[field], list)
            or not all(isinstance(value, str) for value in request[field])
        ):
            raise contract_error()
    if "privacy_generations" in request and (
        not isinstance(request["privacy_generations"], dict)
        or not all(
            isinstance(key, str) and type(value) is int and value >= 0
            for key, value in request["privacy_generations"].items()
        )
    ):
        raise contract_error()
    if "access_closure_hash" in request and not isinstance(request["access_closure_hash"], str):
        raise contract_error()
    if "context_binding" in request:
        _bound_context(request)
    elif not isinstance(request["context"], dict):
        raise contract_error()


def request_context(request: dict[str, Any]) -> dict[str, Any]:
    """Decode locally for consumers; never append this duplicate to model messages."""
    if "request_contract" not in request:
        return request["context"]
    validate_request(request)
    return _bound_context(request) if "context_binding" in request else request["context"]


def legacy_request(request: dict[str, Any]) -> dict[str, Any]:
    """Lossless comparison/sizing only, NOT a new-contract transport payload."""
    if "request_contract" not in request:
        return request
    # Selection logic may size over-cap payloads; do not use bounded decode here.
    if request["request_contract"] != CONTEXT_ONCE_CONTRACT:
        raise contract_error()
    result = deepcopy(request)
    if "context_binding" in result:
        result["context"] = _bound_context(result)
        del result["context_binding"]
    del result["request_contract"]
    return result
