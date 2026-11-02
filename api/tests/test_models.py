import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Chunk, Repo, RepoStatus, User
from tests.fakes import fake_vector


def _chunk(repo: Repo, symbol: str, content: str) -> Chunk:
    return Chunk(
        repo=repo,
        commit_sha="a" * 40,
        path="src/app.py",
        language="python",
        kind="function",
        symbol=symbol,
        start_line=1,
        end_line=3,
        content=content,
        embedding=fake_vector(content),
    )


def test_new_repo_starts_queued(db: Session) -> None:
    user = User(github_id=1, login="octocat")
    user.repos.append(Repo(full_name="octocat/hello-world"))
    db.add(user)
    db.commit()

    repo = db.scalars(select(Repo)).one()
    assert repo.status is RepoStatus.QUEUED
    assert repo.progress == 0
    assert repo.owner.login == "octocat"


def test_same_repo_cannot_be_connected_twice_by_one_user(db: Session) -> None:
    user = User(github_id=1, login="octocat")
    user.repos.extend([Repo(full_name="octocat/a"), Repo(full_name="octocat/a")])
    db.add(user)
    with pytest.raises(IntegrityError):
        db.commit()


def test_deleting_repo_deletes_its_chunks(db: Session) -> None:
    user = User(github_id=1, login="octocat")
    repo = Repo(full_name="octocat/a")
    user.repos.append(repo)
    db.add_all([user, _chunk(repo, "f", "def f(): pass")])
    db.commit()

    db.delete(repo)
    db.commit()

    assert db.scalar(select(Chunk.id)) is None


def test_nearest_neighbour_search_finds_identical_text(db: Session) -> None:
    repo = Repo(owner=User(github_id=1, login="octocat"), full_name="octocat/a")
    db.add_all(
        [
            _chunk(repo, "charge", "def charge(card): ..."),
            _chunk(repo, "refund", "def refund(payment): ..."),
            _chunk(repo, "login", "def login(user): ..."),
        ]
    )
    db.commit()

    query = fake_vector("def refund(payment): ...")
    best = db.scalars(
        select(Chunk.symbol).order_by(Chunk.embedding.cosine_distance(query)).limit(1)
    ).one()
    assert best == "refund"


def test_keyword_search_matches_exact_identifiers(db: Session) -> None:
    repo = Repo(owner=User(github_id=1, login="octocat"), full_name="octocat/a")
    db.add_all(
        [
            _chunk(repo, "verify_jwt", "def verify_jwt(token): return decode(token)"),
            _chunk(repo, "send_email", "def send_email(to): ..."),
        ]
    )
    db.commit()

    found = db.scalars(
        select(Chunk.symbol).where(
            Chunk.search.op("@@")(text("plainto_tsquery('simple', 'verify_jwt')"))
        )
    ).all()
    assert found == ["verify_jwt"]
