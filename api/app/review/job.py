"""Run one review in the background: fetch the PR → review each file → store the findings.

Progress goes to the reviews row as each file finishes, so the page can show it live. When
the GitHub App started the review, the findings are also posted on the pull request.
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
from app.review.github import GitHubClient, GitHubError, PullRequest
from app.review.publish import comment_for, review_body
from app.review.reviewer import ReviewResult, ReviewSettings, review_pull_request

log = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]
# Builds the GitHub client for a review: anonymous (or GITHUB_TOKEN) for reviews started on
# the page, signed in as the app's installation for reviews started by a webhook.
GitHubFactory = Callable[[int | None], GitHubClient]

PROGRESS_FETCHED = 5
PROGRESS_REVIEWED = 95  # the summary takes the rest

# Errors whose message is safe and useful to show on the page.
USER_FACING_ERRORS = (GitHubError, AnswerError, EmbeddingError)


def run_review(
    review_id: int,
    *,
    session_factory: SessionFactory,
    github_factory: GitHubFactory,
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
        installation_id = review.installation_id

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
        github = github_factory(installation_id)
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
    if installation_id is not None:
        _post(github, full_name, pr, result, update)


def _post(
    github: GitHubClient,
    full_name: str,
    pr: PullRequest,
    result: ReviewResult,
    update: Callable[..., None],
) -> None:
    """Comment on the PR. A failure here keeps the review; it's shown on the page instead."""
    try:
        url = github.post_review(
            full_name,
            pr.number,
            commit_sha=pr.head_sha,
            body=review_body(result, pr.head_sha),
            comments=[comment_for(f) for f in result.findings],
        )
    except GitHubError as exc:
        log.info("Posting review of %s#%s failed: %s", full_name, pr.number, exc)
        update(post_error=f"Couldn't post on GitHub: {exc}")
        return
    except Exception:
        log.exception("Posting review of %s#%s crashed", full_name, pr.number)
        update(post_error="Couldn't post on GitHub.")
        return
    update(github_review_url=url, post_error=None)
