import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RepoStatus(enum.StrEnum):
    PENDING = "pending"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    login: Mapped[str] = mapped_column(String(100))
    name: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    repos: Mapped[list["Repo"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Repo(Base):
    __tablename__ = "repos"
    __table_args__ = (UniqueConstraint("owner_id", "full_name", name="uq_repos_owner_full_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str] = mapped_column(String(200))  # e.g. "fastapi/fastapi"
    default_branch: Mapped[str] = mapped_column(String(100), default="main")
    status: Mapped[RepoStatus] = mapped_column(
        Enum(RepoStatus, name="repo_status", values_callable=lambda e: [m.value for m in e]),
        default=RepoStatus.PENDING,
    )
    last_indexed_sha: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner: Mapped[User] = relationship(back_populates="repos")
