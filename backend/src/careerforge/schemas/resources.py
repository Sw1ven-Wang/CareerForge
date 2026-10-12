from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(InputModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def email_format(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("请输入有效邮箱")
        return value


class TimeBudget(InputModel):
    weekly_hours: float | None = Field(default=None, gt=0, le=168)
    target_date: date | None = None
    reserved_fraction: float = Field(default=0.2, ge=0, lt=1)


class GoalInput(InputModel):
    role_direction: str = Field(min_length=1, max_length=200)
    job_type: Literal["internship", "entry_level", "experienced", "unspecified"] = "unspecified"
    regions: list[str] = Field(default_factory=list, max_length=20)
    experience_constraints: str | None = Field(default=None, max_length=2000)
    time_budget: TimeBudget = Field(default_factory=TimeBudget)
    user_objectives: list[str] = Field(default_factory=list, max_length=20)


class GoalRevision(GoalInput):
    expected_version: int = Field(ge=1, strict=True)
    change_reason: str = Field(min_length=1, max_length=2000)


SourceType = Literal["job_posting", "interview_experience", "official_doc", "user_note"]
Precision = Literal["exact", "date_only", "approximate", "unknown"]


class SourceImport(InputModel):
    source_type: SourceType
    title: str | None = Field(default=None, max_length=500)
    source_url: str | None = Field(default=None, max_length=4000)
    import_mode: Literal["text", "link_clue"] = "text"
    content: str | None = None
    published_at: datetime | None = None
    published_at_precision: Precision = "unknown"
    declared_provenance: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_content(self) -> "SourceImport":
        if self.import_mode == "text" and (not self.content or not self.content.strip()):
            raise ValueError("正文不能为空")
        if self.import_mode == "link_clue" and (not self.source_url or self.content is not None):
            raise ValueError("链接线索需要 URL，不能冒充已读正文")
        if self.source_url and not self.source_url.startswith(("https://", "http://")):
            raise ValueError("仅接受 HTTP/HTTPS 来源链接")
        if self.published_at is None and self.published_at_precision != "unknown":
            raise ValueError("日期缺失时精度必须为 unknown")
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("发布时间必须包含时区")
        return self


class MetadataOverride(InputModel):
    source_type: SourceType | None = None
    title: str | None = Field(default=None, max_length=500)
    source_name: str | None = Field(default=None, max_length=500)
    published_at: datetime | None = None
    published_at_precision: Precision | None = None
    published_at_raw: str | None = Field(default=None, max_length=500)


class SnapshotInput(InputModel):
    expected_version: int = Field(ge=1, strict=True)
    change_reason: str = Field(min_length=1, max_length=2000)
    content: str | None = None
    base_snapshot_id: UUID | None = None
    metadata_overrides: MetadataOverride = Field(default_factory=MetadataOverride)

    @model_validator(mode="after")
    def validate_selection(self) -> "SnapshotInput":
        if (self.content is None) == (self.base_snapshot_id is None):
            raise ValueError("content 与 base_snapshot_id 必须恰选一项")
        if self.content is not None and not self.content.strip():
            raise ValueError("正文不能为空")
        return self


class VersionInput(InputModel):
    expected_version: int = Field(ge=1, strict=True)


class CitationInput(InputModel):
    source_snapshot_id: UUID
    char_start: int = Field(ge=0, strict=True)
    char_end: int = Field(gt=0, strict=True)
    text_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class Envelope(BaseModel):
    data: dict[str, Any] | list[dict[str, Any]]
    request_id: str
    next_cursor: str | None = None
