"""Application settings, loaded from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DASHSCOPE_COMPAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEEPSEEK_URL = "https://api.deepseek.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM providers ---
    llm_provider: Literal["qwen", "deepseek"] = "qwen"
    dashscope_api_key: str = ""
    deepseek_api_key: str = ""
    # Empty means "use the provider default" (see `chat_model_name`).
    chat_model: str = ""
    emotion_model: str = ""
    llm_temperature: float = 0.3

    # --- Embeddings / rerank (always DashScope) ---
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024
    rerank_enabled: bool = True
    rerank_model: str = "gte-rerank-v2"

    # --- Storage ---
    database_url: str = "mysql+pymysql://root@127.0.0.1:3306/cs_agent"
    runtime_dir: Path = PROJECT_ROOT / "data" / "runtime"
    docs_dir: Path = PROJECT_ROOT / "data" / "docs"
    faq_path: Path = PROJECT_ROOT / "data" / "faq.yaml"

    # --- Auth ---
    dev_mode: bool = True
    dev_otp_code: str = "123456"
    jwt_secret: str = "dev-only-secret-change-me-in-production-0000"
    jwt_ttl_hours: int = 24
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_resend_seconds: int = 60
    # Required for POST /api/admin/reindex outside dev mode.
    admin_token: str = ""

    # --- Business rules ---
    return_window_days: int = 7

    # --- Retrieval ---
    rag_fetch_k: int = 8
    rag_top_n: int = 4
    rag_min_relevance: float = 0.35
    rag_min_rerank_score: float = 0.2

    # --- FAQ matching ---
    faq_token_coverage: float = 0.75
    faq_embedding_threshold: float = 0.70

    # --- Human handoff ---
    handoff_angry_streak: int = 2
    handoff_unresolved_limit: int = 2

    @property
    def chat_model_name(self) -> str:
        if self.chat_model:
            return self.chat_model
        return "qwen-plus" if self.llm_provider == "qwen" else "deepseek-chat"

    @property
    def emotion_model_name(self) -> str:
        if self.emotion_model:
            return self.emotion_model
        return "qwen-flash" if self.llm_provider == "qwen" else "deepseek-chat"

    @property
    def checkpoint_path(self) -> Path:
        return self.runtime_dir / "checkpoints.sqlite"

    @property
    def chroma_dir(self) -> Path:
        return self.runtime_dir / "chroma"


@lru_cache
def get_settings() -> Settings:
    return Settings()
