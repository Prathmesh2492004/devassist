"""Ask for an AI review of a pull request and read the results.

Reviews run in the Celery worker, since a local model takes a minute or more per PR. The
page polls GET /reviews/{id} until the status is done or failed.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import RepoStatus, Review, ReviewStatus, User
from app.routes.repos import CurrentUser, get_owned_repo
from app.schemas import ReviewIn, ReviewOut, ReviewSummary

router = APIRouter(tags=["reviews"])

Db = Annotated[Session, Depends(get_db)]
Enqueue = Callable[[int], None]


def get_enqueue_review() -> Enqueue:
    from app.worker import review_pull_request  # lazily, so the API starts without Celery

    def enqueue(review_id: int) -> None:
        review_pull_request.delay(review_id)

    return enqueue


EnqueueReview = Annotated[Enqueue, Depends(get_enqueue_review)]


def _get_owned_review(db: Session, user: User, review_id: int) -> Review:
    review = db.get(Review, review_id)
    if review is None or review.repo.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    return review


@router.post("/repos/{repo_id}/reviews", status_code=status.HTTP_202_ACCEPTED)
def create_review(
    repo_id: int, body: ReviewIn, db: Db, user: CurrentUser, enqueue: EnqueueReview
) -> ReviewSummary:
    """Start reviewing a pull request. Related code comes from the repo's index."""
    repo = get_owned_repo(db, user, repo_id)
    if repo.status != RepoStatus.READY:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Index the repository before reviewing its pull requests"
        )
    running = db.scalar(
        select(Review).where(
            Review.repo_id == repo.id,
            Review.pr_number == body.pr_number,
            Review.status.in_([ReviewStatus.QUEUED, ReviewStatus.RUNNING]),
        )
    )
    if running is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Pull request #{body.pr_number} is already being reviewed"
        )
    review = Review(
        repo_id=repo.id,
        pr_number=body.pr_number,
        status=ReviewStatus.QUEUED,
        progress=0,
        findings=[],
        files=[],
        input_tokens=0,
        output_tokens=0,
    )
    db.add(review)
    db.commit()
    enqueue(review.id)
    return ReviewSummary.model_validate(review)


@router.get("/repos/{repo_id}/reviews")
def list_reviews(repo_id: int, db: Db, user: CurrentUser) -> list[ReviewSummary]:
    repo = get_owned_repo(db, user, repo_id)
    reviews = db.scalars(
        select(Review)
        .where(Review.repo_id == repo.id)
        .order_by(Review.created_at.desc(), Review.id.desc())
    )
    return [ReviewSummary.model_validate(r) for r in reviews]


@router.get("/reviews/{review_id}")
def get_review(review_id: int, db: Db, user: CurrentUser) -> ReviewOut:
    return ReviewOut.model_validate(_get_owned_review(db, user, review_id))


@router.delete("/reviews/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_review(review_id: int, db: Db, user: CurrentUser) -> None:
    db.delete(_get_owned_review(db, user, review_id))
    db.commit()
