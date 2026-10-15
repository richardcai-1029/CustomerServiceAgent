"""Factories for chat models and embeddings (OpenAI-compatible endpoints)."""

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from cs_agent.config import DASHSCOPE_COMPAT_URL, DEEPSEEK_URL, Settings


class ConfigError(RuntimeError):
    pass


def _require(value: str, name: str) -> str:
    if not value:
        raise ConfigError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def make_chat_model(settings: Settings, *, model: str | None = None,
                    temperature: float | None = None) -> ChatOpenAI:
    if settings.llm_provider == "deepseek":
        api_key, base_url = _require(settings.deepseek_api_key, "DEEPSEEK_API_KEY"), DEEPSEEK_URL
    else:
        api_key = _require(settings.dashscope_api_key, "DASHSCOPE_API_KEY")
        base_url = DASHSCOPE_COMPAT_URL
    return ChatOpenAI(
        model=model or settings.chat_model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=settings.llm_temperature if temperature is None else temperature,
        timeout=60,
        max_retries=2,
    )


def make_embeddings(settings: Settings) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=_require(settings.dashscope_api_key, "DASHSCOPE_API_KEY"),
        base_url=DASHSCOPE_COMPAT_URL,
        dimensions=settings.embedding_dimensions,
        # DashScope expects raw strings (not token arrays), at most 10 per request,
        # and float vectors rather than the OpenAI SDK's default base64 encoding.
        check_embedding_ctx_length=False,
        chunk_size=10,
        model_kwargs={"encoding_format": "float"},
    )
