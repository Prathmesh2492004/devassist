import logging
from functools import partial

from celery import Celery

from app.config import get_settings
from app.db import SessionLocal
from app.ingest.embedder import build_embedder
from app.ingest.git import clone_repo
from app.ingest.pipeline import index_repository
from app.qa.answer import build_answerer
from app.review.github import GitHubClient
from app.review.job import run_review

settings = get_settings()
logging.basicConfig(level=logging.INFO)

celery_app = Celery("devassist", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,  # a crashed worker's job goes back on the queue
    worker_prefetch_multiplier=1,  # indexing jobs are long; take one at a time
    task_track_started=True,
    task_time_limit=30 * 60,
)


@celery_app.task(name="ping")
def ping() -> str:
    """Smoke-test task."""
    return "pong"


@celery_app.task(name="index_repo")
def index_repo(repo_id: int) -> None:
    index_repository(
        repo_id,
        session_factory=SessionLocal,
        embedder_factory=partial(build_embedder, settings),
        clone=partial(clone_repo, timeout=settings.clone_timeout_seconds),
        settings=settings,
    )


@celery_app.task(name="review_pull_request")
def review_pull_request(review_id: int) -> None:
    token = settings.github_token.get_secret_value() if settings.github_token else None
    run_review(
        review_id,
        session_factory=SessionLocal,
        github=GitHubClient(token),
        answerer_factory=partial(build_answerer, settings),
        embedder_factory=partial(build_embedder, settings),
        settings=settings,
    )
