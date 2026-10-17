from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from careerforge.schemas.resources import InputModel

Channel = Literal[
    "boss",
    "nowcoder",
    "liepin",
    "tencent",
    "baidu",
    "bytedance",
    "alibaba",
    "meituan",
    "jd",
    "netease",
    "kuaishou",
]


DEFAULT_CHANNELS: list[Channel] = ["nowcoder", "tencent"]


class CollectionInput(InputModel):
    query: str = Field(default="Agent 开发", min_length=1, max_length=200)
    channels: list[Channel] = Field(
        default_factory=lambda: list(DEFAULT_CHANNELS),
        min_length=1,
        max_length=11,
    )
    urls: list[str] = Field(default_factory=list, max_length=12)
    mode: Literal["manual", "agent"] = "manual"
    job_category: str = Field(default="Agent 开发", min_length=1, max_length=100)
    limit: int = Field(default=6, ge=1, le=12)
    request_limit: int = Field(default=30, ge=1, le=50)
    model_call_limit: int = Field(default=2, ge=1, le=3)
    allow_model_calls: bool = False
    allow_search_api: bool = False
    refresh_existing: bool = False

    @field_validator("query", "job_category")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("查询和岗位分类不能为空")
        return value.strip()

    @model_validator(mode="after")
    def validate_urls(self) -> Self:
        from careerforge.infrastructure.collection.portals import identify

        self.channels = list(dict.fromkeys(self.channels))
        self.urls = list(dict.fromkeys(self.urls))
        for url in self.urls:
            if len(url) > 4000 or identify(url) not in self.channels:
                raise ValueError("详情链接必须属于本轮选择的招聘渠道")
        if self.mode == "agent" and not self.allow_model_calls:
            raise ValueError("Agent 模式需要明确开启本轮模型调用")
        return self
