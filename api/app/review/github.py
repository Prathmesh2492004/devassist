"""Read pull requests from the GitHub REST API.

Public repositories need no token, but anonymous requests are limited to 60 per hour per IP
address. A free personal access token (GITHUB_TOKEN, no scopes needed for public repos)
raises that to 5,000.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict
from urllib.parse import quote

import httpx

GITHUB_API = "https://api.github.com"
# Raw file downloads don't count against the REST API's hourly limit.
GITHUB_RAW = "https://raw.githubusercontent.com"
MAX_FILE_BYTES = 500_000
FILES_PER_PAGE = 100
MAX_FILE_PAGES = 3  # GitHub returns at most 3,000 files; 300 is plenty to review


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str  # added, modified, removed, renamed, ...
    additions: int
    deletions: int
    patch: str | None  # None for binary files and diffs too large for GitHub to show


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    author: str | None
    html_url: str
    head_sha: str
    files: list[ChangedFile]


class GitHubError(Exception):
    """A failure whose message is safe and useful to show the user."""


class _User(TypedDict):
    login: str


class _Head(TypedDict):
    sha: str


class _PullResponse(TypedDict):
    number: int
    title: str
    body: str | None
    user: _User | None
    html_url: str
    head: _Head


class _FileResponse(TypedDict, total=False):
    filename: str
    status: str
    additions: int
    deletions: int
    patch: str


class GitHubClient:
    def __init__(self, token: str | None = None, *, client: httpx.Client | None = None) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "devassist",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._headers = headers

    def pull_request(self, full_name: str, number: int) -> PullRequest:
        pull: _PullResponse = self._get(
            f"/repos/{full_name}/pulls/{number}",
            not_found=f"Pull request #{number} wasn't found in {full_name}.",
        )
        files: list[_FileResponse] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            batch: list[_FileResponse] = self._get(
                f"/repos/{full_name}/pulls/{number}/files",
                params={"per_page": FILES_PER_PAGE, "page": page},
            )
            files.extend(batch)
            if len(batch) < FILES_PER_PAGE:
                break
        return PullRequest(
            number=pull["number"],
            title=pull["title"],
            body=pull["body"] or "",
            author=pull["user"]["login"] if pull["user"] else None,
            html_url=pull["html_url"],
            head_sha=pull["head"]["sha"],
            files=[
                ChangedFile(
                    path=f["filename"],
                    status=f.get("status", "modified"),
                    additions=f.get("additions", 0),
                    deletions=f.get("deletions", 0),
                    patch=f.get("patch"),
                )
                for f in files
            ],
        )

    def file_text(self, full_name: str, ref: str, path: str) -> str | None:
        """A file's text at a commit, or None if it can't be read (missing, binary, huge)."""
        url = f"{GITHUB_RAW}/{full_name}/{ref}/{quote(path)}"
        try:
            response = self._client.get(url, headers={"User-Agent": self._headers["User-Agent"]})
        except httpx.TransportError:
            return None
        if response.status_code != 200 or len(response.content) > MAX_FILE_BYTES:
            return None
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _get(
        self,
        path: str,
        *,
        params: dict[str, int] | None = None,
        not_found: str = "Not found on GitHub.",
    ) -> Any:  # callers annotate the JSON shape they expect
        try:
            response = self._client.get(GITHUB_API + path, params=params, headers=self._headers)
        except httpx.TransportError as exc:
            raise GitHubError("Could not reach GitHub. Check your connection.") from exc
        if response.status_code == 404:
            raise GitHubError(f"{not_found} Only public repositories can be reviewed.")
        if (
            response.status_code in (403, 429)
            and response.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise GitHubError(_rate_limit_message(response))
        if response.status_code == 401:
            raise GitHubError("GitHub rejected GITHUB_TOKEN. Create a new token or remove it.")
        if response.is_error:
            raise GitHubError(f"GitHub returned an error ({response.status_code}).")
        return response.json()


def _rate_limit_message(response: httpx.Response) -> str:
    message = "GitHub's API limit is used up (60 requests an hour without a token)."
    try:
        reset = datetime.fromtimestamp(int(response.headers["x-ratelimit-reset"]), tz=UTC)
        minutes = max(1, round((reset - datetime.now(UTC)).total_seconds() / 60))
        message += f" It resets in about {minutes} min."
    except (KeyError, ValueError):
        pass
    return message + " Adding a free GITHUB_TOKEN to .env raises the limit to 5,000 an hour."
