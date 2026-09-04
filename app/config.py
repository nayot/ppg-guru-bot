import logging

from pydantic_settings import BaseSettings, SettingsConfigDict

# chromadb's posthog telemetry client has a version-compat bug that throws
# on every call regardless of the anonymized_telemetry=False setting; it's
# caught internally and harmless, just noisy.
logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    line_channel_secret: str
    line_channel_access_token: str

    openrouter_api_key: str
    openrouter_model: str = "anthropic/claude-sonnet-5"

    # Transient upstream failures are retried before the pilot sees an error.
    # OpenRouter forwards the provider's 429s verbatim, and some models (e.g.
    # qwen/qwen3.7-flash) have a single provider, so there's no failover.
    llm_max_attempts: int = 3
    llm_retry_base_delay: float = 1.0
    llm_retry_max_delay: float = 10.0

    embedding_model: str = "intfloat/multilingual-e5-small"
    manuals_dir: str = "manuals"
    data_dir: str = "data"
    top_k: int = 5
    # Wider second pass over the manuals before conceding to the web source.
    manual_retry_k: int = 25

    # Rolling per-user conversation history kept for chat continuity.
    memory_max_messages: int = 20

    # Comma-separated LINE user/group/room IDs allowed to use the bot.
    # Empty (default) means unrestricted.
    allowed_source_ids: str = ""

    # Fallback web source, consulted only when the manuals don't answer a
    # question. Indexed into its own Chroma collection (see app/web_ingest.py)
    # so manual excerpts and web excerpts can never be silently mixed.
    website_enabled: bool = True
    website_base_url: str = "https://www.southwestairsports.com/"
    website_name: str = "Southwest Airsports"
    website_collection: str = "website"
    website_top_k: int = 8
    # Politeness settings for the crawler (one-off/manual runs, not startup).
    website_crawl_delay: float = 0.5
    website_max_pages: int = 0  # 0 = no limit
    website_user_agent: str = "PPGGuruBot/1.0 (+https://eng-ai.buu.ac.th)"


settings = Settings()
