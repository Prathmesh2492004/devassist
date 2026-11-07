import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chunk, Message, Repo, RepoStatus, User
from app.qa.answer import AnswerError
from app.routes.repos import LOCAL_USER_GITHUB_ID
from tests.fakes import FakeAnswerer, fake_vector

SHA = "c" * 40


def _events(body: str) -> list[tuple[str, object]]:
    """Parse a Server-Sent Events body into (event, data) pairs."""
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def _answer_text(events: list[tuple[str, object]]) -> str:
    return "".join(data["text"] for name, data in events if name == "delta")  # type: ignore[index]


@pytest.fixture
def repo(db: Session) -> Repo:
    user = User(github_id=LOCAL_USER_GITHUB_ID, login="local-dev")
    repo = Repo(
        owner=user,
        full_name="octocat/shop",
        status=RepoStatus.READY,
        last_indexed_sha=SHA,
        chunk_count=1,
    )
    content = "def charge(card):\n    return gateway.pay(card)"
    db.add(
        Chunk(
            repo=repo,
            commit_sha=SHA,
            path="src/billing.py",
            language="python",
            kind="function",
            symbol="charge",
            start_line=4,
            end_line=5,
            content=content,
            embedding=fake_vector(content),
        )
    )
    db.commit()
    return repo


def _ask(
    client: TestClient, repo: Repo, question: str, **extra: object
) -> list[tuple[str, object]]:
    res = client.post(f"/repos/{repo.id}/ask", json={"question": question, **extra})
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/event-stream")
    return _events(res.text)


def test_streams_sources_then_answer_then_saved_message(
    client: TestClient, repo: Repo, answerer: FakeAnswerer, db: Session
) -> None:
    events = _ask(client, repo, "How are cards charged?")

    names = [name for name, _ in events]
    assert names[:2] == ["conversation", "sources"]
    assert names[-1] == "done"
    assert set(names[2:-1]) == {"delta"}
    assert _answer_text(events) == answerer.answer

    sources = events[1][1]
    assert sources == [
        {
            "path": "src/billing.py",
            "start_line": 4,
            "end_line": 5,
            "symbol": "charge",
            "language": "python",
            "content": "def charge(card):\n    return gateway.pay(card)",
        }
    ]
    done = events[-1][1]
    assert isinstance(done, dict)
    assert done["role"] == "assistant"
    assert done["content"] == answerer.answer
    assert done["commit_sha"] == SHA
    assert done["model"] == "fake-model"
    assert done["input_tokens"] == 1000 and done["latency_ms"] is not None

    system, messages = answerer.calls[0]
    assert "octocat/shop" in system
    assert 'path="src/billing.py" lines="4-5"' in str(messages[-1]["content"])


def test_question_and_answer_are_saved_in_a_new_conversation(
    client: TestClient, repo: Repo, db: Session
) -> None:
    events = _ask(client, repo, "How are cards charged?")
    conversation_id = events[0][1]["id"]  # type: ignore[index]

    res = client.get(f"/conversations/{conversation_id}")

    assert res.status_code == 200
    body = res.json()
    assert body["title"] == "How are cards charged?"
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "How are cards charged?"),
        ("assistant", "It is in [src/billing.py:4-5]."),
    ]
    assert body["messages"][1]["sources"][0]["path"] == "src/billing.py"


def test_follow_up_sends_earlier_turns_and_stays_in_the_conversation(
    client: TestClient, repo: Repo, answerer: FakeAnswerer
) -> None:
    first = _ask(client, repo, "How are cards charged?")
    conversation_id = first[0][1]["id"]  # type: ignore[index]

    second = _ask(client, repo, "And where is that tested?", conversation_id=conversation_id)

    assert second[0][1]["id"] == conversation_id  # type: ignore[index]
    _, messages = answerer.calls[1]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "How are cards charged?"
    assert messages[1]["content"] == answerer.answer
    res = client.get(f"/repos/{repo.id}/conversations")
    assert [c["id"] for c in res.json()] == [conversation_id]


def test_failed_answers_are_saved_with_the_error_and_left_out_of_history(
    client: TestClient, repo: Repo, answerer: FakeAnswerer
) -> None:
    answerer.error = AnswerError("Claude API rate limit reached. Try again in a minute.")
    events = _ask(client, repo, "How are cards charged?")
    conversation_id = events[0][1]["id"]  # type: ignore[index]

    assert events[-1] == ("error", {"message": answerer.error.args[0]})
    saved = client.get(f"/conversations/{conversation_id}").json()["messages"][1]
    assert saved["error"] == answerer.error.args[0]

    answerer.error = None
    _ask(client, repo, "Try again?", conversation_id=conversation_id)
    _, messages = answerer.calls[1]
    assert len(messages) == 1  # the failed turn isn't sent back to the model


def test_unexpected_errors_are_not_shown_to_users(
    client: TestClient, repo: Repo, answerer: FakeAnswerer
) -> None:
    answerer.error = RuntimeError("internal detail: postgres://secret")

    events = _ask(client, repo, "How are cards charged?")

    name, data = events[-1]
    assert name == "error" and "secret" not in str(data)


def test_cannot_ask_before_the_repo_is_indexed(client: TestClient, repo: Repo, db: Session) -> None:
    repo.last_indexed_sha = None
    db.commit()

    res = client.post(f"/repos/{repo.id}/ask", json={"question": "Anything?"})

    assert res.status_code == 409
    assert db.scalar(select(Message.id)) is None


@pytest.mark.parametrize("question", ["", "   ", "x" * 2001])
def test_rejects_empty_or_huge_questions(client: TestClient, repo: Repo, question: str) -> None:
    assert client.post(f"/repos/{repo.id}/ask", json={"question": question}).status_code == 422


def test_cannot_continue_a_conversation_from_another_repo(
    client: TestClient, repo: Repo, db: Session
) -> None:
    conversation_id = _ask(client, repo, "How are cards charged?")[0][1]["id"]  # type: ignore[index]
    other = Repo(owner=repo.owner, full_name="octocat/other", last_indexed_sha=SHA, chunk_count=1)
    db.add(other)
    db.commit()

    res = client.post(
        f"/repos/{other.id}/ask", json={"question": "Hi", "conversation_id": conversation_id}
    )

    assert res.status_code == 404


def test_missing_conversation_is_not_found(client: TestClient) -> None:
    assert client.get("/conversations/999999").status_code == 404


def test_delete_conversation_removes_its_messages(
    client: TestClient, repo: Repo, db: Session
) -> None:
    conversation_id = _ask(client, repo, "How are cards charged?")[0][1]["id"]  # type: ignore[index]

    assert client.delete(f"/conversations/{conversation_id}").status_code == 204

    assert client.get(f"/conversations/{conversation_id}").status_code == 404
    db.expire_all()
    assert db.scalar(select(Message.id)) is None


def test_deleting_the_repo_deletes_its_conversations(client: TestClient, repo: Repo) -> None:
    conversation_id = _ask(client, repo, "How are cards charged?")[0][1]["id"]  # type: ignore[index]

    assert client.delete(f"/repos/{repo.id}").status_code == 204

    assert client.get(f"/conversations/{conversation_id}").status_code == 404
