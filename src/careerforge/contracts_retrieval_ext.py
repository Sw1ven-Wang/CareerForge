from typing import Literal

from pydantic import Field

from .contracts import Contract


class QueryRewriteOutput(Contract):
    variants: list[str] = Field(default_factory=list, max_length=3)
    selected_topic_keys: list[str] = Field(default_factory=list, max_length=10)
    reason: Literal["terminology", "cross_language", "symbol_hint", "no_improvement"]


class IndexJobInput(Contract):
    namespace_id: str
    funding_id: str
    publication_ids: list[str] = Field(min_length=1, max_length=256)
    batch_size: int = Field(default=8, ge=1, le=16)
    call_cap: int = Field(default=8, ge=1, le=100)
    rebuild: bool = False
    idempotency_key: str = Field(min_length=1, max_length=120)
    confirm: Literal["INDEX"]
