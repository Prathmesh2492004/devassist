import enum
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# voyage-code-3 supports 256/512/1024/2048; 1024 is its default and a good quality/size trade-off.
EMBEDDING_DIMENSIONS = 1024


class RepoStatus(enum.StrEnum):
    QUEUED = "queued"
    CLONING = "cloning"
    PARSING = "parsing"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"

    @property
    def is_active(self) -> bool:
        return self in {
            RepoStatus.QUEUED,
            RepoStatus.CLONING,
            RepoStatus.PARSING,
            RepoStatus.EMBEDDING,
        }


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
    # None means "whatever the repo's default branch is"; filled in after the first clone.
    default_branch: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[RepoStatus] = mapped_column(
        Enum(RepoStatus, name="repo_status", values_callable=lambda e: [m.value for m in e]),
        default=RepoStatus.QUEUED,
    )
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0–100, for the UI
    error: Mapped[str | None] = mapped_column(Text)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    last_indexed_sha: Mapped[str | None] = mapped_column(String(40))
    indexing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner: Mapped[User] = relationship(back_populates="repos")
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan", passive_deletes=True
    )


class Chunk(Base):
    """One searchable piece of code: a function, class, method, or run of module-level code."""

    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_search", "search", postgresql_using="gin"),
        Index("ix_chunks_repo_path", "repo_id", "path"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    commit_sha: Mapped[str] = mapped_column(String(40))
    path: Mapped[str] = mapped_column(String(1000))
    language: Mapped[str] = mapped_column(String(30))
    kind: Mapped[str] = mapped_column(String(20))  # function, class, method, type, module, text
    symbol: Mapped[str | None] = mapped_column(String(300))  # e.g. "PaymentService.retry_charge"
    start_line: Mapped[int] = mapped_column(Integer)  # 1-based, inclusive
    end_line: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    # Keyword search for exact identifiers that embeddings can miss. 'simple' keeps code tokens
    # as-is instead of stemming them like English words.
    search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(symbol, '') || ' ' || path || ' ' || content)",
            persisted=True,
        ),
    )

    repo: Mapped[Repo] = relationship(back_populates="chunks")
