from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high"]


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

    # Answers. "ollama" runs a local model (free, private, weaker); "anthropic" uses Claude
    # (better answers, about $0.02 per question with Sonnet, needs a key with credits).
    answer_provider: Literal["ollama", "anthropic"] = "ollama"
    ollama_answer_model: str = "qwen2.5-coder:3b"
    # Tokens the local model can read at once. Must hold the system prompt, the excerpts, the
    # recent conversation and the answer. 8K fits a 3B model fully on a 4 GB GPU.
    ollama_answer_context: int = 8_192
    anthropic_api_key: SecretStr | None = None
    claude_answer_model: str = "claude-sonnet-5-5"
    # How much Claude thinks before answering. "low" keeps answers fast; raise it for harder
    # questions about how a whole system fits together.
    claude_answer_effort: Effort = "low"
    # How much code goes to the model with each question: at most this many chunks, and at
    # most this many characters of code in total (the best-ranked chunks are kept).
    answer_sources: int = 6
    answer_source_chars: int = 16_000

    # Pull request reviews. Public PRs need no token, but GitHub allows only 60 anonymous API
    # requests an hour; a free personal access token (no scopes) raises that to 5,000.
    github_token: SecretStr | None = None
    review_max_files: int = 15
    review_max_findings_per_file: int = 3

    # Indexing limits: keep one huge or unusual repo from exhausting the worker.
    clone_timeout_seconds: int = 300
    max_file_bytes: int = 300_000
    max_files: int = 10_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
