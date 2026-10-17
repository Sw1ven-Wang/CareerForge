"""Explicit JSON boundary type; nested payloads are validated by their Pydantic contracts."""

from typing import Any

type JsonObject = dict[str, Any]
