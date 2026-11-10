"""GitHub calls this when a pull request opens or changes on a repo where the app is installed.

Anyone can send a request to a public URL, so every event must carry GitHub's signature: an
HMAC of the exact body, keyed with the webhook secret only GitHub and DevAssist know. Events
are answered right away (GitHub gives up after 10 seconds); the review runs in the worker.
"""

import hashlib
import hmac
import json
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from app.config import get_settings
from app.models import Repo, Review, ReviewStatus
from app.review.app_auth import AppCredentials, load_credentials
from app.routes.reviews import Db, EnqueueReview

log = logging.getLogger(__name__)

router = APIRouter(tags=["github"])

# "synchronize" means new commits were pushed to the PR.
REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def get_app_credentials() -> AppCredentials | None:
    return load_credentials(get_settings())


AppCreds = Annotated[AppCredentials | None, Depends(get_app_credentials)]


def signature_for(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def verified_payload(request: Request, credentials: AppCreds) -> dict[str, Any]:
    if credentials is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The GitHub App isn't set up")
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    # compare_digest takes the same time however much matches, so timing reveals nothing.
    if not hmac.compare_digest(signature, signature_for(credentials.webhook_secret, body)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body isn't JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body isn't a JSON object")
    return payload


VerifiedPayload = Annotated[dict[str, Any], Depends(verified_payload)]


class _Login(BaseModel):
    login: str


class _Head(BaseModel):
    sha: str


class _PullRequest(BaseModel):
    number: int
    title: str
    draft: bool = False
    user: _Login | None = None
    html_url: str
    head: _Head


class _Installation(BaseModel):
    id: int


class _Repository(BaseModel):
    full_name: str


class PullRequestEvent(BaseModel):
    action: str
    pull_request: _PullRequest
    repository: _Repository
    installation: _Installation


class WebhookResult(BaseModel):
    outcome: Literal["queued", "ignored"]
    reason: str | None = None
    review_id: int | None = None


def _ignored(reason: str) -> WebhookResult:
    return WebhookResult(outcome="ignored", reason=reason)


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
def github_webhook(
    payload: VerifiedPayload,
    db: Db,
    enqueue: EnqueueReview,
    x_github_event: Annotated[str, Header()] = "",
) -> WebhookResult:
    if x_github_event == "ping":  # sent once when the webhook is created
        return _ignored("pong")
    if x_github_event != "pull_request":
        return _ignored(f"{x_github_event or 'unknown'} events aren't used")
    try:
        event = PullRequestEvent.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unexpected pull_request event") from exc
    pull = event.pull_request
    if event.action not in REVIEW_ACTIONS:
        return _ignored(f"'{event.action}' doesn't change the code")
    if pull.draft:
        return _ignored("draft pull requests are reviewed when marked ready")

    full_name = event.repository.full_name
    repo = db.scalar(
        select(Repo)
        .where(func.lower(Repo.full_name) == full_name.lower())
        .order_by(Repo.id)
        .limit(1)
    )
    if repo is None:
        return _ignored(f"{full_name} hasn't been added to DevAssist")
    # GitHub resends events it thinks failed; a commit is only reviewed once.
    already = db.scalar(
        select(Review.id).where(
            Review.repo_id == repo.id,
            Review.pr_number == pull.number,
            Review.head_sha == pull.head.sha,
            Review.status != ReviewStatus.FAILED,
        )
    )
    if already is not None:
        return _ignored(f"commit {pull.head.sha[:7]} is already reviewed (review {already})")

    review = Review(
        repo_id=repo.id,
        pr_number=pull.number,
        title=pull.title[:500],
        author=pull.user.login if pull.user else None,
        html_url=pull.html_url,
        head_sha=pull.head.sha,
        installation_id=event.installation.id,
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
    log.info("Queued review %s for %s#%s", review.id, full_name, pull.number)
    return WebhookResult(outcome="queued", review_id=review.id)
