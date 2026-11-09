"""Run one review in the background: fetch the PR → review each file → store the findings.

Progress goes to the reviews row as each file finishes, so the page can show it live.
"""

import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import Settings
from app.ingest.embedder import Embedder, EmbeddingError
from app.models import Review, ReviewStatus
from app.qa.answer import Answerer, AnswerError
from app.qa.search import SearchHit, hybrid_search
from app.review.github import GitHubClient, GitHubError
from app.review.reviewer import ReviewSettings, review_pull_request

log = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

PROGRESS_FETCHED = 5
PROGRESS_REVIEWED = 95  # the summary takes the rest

# Errors whose message is safe and useful to show on the page.
USER_FACING_ERRORS = (GitHubError, AnswerError, EmbeddingError)


def run_review(
    review_id: int,
    *,
    session_factory: SessionFactory,
    github: GitHubClient,
    answerer_factory: Callable[[], Answerer],
    embedder_factory: Callable[[], Embedder],
    settings: Settings,
) -> None:
    with session_factory() as db:
        review = db.get(Review, review_id)
        if review is None:
            log.warning("Review %s was deleted before it started", review_id)
            return
        repo_id, full_name, number = review.repo_id, review.repo.full_name, review.pr_number

    def update(**fields: object) -> None:
        with session_factory() as db:
            review = db.get(Review, review_id)
            if review is None:
                return
            for key, value in fields.items():
                setattr(review, key, value)
            db.commit()

    update(status=ReviewStatus.RUNNING, progress=0, error=None)
    try:
        pr = github.pull_request(full_name, number)
        update(
            title=pr.title[:500],
            author=pr.author,
            html_url=pr.html_url,
            head_sha=pr.head_sha,
            progress=PROGRESS_FETCHED,
        )
        answerer = answerer_factory()
        embedder = embedder_factory()

        def related_code(query: str) -> list[SearchHit]:
            vector = embedder.embed_query(query)
            with session_factory() as db:
                hits = hybrid_search(db, repo_id, query, vector, limit=3)
                db.expunge_all()  # the chunks are read after the session closes
                return hits

        def on_progress(done: int, total: int) -> None:
            span = PROGRESS_REVIEWED - PROGRESS_FETCHED
            update(progress=PROGRESS_FETCHED + (span * done // total if total else span))

        result = review_pull_request(
            pr,
            full_name,
            answerer=answerer,
            related_code=related_code,
            file_content=lambda path: github.file_text(full_name, pr.head_sha, path),
            settings=ReviewSettings(
                # Focused checks suit small local models; strong models get an open review.
                strategy="open" if settings.answer_provider == "anthropic" else "checks",
                max_files=settings.review_max_files,
                max_findings_per_file=settings.review_max_findings_per_file,
            ),
            on_progress=on_progress,
        )
    except USER_FACING_ERRORS as exc:
        log.info("Review %s failed: %s", review_id, exc)
        update(status=ReviewStatus.FAILED, error=str(exc), finished_at=datetime.now(UTC))
        return
    except Exception:
        log.exception("Review %s crashed", review_id)
        update(
            status=ReviewStatus.FAILED,
            error="Something went wrong while reviewing. Try again.",
            finished_at=datetime.now(UTC),
        )
        return

    update(
        status=ReviewStatus.DONE,
        progress=100,
        summary=result.summary,
        findings=[asdict(f) for f in result.findings],
        files=[asdict(f) for f in result.files],
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        finished_at=datetime.now(UTC),
    )
