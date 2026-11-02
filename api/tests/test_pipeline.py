from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.ingest.embedder import Embedder, MissingApiKeyError
from app.ingest.git import CloneError, CloneResult
from app.ingest.pipeline import index_repository
from app.models import Chunk, Repo, RepoStatus, User
from tests.fakes import FakeCloner, FakeEmbedder


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    root = tmp_path / "sample"
    (root / "src").mkdir(parents=True)
    (root / "src" / "billing.py").write_text(
        "import os\n\n\ndef charge(card):\n    return 1\n\n\ndef refund(id):\n    return 2\n"
    )
    (root / "README.md").write_text("# Sample\n\nA tiny repo.\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("module.exports = 1\n")
    return root


@pytest.fixture
def repo(db: Session) -> Repo:
    repo = Repo(owner=User(github_id=1, login="octocat"), full_name="octocat/sample")
    db.add(repo)
    db.commit()
    return repo


def _run(
    repo: Repo,
    session_factory: sessionmaker[Session],
    *,
    clone: object,
    embedder: Embedder | None = None,
    embedder_factory: object = None,
) -> None:
    index_repository(
        repo.id,
        session_factory=session_factory,
        embedder_factory=embedder_factory or (lambda: embedder),  # type: ignore[arg-type]
        clone=clone,  # type: ignore[arg-type]
        settings=Settings(),
    )


def test_indexes_repo_and_stores_chunks_with_vectors(
    repo: Repo, sample_repo: Path, db: Session, session_factory: sessionmaker[Session]
) -> None:
    embedder = FakeEmbedder()

    _run(repo, session_factory, clone=FakeCloner(sample_repo, sha="b" * 40), embedder=embedder)

    db.refresh(repo)
    assert repo.status is RepoStatus.READY
    assert repo.progress == 100
    assert repo.file_count == 2  # node_modules skipped
    assert repo.chunk_count == 4  # imports, charge, refund, README
    assert repo.last_indexed_sha == "b" * 40
    assert repo.default_branch == "main"
    assert repo.embedding_tokens == 40
    assert repo.indexed_at is not None and repo.indexing_started_at is not None

    chunks = db.scalars(select(Chunk).where(Chunk.repo_id == repo.id).order_by(Chunk.id)).all()
    assert [(c.path, c.symbol, c.start_line, c.end_line) for c in chunks] == [
        ("README.md", None, 1, 4),
        ("src/billing.py", None, 1, 1),
        ("src/billing.py", "charge", 4, 5),
        ("src/billing.py", "refund", 8, 9),
    ]
    assert all(len(c.embedding) == 1024 and c.commit_sha == "b" * 40 for c in chunks)


def test_reindexing_replaces_old_chunks(
    repo: Repo, sample_repo: Path, db: Session, session_factory: sessionmaker[Session]
) -> None:
    _run(repo, session_factory, clone=FakeCloner(sample_repo), embedder=FakeEmbedder())
    (sample_repo / "src" / "billing.py").write_text("def only_one():\n    pass\n")

    _run(repo, session_factory, clone=FakeCloner(sample_repo), embedder=FakeEmbedder())

    symbols = db.scalars(select(Chunk.symbol).where(Chunk.repo_id == repo.id)).all()
    assert sorted(s or "" for s in symbols) == ["", "only_one"]


def test_clone_failure_marks_repo_failed_with_message(
    repo: Repo, db: Session, session_factory: sessionmaker[Session]
) -> None:
    def failing_clone(full_name: str, branch: str | None, dest: Path) -> CloneResult:
        raise CloneError("Repository not found, or it is private.")

    with pytest.raises(CloneError):
        _run(repo, session_factory, clone=failing_clone, embedder=FakeEmbedder())

    db.refresh(repo)
    assert repo.status is RepoStatus.FAILED
    assert repo.error == "Repository not found, or it is private."


def test_missing_api_key_fails_before_cloning(
    repo: Repo, sample_repo: Path, db: Session, session_factory: sessionmaker[Session]
) -> None:
    cloner = FakeCloner(sample_repo)
    cloned: list[str] = []

    def tracking_clone(full_name: str, branch: str | None, dest: Path) -> CloneResult:
        cloned.append(full_name)
        return cloner(full_name, branch, dest)

    def no_key() -> Embedder:
        raise MissingApiKeyError("VOYAGE_API_KEY is not set.")

    with pytest.raises(MissingApiKeyError):
        _run(repo, session_factory, clone=tracking_clone, embedder_factory=no_key)

    db.refresh(repo)
    assert repo.status is RepoStatus.FAILED
    assert repo.error is not None and "VOYAGE_API_KEY" in repo.error
    assert cloned == []


def test_unexpected_errors_are_not_shown_to_users(
    repo: Repo, sample_repo: Path, db: Session, session_factory: sessionmaker[Session]
) -> None:
    class BrokenEmbedder:
        def embed_documents(self, texts: object) -> object:
            raise RuntimeError("internal detail: connection string postgres://secret")

    with pytest.raises(RuntimeError):
        _run(
            repo,
            session_factory,
            clone=FakeCloner(sample_repo),
            embedder=BrokenEmbedder(),  # type: ignore[arg-type]
        )

    db.refresh(repo)
    assert repo.status is RepoStatus.FAILED
    assert repo.error is not None and "secret" not in repo.error
