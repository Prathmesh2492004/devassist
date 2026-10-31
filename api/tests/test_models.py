import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Repo, RepoStatus, User


def test_new_repo_starts_pending(db: Session) -> None:
    user = User(github_id=1, login="octocat")
    user.repos.append(Repo(full_name="octocat/hello-world"))
    db.add(user)
    db.commit()

    repo = db.query(Repo).one()
    assert repo.status is RepoStatus.PENDING
    assert repo.default_branch == "main"
    assert repo.owner.login == "octocat"


def test_same_repo_cannot_be_connected_twice_by_one_user(db: Session) -> None:
    user = User(github_id=1, login="octocat")
    user.repos.extend([Repo(full_name="octocat/a"), Repo(full_name="octocat/a")])
    db.add(user)
    with pytest.raises(IntegrityError):
        db.commit()


def test_deleting_user_deletes_their_repos(db: Session) -> None:
    user = User(github_id=1, login="octocat")
    user.repos.append(Repo(full_name="octocat/a"))
    db.add(user)
    db.commit()

    db.delete(user)
    db.commit()

    assert db.query(Repo).count() == 0
