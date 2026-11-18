"""Explicit learning-state events; none grant execution, publication or mastery."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, model_validator

from .contracts import Contract


class PreferenceScope(Contract):
    kind: Literal["global", "topic", "source", "project_facts"] = "global"
    identifier: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def bounded_scope(self):
        if (self.kind in {"topic", "source"}) != (self.identifier is not None):
            raise ValueError("Only topic/source scopes require an identifier")
        return self


class PreferenceEventInput(Contract):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=150)
    operation: Literal["set", "correct", "revoke"] = "set"
    key: Literal["requested_style", "practice", "check_understanding"]
    value: (
        Literal[
            "concept_first",
            "code_first",
            "comparison_first",
            "architecture_first",
            "evidence_first",
        ]
        | StrictBool
        | None
    ) = None
    scope: PreferenceScope = Field(default_factory=PreferenceScope)
    lifetime: Literal["turn", "conversation", "persistent"] = "turn"
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    target_event_id: str | None = Field(default=None, min_length=1, max_length=100)
    durable_consent: StrictBool = False

    @model_validator(mode="after")
    def explicit_event(self):
        if self.operation == "revoke":
            if self.value is not None or not self.target_event_id:
                raise ValueError("Revocation requires a target and no value")
        else:
            if self.key == "requested_style":
                if type(self.value) is not str:
                    raise ValueError("Style requires a closed style value")
            elif type(self.value) is not bool:
                raise ValueError("Optional activities require a boolean")
            if (self.operation == "correct") != bool(self.target_event_id):
                raise ValueError("Only a correction has a target")
        if self.lifetime != "persistent" and not self.conversation_id:
            raise ValueError("Temporary changes require a conversation")
        if (
            self.lifetime == "persistent"
            and self.operation != "revoke"
            and not self.durable_consent
        ):
            raise ValueError("Persistent changes require explicit durable consent")
        return self


class NavigationAckInput(Contract):
    answer_block_id: str = Field(min_length=1, max_length=100)
    citation_index: int = Field(default=0, ge=0, le=100)
    source_id: str = Field(min_length=1, max_length=100)
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    path: str = Field(min_length=1, max_length=1000)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    client_event: Literal["source_highlight_ack"]
    idempotency_key: str = Field(min_length=1, max_length=150)

    @model_validator(mode="after")
    def line_range(self):
        if self.end_line < self.start_line:
            raise ValueError("Invalid highlight range")
        return self


class LearningIntentInput(Contract):
    question: str = Field(min_length=1, max_length=10000)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
