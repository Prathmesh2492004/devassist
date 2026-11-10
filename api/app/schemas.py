from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, computed_field

from app.ingest.git import BRANCH_PATTERN, FULL_NAME_PATTERN
from app.models import RepoStatus, ReviewStatus


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


class AskIn(BaseModel):
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    conversation_id: int | None = Field(
        default=None, description="Continue this conversation. Omit to start a new one."
    )


class SourceOut(BaseModel):
    """A chunk of code shown to the model. Citations in the answer point into these."""

    path: str
    start_line: int
    end_line: int
    symbol: str | None
    language: str
    content: str


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    sources: list[SourceOut] | None
    commit_sha: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    error: str | None
    created_at: datetime


class ConversationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_id: int
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationOut(ConversationSummary):
    messages: list[MessageOut]


class ReviewIn(BaseModel):
    pr_number: int = Field(gt=0, description="Pull request number in the repository")


class FindingOut(BaseModel):
    path: str
    line: int
    severity: str  # bug, risk or suggestion
    comment: str
    # "rule" for a linter rule (reliable), "model" for the AI's judgement (a hint to check)
    source: str = "model"


class ReviewedFileOut(BaseModel):
    path: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    skipped: str | None  # why the file wasn't reviewed


class ReviewSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_id: int
    pr_number: int
    title: str | None
    author: str | None
    html_url: str | None
    head_sha: str | None
    status: ReviewStatus
    progress: int
    error: str | None
    finding_count: int
    # True when the GitHub App started it; github_review_url links to the posted comments.
    automatic: bool
    github_review_url: str | None
    post_error: str | None
    created_at: datetime
    finished_at: datetime | None


class ReviewOut(ReviewSummary):
    summary: str | None
    findings: list[FindingOut]
    files: list[ReviewedFileOut]
    model: str | None
    input_tokens: int
    output_tokens: int
