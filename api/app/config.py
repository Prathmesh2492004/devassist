from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings, read from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "DevAssist API"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://devassist:devassist@localhost:5432/devassist"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = ["http://localhost:3000"]

    # Embeddings. "ollama" runs a local model (free, no limits); "voyage" uses the Voyage AI API.
    embedding_provider: Literal["ollama", "voyage"] = "ollama"
    ollama_url: str = "http://localhost:11434"
    # Must output 1024-dimensional vectors to match the chunks.embedding column.
    ollama_model: str = "qwen3-embedding:0.6b"
    voyage_api_key: SecretStr | None = None
    voyage_model: str = "voyage-code-3"

    # Indexing limits: keep one huge or unusual repo from exhausting the worker.
    clone_timeout_seconds: int = 300
    max_file_bytes: int = 300_000
    max_files: int = 10_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
