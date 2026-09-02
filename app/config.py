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

    embedding_model: str = "intfloat/multilingual-e5-small"
    manuals_dir: str = "manuals"
    data_dir: str = "data"
    top_k: int = 5

    # Rolling per-user conversation history kept for chat continuity.
    memory_max_messages: int = 20

    # Comma-separated LINE user/group/room IDs allowed to use the bot.
    # Empty (default) means unrestricted.
    allowed_source_ids: str = ""


settings = Settings()
