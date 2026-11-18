"""Shared subscriptions never contain a payer or private task input."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from .contracts import Contract


class SharedBuildInput(Contract):
    namespace_id: str
    funding_id: str
    source_id: str
    topic_keys: list[str] = Field(default_factory=list, max_length=15)
    duration_minutes: int = Field(default=30, ge=1, le=10080, strict=True)
    planning_call_seconds: int = Field(default=120, ge=1, le=3600, strict=True)
    call_cap: int = Field(default=24, ge=5, le=100)
    max_units_per_project: int = Field(default=2, ge=1, le=5)
    no_subscribers_policy: Literal["continue", "cancel_after_last_detach"] = "continue"
    force_new_generation: bool = False
    idempotency_key: str = Field(min_length=1, max_length=120)
    confirm: Literal["SYSTEM_BUILD"]

    @field_validator("topic_keys")
    @classmethod
    def unique_topics(cls, value: list[str]) -> list[str]:
        return sorted(set(value))


class SubscribeInput(Contract):
    rights: list[Literal["progress", "result"]] = Field(
        default=["progress", "result"], min_length=1, max_length=2
    )
    idempotency_key: str = Field(min_length=1, max_length=120)

    @field_validator("rights")
    @classmethod
    def unique_rights(
        cls, value: list[Literal["progress", "result"]]
    ) -> list[Literal["progress", "result"]]:
        return sorted(set(value))


class UnsubscribeInput(Contract):
    idempotency_key: str = Field(min_length=1, max_length=120)
