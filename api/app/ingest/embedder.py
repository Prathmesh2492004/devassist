"""Turn chunks of code into embedding vectors.

Two providers share one interface:
- Ollama (default): an open-source model running locally. Free, no rate limits, no data
  leaves the machine; speed depends on the machine.
- Voyage AI: a hosted API. Faster and slightly more accurate, but needs an API key.
"""

import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol, TypedDict

import httpx

from app.config import Settings
from app.models import EMBEDDING_DIMENSIONS

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 6


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    tokens: int


class Embedder(Protocol):
    # Per-request limits, used by iter_batches to size each call.
    batch_items: int
    batch_chars: int

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult: ...

    def embed_query(self, text: str) -> list[float]: ...


class EmbeddingError(Exception):
    pass


class MissingApiKeyError(EmbeddingError):
    pass


def iter_batches(
    texts: Sequence[str], max_items: int, max_chars: int
) -> Iterator[tuple[int, Sequence[str]]]:
    """Yield (start_index, batch) slices that respect the per-request limits."""
    start = 0
    while start < len(texts):
        end, chars = start, 0
        while end < len(texts) and end - start < max_items:
            chars += len(texts[end])
            if chars > max_chars and end > start:
                break
            end += 1
        yield start, texts[start:end]
        start = end


class _VoyageItem(TypedDict):
    index: int
    embedding: list[float]


class _VoyageUsage(TypedDict):
    total_tokens: int


class _VoyageResponse(TypedDict):
    data: list[_VoyageItem]
    usage: _VoyageUsage


class VoyageEmbedder:
    # Voyage allows 1,000 inputs and 120k tokens per request for voyage-code-3. Stay well
    # under both; code averages about 3 characters per token.
    batch_items = 128
    batch_chars = 240_000

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
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query").vectors[0]

    def _embed(self, texts: Sequence[str], input_type: str) -> EmbeddingResult:
        payload = {
            "input": list(texts),
            "model": self._model,
            "input_type": input_type,
            "output_dimension": EMBEDDING_DIMENSIONS,
        }
        response = _post_with_retries(
            self._client, VOYAGE_URL, payload, "Voyage AI", self._sleep, headers=self._headers
        )
        if response.status_code == 401:
            raise EmbeddingError("Voyage AI rejected the API key. Check VOYAGE_API_KEY.")
        if response.status_code == 429:
            raise EmbeddingError(
                "Voyage AI rate limit reached. Without a payment method on your Voyage account "
                "the limit is 3 requests per minute. Add one, or set EMBEDDING_PROVIDER=ollama."
            )
        _raise_for_error(response, "Voyage AI")

        body: _VoyageResponse = response.json()
        data = sorted(body["data"], key=lambda item: item["index"])
        if len(data) != len(texts):
            raise EmbeddingError(f"Voyage returned {len(data)} vectors for {len(texts)} inputs")
        return EmbeddingResult(
            vectors=[item["embedding"] for item in data],
            tokens=body["usage"]["total_tokens"],
        )


class _OllamaResponse(TypedDict, total=False):
    embeddings: list[list[float]]
    prompt_eval_count: int


# Qwen3-Embedding is trained with an instruction on the query side only; documents are embedded
# as-is. See the model card.
QUERY_INSTRUCTION = "Given a question about a codebase, retrieve the code that answers it"


class OllamaEmbedder:
    # A local model on CPU: small batches keep memory low and progress updates frequent.
    batch_items = 16
    batch_chars = 48_000

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/api/embed"
        self._base_url = base_url
        self._model = model
        # The first call loads the model into memory, and CPU inference is slow: be patient.
        self._client = client or httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0))
        self._sleep = sleep

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}"]).vectors[0]

    def _embed(self, texts: Sequence[str]) -> EmbeddingResult:
        payload = {"model": self._model, "input": list(texts), "truncate": True}
        response = _post_with_retries(
            self._client, self._url, payload, f"Ollama at {self._base_url}", self._sleep
        )
        if response.status_code == 404:
            raise EmbeddingError(
                f"Ollama does not have the model {self._model}. "
                "Run `docker compose up ollama-pull` to download it."
            )
        _raise_for_error(response, "Ollama")

        body: _OllamaResponse = response.json()
        vectors = body.get("embeddings", [])
        if len(vectors) != len(texts):
            raise EmbeddingError(f"Ollama returned {len(vectors)} vectors for {len(texts)} inputs")
        if vectors and len(vectors[0]) != EMBEDDING_DIMENSIONS:
            raise EmbeddingError(
                f"{self._model} produces {len(vectors[0])}-dimensional vectors, but the "
                f"database stores {EMBEDDING_DIMENSIONS}. Choose a {EMBEDDING_DIMENSIONS}-"
                "dimensional model."
            )
        return EmbeddingResult(vectors=vectors, tokens=body.get("prompt_eval_count", 0))


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "ollama":
        return OllamaEmbedder(settings.ollama_url, settings.ollama_model)

    if settings.voyage_api_key is None or not settings.voyage_api_key.get_secret_value():
        raise MissingApiKeyError(
            "VOYAGE_API_KEY is not set. Add it to .env and restart the worker, "
            "or set EMBEDDING_PROVIDER=ollama to embed locally."
        )
    return VoyageEmbedder(settings.voyage_api_key.get_secret_value(), settings.voyage_model)


def _post_with_retries(
    client: httpx.Client,
    url: str,
    payload: dict[str, object],
    service: str,
    sleep: Callable[[float], None],
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST, retrying network errors and retryable statuses. Returns the last response."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.post(url, json=payload, headers=headers)
        except httpx.TransportError as exc:
            if attempt == MAX_ATTEMPTS:
                raise EmbeddingError(f"Could not reach {service}: {exc}") from exc
            sleep(_backoff(attempt))
            continue
        if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
            sleep(_retry_after(response) or _backoff(attempt))
            continue
        return response
    raise AssertionError("unreachable")  # pragma: no cover


def _raise_for_error(response: httpx.Response, service: str) -> None:
    if response.is_error:
        raise EmbeddingError(f"{service} returned {response.status_code}: {response.text[:300]}")


def _backoff(attempt: int) -> float:
    return float(min(2**attempt, 60))


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return min(float(response.headers["retry-after"]), 60.0)
    except (KeyError, ValueError):
        return None
