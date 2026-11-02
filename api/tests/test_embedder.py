import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.ingest.embedder import (
    MAX_BATCH_CHARS,
    MAX_BATCH_ITEMS,
    EmbeddingError,
    MissingApiKeyError,
    VoyageEmbedder,
    build_embedder,
    iter_batches,
)


def _voyage(handler: httpx.MockTransport) -> tuple[VoyageEmbedder, list[float]]:
    sleeps: list[float] = []
    embedder = VoyageEmbedder(
        "test-key", "voyage-code-3", client=httpx.Client(transport=handler), sleep=sleeps.append
    )
    return embedder, sleeps


def _ok(request: httpx.Request) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    # Return out of order to prove we sort by index.
    data = [{"index": i, "embedding": [float(i)]} for i in reversed(range(len(inputs)))]
    return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 42}})


def test_batches_respect_item_and_size_limits() -> None:
    texts = ["x"] * (MAX_BATCH_ITEMS + 5) + ["y" * (MAX_BATCH_CHARS - 10), "z" * 100]
    batches = list(iter_batches(texts))

    assert [len(b) for _, b in batches] == [MAX_BATCH_ITEMS, 6, 1]
    assert [start for start, _ in batches] == [0, MAX_BATCH_ITEMS, MAX_BATCH_ITEMS + 6]


def test_one_oversized_text_still_gets_its_own_batch() -> None:
    assert [len(b) for _, b in iter_batches(["x" * (MAX_BATCH_CHARS * 2)])] == [1]


def test_sends_documents_and_returns_vectors_in_input_order() -> None:
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


def test_retries_rate_limits_using_retry_after() -> None:
    responses = iter([httpx.Response(429, headers={"retry-after": "7"}), None])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses) or _ok(request)

    embedder, sleeps = _voyage(httpx.MockTransport(handler))

    assert embedder.embed_documents(["a"]).vectors == [[0.0]]
    assert sleeps == [7.0]


def test_retries_network_errors_with_backoff_then_gives_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    embedder, sleeps = _voyage(httpx.MockTransport(handler))

    with pytest.raises(EmbeddingError, match="Could not reach Voyage"):
        embedder.embed_documents(["a"])
    assert sleeps == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_bad_api_key_fails_with_a_clear_message() -> None:
    embedder, sleeps = _voyage(httpx.MockTransport(lambda r: httpx.Response(401)))

    with pytest.raises(EmbeddingError, match="VOYAGE_API_KEY"):
        embedder.embed_documents(["a"])
    assert sleeps == []


def test_build_embedder_requires_an_api_key() -> None:
    with pytest.raises(MissingApiKeyError):
        build_embedder(Settings(voyage_api_key=None))
    assert isinstance(build_embedder(Settings(voyage_api_key=SecretStr("k"))), VoyageEmbedder)
