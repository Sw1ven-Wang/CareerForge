from uuid import UUID

from pydantic import Field, field_validator

from careerforge.schemas.resources import InputModel, SourceType


class CategoryInput(InputModel):
    name: str = Field(min_length=1, max_length=100)
    parent_id: UUID | None = None

    @field_validator("name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("分类名称不能为空")
        return value


class CategoryRevision(CategoryInput):
    expected_version: int = Field(ge=1, strict=True)


class LibrarySourceRevision(InputModel):
    expected_version: int = Field(ge=1, strict=True)
    title: str = Field(min_length=1, max_length=500)
    source_url: str | None = Field(default=None, max_length=4000)
    source_type: SourceType
    category_id: UUID

    @field_validator("title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("岗位标题不能为空")
        return value

    @field_validator("source_url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        value = value.strip() if value else None
        if value and not value.startswith(("https://", "http://")):
            raise ValueError("仅接受 HTTP/HTTPS 来源链接")
        return value or None
