from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery("devassist", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,  # a crashed worker's job goes back on the queue
    worker_prefetch_multiplier=1,  # indexing jobs are long; take one at a time
    task_track_started=True,
)


@celery_app.task(name="ping")
def ping() -> str:
    """Smoke-test task. Week 2 adds the real indexing tasks here."""
    return "pong"
