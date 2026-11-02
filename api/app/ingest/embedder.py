"""Turn chunks of code into embedding vectors with Voyage AI."""

import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol, TypedDict

import httpx

from app.config import Settings
from app.models import EMBEDDING_DIMENSIONS

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
# Voyage allows 1,000 inputs and 120k tokens per request for voyage-code-3. Stay well under
# both; code averages about 3 characters per token.
MAX_BATCH_ITEMS = 128
MAX_BATCH_CHARS = 240_000
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 6


class _VoyageItem(TypedDict):
    index: int
    embedding: list[float]


class _VoyageUsage(TypedDict):
    total_tokens: int


class _VoyageResponse(TypedDict):
    data: list[_VoyageItem]
    usage: _VoyageUsage


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    tokens: int


class Embedder(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult: ...


class EmbeddingError(Exception):
    pass


class MissingApiKeyError(EmbeddingError):
    pass


def iter_batches(texts: Sequence[str]) -> Iterator[tuple[int, Sequence[str]]]:
    """Yield (start_index, batch) slices that respect the per-request limits."""
    start = 0
    while start < len(texts):
        end, chars = start, 0
        while end < len(texts) and end - start < MAX_BATCH_ITEMS:
            chars += len(texts[end])
            if chars > MAX_BATCH_CHARS and end > start:
                break
            end += 1
        yield start, texts[start:end]
        start = end


class VoyageEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._model = model
        self._client = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._sleep = sleep

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        payload = {
            "input": list(texts),
            "model": self._model,
            "input_type": "document",
            "output_dimension": EMBEDDING_DIMENSIONS,
        }
        body = self._post_with_retries(payload)
        data = sorted(body["data"], key=lambda item: item["index"])
        if len(data) != len(texts):
            raise EmbeddingError(f"Voyage returned {len(data)} vectors for {len(texts)} inputs")
        return EmbeddingResult(
            vectors=[item["embedding"] for item in data],
            tokens=body["usage"]["total_tokens"],
        )

    def _post_with_retries(self, payload: dict[str, object]) -> _VoyageResponse:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.post(VOYAGE_URL, json=payload, headers=self._headers)
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise EmbeddingError(f"Could not reach Voyage AI: {exc}") from exc
                self._sleep(_backoff(attempt))
                continue

            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                self._sleep(_retry_after(response) or _backoff(attempt))
                continue
            if response.status_code == 401:
                raise EmbeddingError("Voyage AI rejected the API key. Check VOYAGE_API_KEY.")
            if response.is_error:
                raise EmbeddingError(
                    f"Voyage AI returned {response.status_code}: {response.text[:300]}"
                )
            result: _VoyageResponse = response.json()
            return result
        raise EmbeddingError("Voyage AI kept failing after retries")  # pragma: no cover


def build_embedder(settings: Settings) -> Embedder:
    if settings.voyage_api_key is None or not settings.voyage_api_key.get_secret_value():
        raise MissingApiKeyError(
            "VOYAGE_API_KEY is not set. Add it to .env and restart the worker."
        )
    return VoyageEmbedder(settings.voyage_api_key.get_secret_value(), settings.embedding_model)


def _backoff(attempt: int) -> float:
    return float(min(2**attempt, 60))


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return min(float(response.headers["retry-after"]), 60.0)
    except (KeyError, ValueError):
        return None
