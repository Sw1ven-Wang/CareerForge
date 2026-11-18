"""Collect a single SSE completion; incomplete streams never become model results."""

import json

from ..errors import DomainError


def uncertain():
    return DomainError(
        "MODEL_RESULT_UNCERTAIN", "模型流未完整结束，未接受部分结果，也未自动重试。", 502
    )


async def collect_chat_stream(response):
    text, calls, usage = [], {}, {}
    response_id = None
    finished = None
    done = False
    total = 0
    data_lines = []

    def consume(data):
        nonlocal response_id, finished, done, usage
        if done:
            raise uncertain()
        if data == "[DONE]":
            done = True
            return
        value = json.loads(data)
        if not isinstance(value, dict) or value.get("error"):
            raise uncertain()
        if value.get("id"):
            if response_id and response_id != value["id"]:
                raise uncertain()
            response_id = value["id"]
        if value.get("usage"):
            usage = value["usage"]
        for choice in value.get("choices", []):
            if choice.get("index") != 0 or finished is not None:
                raise uncertain()
            delta = choice.get("delta") or {}
            if delta.get("content") is not None:
                if not isinstance(delta["content"], str):
                    raise uncertain()
                text.append(delta["content"])
            for item in delta.get("tool_calls") or []:
                index = item["index"]
                if type(index) is not int or not 0 <= index < 32:
                    raise uncertain()
                call = calls.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if item.get("id"):
                    if call["id"] and call["id"] != item["id"]:
                        raise uncertain()
                    call["id"] = item["id"]
                for key in ("name", "arguments"):
                    part = item.get("function", {}).get(key)
                    if part is not None:
                        if not isinstance(part, str):
                            raise uncertain()
                        call["function"][key] += part
            if choice.get("finish_reason") is not None:
                finished = choice["finish_reason"]

    async for line in response.aiter_lines():
        total += len(line.encode("utf-8"))
        if total > 4_000_000:
            raise uncertain()
        if line == "":
            if data_lines:
                consume("\n".join(data_lines))
                data_lines = []
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if data_lines:
        consume("\n".join(data_lines))
    if not done or finished not in {"stop", "tool_calls"}:
        raise uncertain()
    ordered = [calls[i] for i in sorted(calls)]
    if any(not c["id"] or not c["function"]["name"] for c in ordered):
        raise uncertain()
    return {
        "id": response_id,
        "usage": usage,
        "choices": [
            {
                "finish_reason": finished,
                "message": {"content": "".join(text), "tool_calls": ordered},
            }
        ],
    }
