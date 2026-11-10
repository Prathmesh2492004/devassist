import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Repo, RepoStatus, Review, ReviewStatus, User
from app.review.app_auth import AppCredentials
from app.routes.repos import LOCAL_USER_GITHUB_ID
from app.routes.webhooks import get_app_credentials, signature_for

SECRET = "webhook-s3cret"
SHA = "a" * 40


def _set_credentials(client: TestClient, credentials: AppCredentials | None) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_app_credentials] = lambda: credentials


@pytest.fixture
def app_client(client: TestClient) -> TestClient:
    _set_credentials(client, AppCredentials(app_id="1", private_key="KEY", webhook_secret=SECRET))
    return client


@pytest.fixture
def repo(db: Session) -> Repo:
    user = User(github_id=LOCAL_USER_GITHUB_ID, login="local-dev")
    repo = Repo(owner=user, full_name="Octocat/Shop", status=RepoStatus.READY)
    db.add(repo)
    db.commit()
    return repo


def _event(
    action: str = "opened", *, draft: bool = False, sha: str = SHA, full_name: str = "octocat/shop"
) -> dict[str, Any]:
    return {
        "action": action,
        "installation": {"id": 555},
        "repository": {"full_name": full_name},
        "pull_request": {
            "number": 7,
            "title": "Add card fees",
            "draft": draft,
            "user": {"login": "octocat"},
            "html_url": "https://github.com/octocat/shop/pull/7",
            "head": {"sha": sha},
        },
    }


def _send(
    client: TestClient, payload: object, *, event: str = "pull_request", secret: str = SECRET
) -> Any:
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature_for(secret, body),
            "Content-Type": "application/json",
        },
    )


def test_a_new_pull_request_queues_a_review_that_will_be_posted(
    app_client: TestClient, repo: Repo, db: Session, enqueued_reviews: list[int]
) -> None:
    res = _send(app_client, _event())

    assert res.status_code == 202, res.text
    assert res.json()["outcome"] == "queued"
    review = db.get(Review, res.json()["review_id"])
    assert review is not None
    assert (review.repo_id, review.pr_number, review.head_sha) == (repo.id, 7, SHA)
    assert (review.installation_id, review.automatic) == (555, True)
    assert review.status == ReviewStatus.QUEUED
    assert enqueued_reviews == [review.id]


def test_new_commits_are_reviewed_but_a_resent_event_is_not(
    app_client: TestClient, repo: Repo, enqueued_reviews: list[int]
) -> None:
    assert _send(app_client, _event()).json()["outcome"] == "queued"

    again = _send(app_client, _event()).json()
    pushed = _send(app_client, _event("synchronize", sha="b" * 40)).json()

    assert (again["outcome"], pushed["outcome"]) == ("ignored", "queued")
    assert "already reviewed" in again["reason"]
    assert len(enqueued_reviews) == 2


def test_a_failed_review_can_be_retried_for_the_same_commit(
    app_client: TestClient, repo: Repo, db: Session
) -> None:
    first = db.get(Review, _send(app_client, _event()).json()["review_id"])
    assert first is not None
    first.status = ReviewStatus.FAILED
    db.commit()

    assert _send(app_client, _event("reopened")).json()["outcome"] == "queued"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (_event("closed"), "doesn't change the code"),
        (_event(draft=True), "draft"),
        (_event(full_name="someone/else"), "hasn't been added to DevAssist"),
    ],
)
def test_ignores_events_that_need_no_review(
    app_client: TestClient, repo: Repo, enqueued_reviews: list[int], payload: object, reason: str
) -> None:
    res = _send(app_client, payload)

    assert res.status_code == 202
    assert res.json()["outcome"] == "ignored"
    assert reason in res.json()["reason"]
    assert enqueued_reviews == []


def test_answers_githubs_ping_and_ignores_other_events(app_client: TestClient) -> None:
    assert _send(app_client, {"zen": "Keep it simple."}, event="ping").json()["reason"] == "pong"
    assert _send(app_client, {}, event="issues").json()["outcome"] == "ignored"


def test_rejects_events_without_githubs_signature(
    app_client: TestClient, repo: Repo, enqueued_reviews: list[int]
) -> None:
    assert _send(app_client, _event(), secret="guessed").status_code == 401
    unsigned = app_client.post(
        "/webhooks/github", json=_event(), headers={"X-GitHub-Event": "pull_request"}
    )
    assert unsigned.status_code == 401
    assert enqueued_reviews == []


def test_rejects_a_malformed_pull_request_event(app_client: TestClient) -> None:
    assert _send(app_client, {"action": "opened"}).status_code == 400
    assert _send(app_client, ["not", "an", "object"]).status_code == 400


def test_webhooks_are_refused_until_the_app_is_set_up(client: TestClient) -> None:
    _set_credentials(client, None)
    assert _send(client, _event()).status_code == 503
