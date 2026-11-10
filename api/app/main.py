import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.review.webhook_url import WebhookUrlKeeper, keep_webhook_url_current
from app.routes import conversations, github_app, health, repos, reviews, webhooks

logging.basicConfig(level=logging.INFO)
# httpx logs every request at INFO; the tunnel check alone would print one every 30 seconds.
logging.getLogger("httpx").setLevel(logging.WARNING)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if not settings.tunnel_metrics_url:  # a fixed webhook address never needs updating
        yield
        return
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        task = asyncio.create_task(keep_webhook_url_current(WebhookUrlKeeper(settings, client)))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(repos.router)
    app.include_router(conversations.router)
    app.include_router(reviews.router)
    app.include_router(webhooks.router)
    app.include_router(github_app.router)
    return app


app = create_app()
