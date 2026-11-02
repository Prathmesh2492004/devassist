import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.ingest.embedder import (
    EmbeddingError,
    MissingApiKeyError,
    OllamaEmbedder,
    VoyageEmbedder,
    build_embedder,
    iter_batches,
)
from app.models import EMBEDDING_DIMENSIONS


def _voyage(handler: httpx.MockTransport) -> tuple[VoyageEmbedder, list[float]]:
    sleeps: list[float] = []
    embedder = VoyageEmbedder(
        "test-key", "voyage-code-3", client=httpx.Client(transport=handler), sleep=sleeps.append
    )
    return embedder, sleeps


def _ollama(handler: httpx.MockTransport) -> tuple[OllamaEmbedder, list[float]]:
    sleeps: list[float] = []
    embedder = OllamaEmbedder(
        "http://ollama:11434/",
        "qwen3-embedding:0.6b",
        client=httpx.Client(transport=handler),
        sleep=sleeps.append,
    )
    return embedder, sleeps


def _ok(request: httpx.Request) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    # Return out of order to prove we sort by index.
    data = [{"index": i, "embedding": [float(i)]} for i in reversed(range(len(inputs)))]
    return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 42}})


def _ollama_ok(request: httpx.Request) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    vectors = [[float(i)] * EMBEDDING_DIMENSIONS for i in range(len(inputs))]
    return httpx.Response(200, json={"embeddings": vectors, "prompt_eval_count": 17})


# Batching


def test_batches_respect_item_and_size_limits() -> None:
    texts = ["x"] * 13 + ["y" * 990, "z" * 100]
    batches = list(iter_batches(texts, max_items=10, max_chars=1000))

    assert [len(b) for _, b in batches] == [10, 4, 1]
    assert [start for start, _ in batches] == [0, 10, 14]


def test_one_oversized_text_still_gets_its_own_batch() -> None:
    assert [len(b) for _, b in iter_batches(["x" * 5000], max_items=10, max_chars=1000)] == [1]


# Voyage


def test_voyage_sends_documents_and_returns_vectors_in_input_order() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"auth": request.headers["authorization"], **json.loads(request.content)})
        return _ok(request)

    embedder, _ = _voyage(httpx.MockTransport(handler))
    result = embedder.embed_documents(["a", "b", "c"])

    assert result.vectors == [[0.0], [1.0], [2.0]]
    assert result.tokens == 42
    assert seen[0]["auth"] == "Bearer test-key"
    assert seen[0]["input_type"] == "document"
    assert seen[0]["output_dimension"] == 1024


def test_voyage_retries_rate_limits_using_retry_after() -> None:
    responses = iter([httpx.Response(429, headers={"retry-after": "7"}), None])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses) or _ok(request)

    embedder, sleeps = _voyage(httpx.MockTransport(handler))

    assert embedder.embed_documents(["a"]).vectors == [[0.0]]
    assert sleeps == [7.0]


def test_voyage_persistent_rate_limit_explains_the_fix() -> None:
    embedder, sleeps = _voyage(httpx.MockTransport(lambda r: httpx.Response(429)))

    with pytest.raises(EmbeddingError, match="payment method.*EMBEDDING_PROVIDER=ollama"):
        embedder.embed_documents(["a"])
    assert len(sleeps) == 5


def test_voyage_retries_network_errors_with_backoff_then_gives_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    embedder, sleeps = _voyage(httpx.MockTransport(handler))

    with pytest.raises(EmbeddingError, match="Could not reach Voyage"):
        embedder.embed_documents(["a"])
    assert sleeps == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_voyage_bad_api_key_fails_with_a_clear_message() -> None:
    embedder, sleeps = _voyage(httpx.MockTransport(lambda r: httpx.Response(401)))

    with pytest.raises(EmbeddingError, match="VOYAGE_API_KEY"):
        embedder.embed_documents(["a"])
    assert sleeps == []


# Ollama


def test_ollama_sends_batch_to_embed_endpoint() -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return _ollama_ok(request)

    embedder, _ = _ollama(httpx.MockTransport(handler))
    result = embedder.embed_documents(["a", "b"])

    assert [v[0] for v in result.vectors] == [0.0, 1.0]
    assert result.tokens == 17
    assert seen == [
        (
            "http://ollama:11434/api/embed",
            {"model": "qwen3-embedding:0.6b", "input": ["a", "b"], "truncate": True},
        )
    ]


def test_ollama_missing_model_says_how_to_download_it() -> None:
    embedder, _ = _ollama(
        httpx.MockTransport(lambda r: httpx.Response(404, json={"error": "model not found"}))
    )

    with pytest.raises(EmbeddingError, match="docker compose up ollama-pull"):
        embedder.embed_documents(["a"])


def test_ollama_rejects_vectors_of_the_wrong_size() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1] * 768]})

    embedder, _ = _ollama(httpx.MockTransport(handler))

    with pytest.raises(EmbeddingError, match="768-dimensional"):
        embedder.embed_documents(["a"])


def test_ollama_unreachable_names_the_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    embedder, sleeps = _ollama(httpx.MockTransport(handler))

    with pytest.raises(EmbeddingError, match="Could not reach Ollama at http://ollama:11434"):
        embedder.embed_documents(["a"])
    assert len(sleeps) == 5


# Choosing a provider


def test_ollama_is_the_default_and_needs_no_key() -> None:
    assert isinstance(build_embedder(Settings(voyage_api_key=None)), OllamaEmbedder)


def test_voyage_provider_requires_an_api_key() -> None:
    with pytest.raises(MissingApiKeyError, match="EMBEDDING_PROVIDER=ollama"):
        build_embedder(Settings(embedding_provider="voyage", voyage_api_key=None))
    settings = Settings(embedding_provider="voyage", voyage_api_key=SecretStr("k"))
    assert isinstance(build_embedder(settings), VoyageEmbedder)
