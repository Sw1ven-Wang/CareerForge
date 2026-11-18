"""Bounded SSE decoder. Only complete frames can advance a replay cursor."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class StreamProtocolError(ValueError):
    """The server did not return the documented persisted-event contract."""


@dataclass(frozen=True)
class ServerEvent:
    sequence: int
    name: str
    payload: dict[str, Any]
    metadata: dict[str, Any] | None = None


def parse_events(lines: Iterable[str], max_characters: int = 2_000_000) -> Iterator[ServerEvent]:
    """Decode complete SSE frames; an unfinished final frame is retried, not acknowledged.

    Keep the cursor outside this iterator: duplicate replay frames are harmless and
    caller state is persisted only after the complete event has been displayed.
    """
    name = "message"
    identifier: str | None = None
    data: list[str] = []
    size = 0
    for line in lines:
        size += len(line)
        if size > max_characters:
            raise StreamProtocolError("event exceeds the bounded SSE contract")
        if line == "":
            if data:
                if not identifier or not identifier.isascii() or not identifier.isdecimal():
                    raise StreamProtocolError("event is missing a positive persisted sequence")
                try:
                    sequence = int(identifier)
                    payload = json.loads("\n".join(data))
                except (ValueError, RecursionError) as exc:
                    raise StreamProtocolError("invalid event payload") from exc
                if sequence < 1 or not isinstance(payload, dict):
                    raise StreamProtocolError("invalid event sequence or payload shape")
                metadata = None
                if "schema_version" in payload:
                    required = {
                        "schema_version",
                        "stream_id",
                        "seq",
                        "event_id",
                        "type",
                        "occurred_at",
                        "payload_ref",
                        "correlation_id",
                        "payload",
                    }
                    if (
                        not required <= payload.keys()
                        or payload["schema_version"] != "answer-event-1.6"
                        or type(payload["seq"]) is not int
                        or payload["seq"] != sequence
                        or payload["type"] != name
                        or not isinstance(payload["payload"], dict)
                        or any(
                            not isinstance(payload[key], str) or not 1 <= len(payload[key]) <= 200
                            for key in ("stream_id", "event_id", "correlation_id", "occurred_at")
                        )
                        or payload["payload_ref"] is not None
                        and not isinstance(payload["payload_ref"], str)
                    ):
                        raise StreamProtocolError(
                            "event envelope does not match its persisted SSE identity"
                        )
                    try:
                        datetime.fromisoformat(payload["occurred_at"])
                    except ValueError as exc:
                        raise StreamProtocolError("event timestamp is invalid") from exc
                    metadata = {key: value for key, value in payload.items() if key != "payload"}
                    payload = payload["payload"]
                # Legacy payload-only frames remain readable by the new CLI;
                # new servers always send the complete versioned envelope.
                yield ServerEvent(sequence, name, payload, metadata)
            name, identifier, data, size = "message", None, [], 0
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            identifier = value
        elif field == "event":
            name = value
        elif field == "data":
            data.append(value)
