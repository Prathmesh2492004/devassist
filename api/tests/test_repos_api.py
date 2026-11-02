import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Repo, RepoStatus


def test_add_repo_creates_it_and_queues_indexing(client: TestClient, enqueued: list[int]) -> None:
    res = client.post("/repos", json={"full_name": "tiangolo/typer"})

    assert res.status_code == 201
    body = res.json()
    assert body["full_name"] == "tiangolo/typer"
    assert body["status"] == "queued"
    assert body["default_branch"] is None
    assert body["index_seconds"] is None
    assert enqueued == [body["id"]]


@pytest.mark.parametrize(
    "full_name",
    ["", "typer", "a/b/c", "owner/../etc", "-owner/repo", "owner/repo; rm -rf /", "x" * 50 + "/y"],
)
def test_add_repo_rejects_invalid_names(
    client: TestClient, enqueued: list[int], full_name: str
) -> None:
    res = client.post("/repos", json={"full_name": full_name})

    assert res.status_code == 422
    assert enqueued == []


def test_add_repo_rejects_branch_names_that_look_like_options(client: TestClient) -> None:
    res = client.post("/repos", json={"full_name": "a/b", "branch": "--upload-pack=evil"})
    assert res.status_code == 422


def test_adding_the_same_repo_twice_conflicts(client: TestClient) -> None:
    assert client.post("/repos", json={"full_name": "tiangolo/typer"}).status_code == 201

    res = client.post("/repos", json={"full_name": "tiangolo/typer"})

    assert res.status_code == 409
    assert res.json()["detail"] == "tiangolo/typer is already connected"


def test_list_returns_newest_first(client: TestClient) -> None:
    client.post("/repos", json={"full_name": "a/first"})
    client.post("/repos", json={"full_name": "a/second"})

    names = [r["full_name"] for r in client.get("/repos").json()]

    assert names == ["a/second", "a/first"]


def test_reindex_requeues_a_finished_repo(
    client: TestClient, db: Session, enqueued: list[int]
) -> None:
    repo_id = client.post("/repos", json={"full_name": "a/b"}).json()["id"]
    repo = db.get(Repo, repo_id)
    assert repo is not None
    repo.status, repo.progress, repo.error = RepoStatus.FAILED, 40, "boom"
    db.commit()

    res = client.post(f"/repos/{repo_id}/reindex")

    assert res.status_code == 202
    assert res.json()["status"] == "queued"
    assert res.json()["error"] is None
    assert enqueued == [repo_id, repo_id]


def test_reindex_is_refused_while_indexing(client: TestClient) -> None:
    repo_id = client.post("/repos", json={"full_name": "a/b"}).json()["id"]  # status: queued

    assert client.post(f"/repos/{repo_id}/reindex").status_code == 409


def test_delete_then_get_returns_404(client: TestClient) -> None:
    repo_id = client.post("/repos", json={"full_name": "a/b"}).json()["id"]

    assert client.delete(f"/repos/{repo_id}").status_code == 204
    assert client.get(f"/repos/{repo_id}").status_code == 404
