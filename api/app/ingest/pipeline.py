"""Index one repository: clone → parse into chunks → embed → store.

Progress is written to the repos row after each stage so the dashboard can show it live.
The new chunks replace the old ones in a single transaction, so search never sees a
half-indexed repository.
"""

import logging
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.ingest.chunker import CodeChunk, chunk_file
from app.ingest.embedder import Embedder, EmbeddingError, iter_batches
from app.ingest.files import TooManyFilesError, iter_source_files
from app.ingest.git import CloneError, CloneResult
from app.models import Chunk, Repo, RepoStatus

log = logging.getLogger(__name__)

Cloner = Callable[[str, str | None, Path], CloneResult]
SessionFactory = Callable[[], Session]

# How the 0–100 progress bar is split between stages.
PROGRESS_CLONED = 10
PROGRESS_PARSED = 25

# Errors whose message is safe and useful to show in the dashboard.
USER_FACING_ERRORS = (CloneError, EmbeddingError, TooManyFilesError)


def index_repository(
    repo_id: int,
    *,
    session_factory: SessionFactory,
    embedder_factory: Callable[[], Embedder],
    clone: Cloner,
    settings: Settings,
) -> None:
    started = time.monotonic()
    with session_factory() as db:
        repo = db.get(Repo, repo_id)
        if repo is None:
            log.warning("Repo %s was deleted before indexing started", repo_id)
            return
        full_name, branch = repo.full_name, repo.default_branch

    def update(**fields: object) -> None:
        with session_factory() as db:
            repo = db.get(Repo, repo_id)
            if repo is None:
                return
            for key, value in fields.items():
                setattr(repo, key, value)
            db.commit()

    update(
        status=RepoStatus.CLONING,
        progress=0,
        error=None,
        indexing_started_at=datetime.now(UTC),
    )
    try:
        embedder = embedder_factory()  # fail fast on a missing API key, before cloning
        with tempfile.TemporaryDirectory(prefix="devassist-") as tmp:
            checkout = Path(tmp) / "repo"
            cloned = clone(full_name, branch, checkout)
            update(
                status=RepoStatus.PARSING, progress=PROGRESS_CLONED, default_branch=cloned.branch
            )

            chunks: list[CodeChunk] = []
            file_count = 0
            for source in iter_source_files(
                checkout, max_file_bytes=settings.max_file_bytes, max_files=settings.max_files
            ):
                file_count += 1
                chunks.extend(chunk_file(source.path, source.language, source.text))
        log.info("%s: %d files, %d chunks", full_name, file_count, len(chunks))

        update(
            status=RepoStatus.EMBEDDING,
            progress=PROGRESS_PARSED,
            file_count=file_count,
            chunk_count=len(chunks),
        )
        texts = [_embedding_text(c) for c in chunks]
        vectors: list[list[float]] = []
        tokens = 0
        for start, batch in iter_batches(texts, embedder.batch_items, embedder.batch_chars):
            result = embedder.embed_documents(batch)
            vectors.extend(result.vectors)
            tokens += result.tokens
            done = start + len(batch)
            update(
                # Capped at 99: 100 means "stored and searchable", which happens below.
                progress=min(99, PROGRESS_PARSED + (100 - PROGRESS_PARSED) * done // len(texts)),
                embedding_tokens=tokens,
            )

        with session_factory() as db:
            db.execute(delete(Chunk).where(Chunk.repo_id == repo_id))
            if chunks:
                db.execute(
                    insert(Chunk),
                    [
                        {
                            "repo_id": repo_id,
                            "commit_sha": cloned.sha,
                            "path": c.path,
                            "language": c.language,
                            "kind": c.kind,
                            "symbol": c.symbol,
                            "start_line": c.start_line,
                            "end_line": c.end_line,
                            "content": c.content,
                            "embedding": vector,
                        }
                        for c, vector in zip(chunks, vectors, strict=True)
                    ],
                )
            repo = db.get(Repo, repo_id)
            if repo is None:  # deleted while we were indexing; drop the work
                db.rollback()
                return
            repo.status = RepoStatus.READY
            repo.progress = 100
            repo.last_indexed_sha = cloned.sha
            repo.indexed_at = datetime.now(UTC)
            repo.embedding_tokens = tokens
            db.commit()
        log.info("%s indexed in %.1fs", full_name, time.monotonic() - started)

    except Exception as exc:
        log.exception("Indexing %s failed", full_name)
        update(status=RepoStatus.FAILED, error=_user_message(exc))
        raise


def _embedding_text(chunk: CodeChunk) -> str:
    # The path and symbol carry meaning the code alone may not, e.g. "billing/retry.py".
    header = f"File: {chunk.path}"
    if chunk.symbol:
        header += f"\nSymbol: {chunk.symbol}"
    return f"{header}\n\n{chunk.content}"


def _user_message(exc: Exception) -> str:
    if isinstance(exc, USER_FACING_ERRORS):
        return str(exc)
    return "Indexing failed because of an internal error. Check the worker logs."
