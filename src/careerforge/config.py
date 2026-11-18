from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import DomainError


@dataclass(frozen=True)
class Settings:
    mode: str = "production"
    database_url: str = "postgresql+psycopg://careerforge:careerforge@localhost:5432/careerforge"
    redis_broker_url: str = "redis://localhost:6379/0"
    redis_cache_url: str = "redis://localhost:6380/0"
    model_base_url: str = ""
    model_api_key: str = ""
    model_name: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dimensions: int = 384
    rerank_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = ""
    github_token: str = ""
    model_request_contract: str = "legacy-v1"
    max_output_tokens: int = 8192
    model_timeout: int = 120
    model_streaming: bool = False
    sandbox_image: str = ""
    state_dir: Path = Path.home() / ".careerforge"
    api_url: str = "http://127.0.0.1:8000"
    otlp_endpoint: str = ""
    pi_runtime_dir: Path | None = None
    allow_fixture_publication: bool = False
    role_models: dict[str, str] = field(default_factory=dict)
    structured_output: str = "auto"
    workflow_engine: str = "legacy"
    session_graph_version: str = "knowledge-session-v1"
    learning_graph_version: str | None = None
    graph_checkpoint_url: str = ""
    evolution_graph_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            workflow_engine=os.getenv("CAREERFORGE_WORKFLOW_ENGINE", "legacy"),
            session_graph_version=os.getenv(
                "CAREERFORGE_SESSION_GRAPH_VERSION", "knowledge-session-v1"
            ),
            evolution_graph_enabled=os.getenv("CAREERFORGE_EVOLUTION_GRAPH_ENABLED", "0") == "1",
            learning_graph_version=os.getenv("CAREERFORGE_LEARNING_GRAPH_VERSION") or None,
            graph_checkpoint_url=os.getenv("CAREERFORGE_GRAPH_CHECKPOINT_URL", ""),
            model_request_contract=os.getenv("CAREERFORGE_MODEL_REQUEST_CONTRACT", "legacy-v1"),
            mode=os.getenv("CAREERFORGE_MODE", "production"),
            database_url=os.getenv("CAREERFORGE_DATABASE_URL", cls.database_url),
            redis_broker_url=os.getenv("CAREERFORGE_REDIS_BROKER_URL", cls.redis_broker_url),
            redis_cache_url=os.getenv("CAREERFORGE_REDIS_CACHE_URL", cls.redis_cache_url),
            model_base_url=os.getenv("OPENAI_BASE_URL", ""),
            model_api_key=os.getenv("OPENAI_API_KEY", ""),
            model_name=os.getenv("MODEL_NAME", ""),
            role_models=json.loads(os.getenv("CAREERFORGE_ROLE_MODELS", "{}")),
            structured_output=os.getenv("CAREERFORGE_STRUCTURED_OUTPUT", "auto"),
            embedding_base_url=os.getenv("EMBEDDING_BASE_URL", ""),
            embedding_api_key=os.getenv("EMBEDDING_API_KEY", ""),
            embedding_model=os.getenv("EMBEDDING_MODEL", ""),
            embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "384")),
            rerank_url=os.getenv("RERANK_URL", ""),
            rerank_api_key=os.getenv("RERANK_API_KEY", ""),
            rerank_model=os.getenv("RERANK_MODEL", ""),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            max_output_tokens=int(os.getenv("CAREERFORGE_MAX_OUTPUT_TOKENS", "8192")),
            model_timeout=int(os.getenv("CAREERFORGE_MODEL_TIMEOUT", "120")),
            model_streaming=os.getenv("CAREERFORGE_MODEL_STREAMING", "0") == "1",
            sandbox_image=os.getenv("CAREERFORGE_SANDBOX_IMAGE", ""),
            state_dir=Path(os.getenv("CAREERFORGE_STATE_DIR", str(Path.home() / ".careerforge"))),
            api_url=os.getenv("CAREERFORGE_API_URL", "http://127.0.0.1:8000"),
            otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            pi_runtime_dir=Path(os.environ["CAREERFORGE_PI_RUNTIME_DIR"])
            if os.getenv("CAREERFORGE_PI_RUNTIME_DIR")
            else None,
        )

    def validate(self) -> None:
        if self.model_request_contract not in {"legacy-v1", "context-once-v1"}:
            raise DomainError("CONFIG", "未知模型请求封装合同。", 500)
        if self.learning_graph_version not in {
            None,
            "learning-session-v1",
            "learning-session-practice-v1",
        }:
            raise DomainError("CONFIG", "未知学习会话图版本。", 500)
        if self.learning_graph_version and (
            self.workflow_engine != "langgraph"
            or self.session_graph_version != "knowledge-session-v2"
        ):
            raise DomainError("CONFIG", "学习图新准入需要显式 knowledge-session-v2。", 500)
        if self.workflow_engine not in {"legacy", "langgraph"}:
            raise DomainError("CONFIG", "未知工作流引擎。", 500)
        if self.session_graph_version not in {"knowledge-session-v1", "knowledge-session-v2"}:
            raise DomainError("CONFIG", "未知会话图版本。", 500)
        if (
            (self.workflow_engine == "langgraph" or self.evolution_graph_enabled)
            and self.mode != "test"
            and not self.graph_checkpoint_url.startswith(("postgresql://", "postgres://"))
        ):
            raise DomainError("CONFIG", "LangGraph 需要独立配置 PostgreSQL checkpoint URL。", 500)
        if self.structured_output not in {"auto", "json_schema", "json_object"}:
            raise DomainError("CONFIG", "结构化输出模式无效。", 500)
        if not isinstance(self.role_models, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in self.role_models.items()
        ):
            raise DomainError("CONFIG", "角色模型配置必须是字符串映射。", 500)
        if self.mode not in {"production", "development", "test"}:
            raise DomainError("CONFIG", "未知运行模式。", 500)
        if self.mode == "production":
            if not self.database_url.startswith("postgresql"):
                raise DomainError("CONFIG", "生产模式必须使用 PostgreSQL。", 500)
            if self.redis_broker_url == self.redis_cache_url:
                raise DomainError("CONFIG", "Broker 和业务缓存必须隔离。", 500)
            if self.allow_fixture_publication:
                raise DomainError("CONFIG", "生产模式禁止测试 Provider 发布知识。", 500)
        if not 128 <= self.max_output_tokens <= 8192:
            raise DomainError("CONFIG", "输出 token 上限无效。", 500)
        if self.embedding_dimensions < 8 or self.embedding_dimensions > 2000:
            raise DomainError("CONFIG", "向量维度无效。", 500)
