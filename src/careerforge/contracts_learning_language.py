"""Learning language choices are user policy, never inferred employer requirements."""

from typing import Literal

from pydantic import Field, field_validator

from .contracts import Contract
from .contracts_conversation_actions_v1 import WorkflowControl


def normalize_language(value: str) -> str:
    value = value.strip().casefold()
    aliases = {
        "py": "python",
        "python3": "python",
        "golang": "go",
        "js": "javascript",
        "ts": "typescript",
        "csharp": "c#",
        "c++": "cpp",
    }
    value = aliases.get(value, value)
    if (
        not value
        or len(value) > 40
        or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789+#.-" for c in value)
    ):
        raise ValueError("请输入具体语言名称，如 Python、Java、Go 或 Rust")
    return value


class ImplementationLanguageInput(WorkflowControl):
    expected_focus_revision: int = Field(ge=1, strict=True)
    languages: list[str] = Field(min_length=1, max_length=8)

    @field_validator("languages")
    @classmethod
    def normalized(cls, values):
        return list(dict.fromkeys(normalize_language(v) for v in values))


class KnowledgeImplementationLinkInput(Contract):
    namespace_id: str = Field(min_length=1, max_length=80)
    architecture_publication_id: str = Field(min_length=1, max_length=80)
    implementation_publication_id: str = Field(min_length=1, max_length=80)
    language: str = Field(min_length=1, max_length=40)
    domain: Literal["ai_agent", "backend"] = "ai_agent"
    # A user association is not an independently verified semantic claim.
    relation: Literal["implementation_example_for"] = "implementation_example_for"
    semantic_quality_verified: Literal[False] = False

    @field_validator("language")
    @classmethod
    def normalized(cls, value):
        return normalize_language(value)
