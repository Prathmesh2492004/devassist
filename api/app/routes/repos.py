from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Repo, RepoStatus, User
from app.schemas import RepoCreate, RepoOut

router = APIRouter(prefix="/repos", tags=["repos"])

Db = Annotated[Session, Depends(get_db)]
Enqueue = Callable[[int], None]

# Until GitHub sign-in lands in Week 5, every request acts as this one local user.
LOCAL_USER_GITHUB_ID = 0


def get_current_user(db: Db) -> User:
    user = db.scalar(select(User).where(User.github_id == LOCAL_USER_GITHUB_ID))
    if user is None:
        user = User(github_id=LOCAL_USER_GITHUB_ID, login="local-dev", name="Local developer")
        db.add(user)
        db.commit()
    return user


def get_enqueue_index() -> Enqueue:
    from app.worker import index_repo  # imported lazily so the API starts without Celery config

    def enqueue(repo_id: int) -> None:
        index_repo.delay(repo_id)

    return enqueue


CurrentUser = Annotated[User, Depends(get_current_user)]
EnqueueIndex = Annotated[Enqueue, Depends(get_enqueue_index)]


def get_owned_repo(db: Session, user: User, repo_id: int) -> Repo:
    repo = db.get(Repo, repo_id)
    if repo is None or repo.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")
    return repo


@router.get("")
def list_repos(db: Db, user: CurrentUser) -> list[RepoOut]:
    repos = db.scalars(
        select(Repo)
        .where(Repo.owner_id == user.id)
        .order_by(Repo.created_at.desc(), Repo.id.desc())
    )
    return [RepoOut.model_validate(r) for r in repos]


@router.post("", status_code=status.HTTP_201_CREATED)
def add_repo(body: RepoCreate, db: Db, user: CurrentUser, enqueue: EnqueueIndex) -> RepoOut:
    """Connect a public GitHub repository and start indexing it."""
    repo = Repo(owner_id=user.id, full_name=body.full_name, default_branch=body.branch)
    db.add(repo)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{body.full_name} is already connected"
        ) from None
    enqueue(repo.id)
    return RepoOut.model_validate(repo)


@router.get("/{repo_id}")
def get_repo(repo_id: int, db: Db, user: CurrentUser) -> RepoOut:
    return RepoOut.model_validate(get_owned_repo(db, user, repo_id))


@router.post("/{repo_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
def reindex_repo(repo_id: int, db: Db, user: CurrentUser, enqueue: EnqueueIndex) -> RepoOut:
    repo = get_owned_repo(db, user, repo_id)
    if repo.status.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT, "This repository is already being indexed")
    repo.status = RepoStatus.QUEUED
    repo.progress = 0
    repo.error = None
    db.commit()
    enqueue(repo.id)
    return RepoOut.model_validate(repo)


@router.delete("/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_repo(repo_id: int, db: Db, user: CurrentUser) -> None:
    db.delete(get_owned_repo(db, user, repo_id))
    db.commit()
