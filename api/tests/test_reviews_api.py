import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Repo, RepoStatus, Review, ReviewStatus, User
from app.review.github import GitHubClient
from app.review.job import run_review
from app.routes.repos import LOCAL_USER_GITHUB_ID
from tests.fakes import FakeAnswerer, FakeEmbedder

PATCH = "@@ -1,2 +1,2 @@\n def charge(card):\n-    return 1\n+    return card.total * 1.03"


@pytest.fixture
def repo(db: Session) -> Repo:
    user = User(github_id=LOCAL_USER_GITHUB_ID, login="local-dev")
    repo = Repo(owner=user, full_name="octocat/shop", status=RepoStatus.READY)
    db.add(repo)
    db.commit()
    return repo


def test_starts_a_review_in_the_background(
    client: TestClient, repo: Repo, enqueued_reviews: list[int]
) -> None:
    res = client.post(f"/repos/{repo.id}/reviews", json={"pr_number": 7})

    assert res.status_code == 202, res.text
    body = res.json()
    assert (body["pr_number"], body["status"], body["finding_count"]) == (7, "queued", 0)
    assert enqueued_reviews == [body["id"]]


def test_refuses_a_repo_that_is_not_indexed(
    client: TestClient, repo: Repo, db: Session, enqueued_reviews: list[int]
) -> None:
    repo.status = RepoStatus.EMBEDDING
    db.commit()

    res = client.post(f"/repos/{repo.id}/reviews", json={"pr_number": 7})

    assert res.status_code == 409
    assert enqueued_reviews == []


def test_refuses_a_second_review_of_a_pr_while_one_runs(client: TestClient, repo: Repo) -> None:
    assert client.post(f"/repos/{repo.id}/reviews", json={"pr_number": 7}).status_code == 202

    res = client.post(f"/repos/{repo.id}/reviews", json={"pr_number": 7})

    assert res.status_code == 409
    assert "already being reviewed" in res.json()["detail"]


@pytest.mark.parametrize("number", [0, -3, "seven"])
def test_validates_the_pr_number(client: TestClient, repo: Repo, number: object) -> None:
    assert client.post(f"/repos/{repo.id}/reviews", json={"pr_number": number}).status_code == 422


def test_lists_reads_and_deletes_reviews(client: TestClient, repo: Repo) -> None:
    first = client.post(f"/repos/{repo.id}/reviews", json={"pr_number": 7}).json()
    second = client.post(f"/repos/{repo.id}/reviews", json={"pr_number": 8}).json()

    listed = client.get(f"/repos/{repo.id}/reviews").json()
    assert [r["id"] for r in listed] == [second["id"], first["id"]]

    detail = client.get(f"/reviews/{first['id']}").json()
    assert (detail["findings"], detail["files"], detail["summary"]) == ([], [], None)

    assert client.delete(f"/reviews/{first['id']}").status_code == 204
    assert client.get(f"/reviews/{first['id']}").status_code == 404


def test_hides_other_users_reviews(client: TestClient, db: Session) -> None:
    other = Repo(owner=User(github_id=99, login="someone"), full_name="x/y")
    review = Review(repo=other, pr_number=1, status=ReviewStatus.DONE, progress=100,
                    findings=[], files=[], input_tokens=0, output_tokens=0)  # fmt: skip
    db.add(review)
    db.commit()

    assert client.get(f"/reviews/{review.id}").status_code == 404
    assert client.delete(f"/reviews/{review.id}").status_code == 404


def _github(status_code: int = 200) -> GitHubClient:
    pull = {
        "number": 7,
        "title": "Add card fees",
        "body": "",
        "user": {"login": "octocat"},
        "html_url": "https://github.com/octocat/shop/pull/7",
        "head": {"sha": "d" * 40},
    }
    files = [{"filename": "src/billing.py", "status": "modified", "additions": 1,
              "deletions": 1, "patch": PATCH}]  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        if status_code != 200:
            return httpx.Response(status_code)
        return httpx.Response(200, json=files if request.url.path.endswith("/files") else pull)

    return GitHubClient(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _queued(db: Session, repo: Repo) -> Review:
    review = Review(repo=repo, pr_number=7, status=ReviewStatus.QUEUED, progress=0,
                    findings=[], files=[], input_tokens=0, output_tokens=0)  # fmt: skip
    db.add(review)
    db.commit()
    return review


def test_job_stores_findings_and_the_diff(
    db: Session, session_factory: sessionmaker[Session], repo: Repo
) -> None:
    review = _queued(db, repo)
    answerer = FakeAnswerer()
    answerer.json_replies = [
        json.dumps({"findings": [{"line": 2, "severity": "risk", "comment": "Float money."}]}),
        json.dumps({"summary": "Adds a fee."}),
    ]

    run_review(
        review.id,
        session_factory=session_factory,
        github=_github(),
        answerer_factory=lambda: answerer,
        embedder_factory=FakeEmbedder,
        settings=Settings(),
    )

    db.refresh(review)
    assert (review.status, review.progress, review.error) == (ReviewStatus.DONE, 100, None)
    assert (review.title, review.author, review.head_sha) == ("Add card fees", "octocat", "d" * 40)
    assert review.summary == "Adds a fee."
    assert review.findings == [
        {"path": "src/billing.py", "line": 2, "severity": "risk", "comment": "Float money."}
    ]
    assert review.files[0]["patch"] == PATCH
    assert review.finished_at is not None


def test_job_records_a_readable_failure(
    db: Session, session_factory: sessionmaker[Session], repo: Repo
) -> None:
    review = _queued(db, repo)

    run_review(
        review.id,
        session_factory=session_factory,
        github=_github(status_code=404),
        answerer_factory=FakeAnswerer,
        embedder_factory=FakeEmbedder,
        settings=Settings(),
    )

    db.refresh(review)
    assert review.status == ReviewStatus.FAILED
    assert review.error is not None and "wasn't found in octocat/shop" in review.error
