from uuid import UUID

from pydantic import Field, field_validator

from careerforge.schemas.repositories import RepositoryInput
from careerforge.schemas.resources import InputModel


class KnowledgeImport(RepositoryInput):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    files: dict[str, str] | None = Field(default=None, max_length=1000)

    @field_validator("requested_ref")
    @classmethod
    def pinned_commit(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
            raise ValueError("请提供完整 40 位 commit SHA，不能使用分支或标签")
        return value.lower()

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, values: list[str]) -> list[str]:
        if any(not v.strip() or len(v) > 60 for v in values):
            raise ValueError("主题标签不能为空或超过 60 字符")
        return list(dict.fromkeys(v.strip() for v in values))


class KnowledgeMetadata(InputModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    expected_revision: int = Field(ge=1)

    _tags = field_validator("tags")(KnowledgeImport.valid_tags)


class KnowledgeBind(InputModel):
    chunk_id: UUID
    objective_version_id: UUID
    note: str = Field(default="", max_length=2000)
