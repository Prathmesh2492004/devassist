import hashlib
import math
import shutil
from collections.abc import Sequence
from pathlib import Path

from app.ingest.embedder import EmbeddingResult
from app.ingest.git import CloneResult
from app.models import EMBEDDING_DIMENSIONS


class FakeEmbedder:
    """Deterministic embeddings derived from a hash of the text. No network calls."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        self.calls.append(len(texts))
        return EmbeddingResult(vectors=[fake_vector(t) for t in texts], tokens=len(texts) * 10)


def fake_vector(text: str) -> list[float]:
    seed = hashlib.sha256(text.encode()).digest()
    raw = [((seed[i % len(seed)] + i) % 251) - 125.0 for i in range(EMBEDDING_DIMENSIONS)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


class FakeCloner:
    """Copies a local directory instead of cloning from GitHub."""

    def __init__(self, source: Path, sha: str = "a" * 40, branch: str = "main") -> None:
        self.source = source
        self.sha = sha
        self.branch = branch

    def __call__(self, full_name: str, branch: str | None, dest: Path) -> CloneResult:
        shutil.copytree(self.source, dest)
        return CloneResult(sha=self.sha, branch=branch or self.branch)
