import hashlib
import math
import shutil
from collections.abc import Iterator, Sequence
from pathlib import Path

from anthropic.types.beta import BetaMessageParam

from app.ingest.embedder import EmbeddingResult
from app.ingest.git import CloneResult
from app.models import EMBEDDING_DIMENSIONS
from app.qa.answer import AnswerDelta, AnswerDone


class FakeEmbedder:
    """Deterministic embeddings derived from a hash of the text. No network calls."""

    batch_items = 128
    batch_chars = 240_000

    def __init__(self) -> None:
        self.calls: list[int] = []

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        self.calls.append(len(texts))
        return EmbeddingResult(vectors=[fake_vector(t) for t in texts], tokens=len(texts) * 10)

    def embed_query(self, text: str) -> list[float]:
        return fake_vector(text)


def fake_vector(text: str) -> list[float]:
    seed = hashlib.sha256(text.encode()).digest()
    raw = [((seed[i % len(seed)] + i) % 251) - 125.0 for i in range(EMBEDDING_DIMENSIONS)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


class FakeAnswerer:
    """Streams a fixed answer and records what it was asked."""

    model = "fake-model"

    def __init__(self, answer: str = "It is in [src/billing.py:4-5].") -> None:
        self.answer = answer
        self.calls: list[tuple[str, list[BetaMessageParam]]] = []
        self.error: Exception | None = None

    def stream(
        self, system: str, messages: Sequence[BetaMessageParam]
    ) -> Iterator[AnswerDelta | AnswerDone]:
        self.calls.append((system, list(messages)))
        words = self.answer.split(" ")
        for i, word in enumerate(words):
            yield AnswerDelta(word if i == len(words) - 1 else word + " ")
            if self.error is not None and i == 0:
                raise self.error
        yield AnswerDone(model=self.model, input_tokens=1000, output_tokens=len(words))


class FakeCloner:
    """Copies a local directory instead of cloning from GitHub."""

    def __init__(self, source: Path, sha: str = "a" * 40, branch: str = "main") -> None:
        self.source = source
        self.sha = sha
        self.branch = branch

    def __call__(self, full_name: str, branch: str | None, dest: Path) -> CloneResult:
        shutil.copytree(self.source, dest)
        return CloneResult(sha=self.sha, branch=branch or self.branch)
