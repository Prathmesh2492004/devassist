from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, computed_field

from app.ingest.git import BRANCH_PATTERN, FULL_NAME_PATTERN
from app.models import RepoStatus


class RepoCreate(BaseModel):
    full_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, pattern=FULL_NAME_PATTERN.pattern),
        Field(examples=["tiangolo/typer"], description="GitHub repository as owner/name"),
    ]
    branch: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, pattern=BRANCH_PATTERN.pattern),
        Field(description="Branch to index. Defaults to the repository's default branch."),
    ] = None


class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    default_branch: str | None
    status: RepoStatus
    progress: int
    error: str | None
    file_count: int
    chunk_count: int
    embedding_tokens: int
    last_indexed_sha: str | None
    indexing_started_at: datetime | None
    indexed_at: datetime | None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def index_seconds(self) -> float | None:
        """How long the last successful index took."""
        if self.indexed_at is None or self.indexing_started_at is None:
            return None
        return round((self.indexed_at - self.indexing_started_at).total_seconds(), 1)
