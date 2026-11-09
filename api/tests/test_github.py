import httpx
import pytest

from app.review.github import FILES_PER_PAGE, GitHubClient, GitHubError

PULL = {
    "number": 7,
    "title": "Add fees",
    "body": None,
    "user": {"login": "octocat"},
    "html_url": "https://github.com/octocat/shop/pull/7",
    "head": {"sha": "d" * 40},
}


def _file(n: int) -> dict[str, object]:
    return {"filename": f"src/f{n}.py", "status": "modified", "additions": 1, "deletions": 0,
            "patch": PATCH}  # fmt: skip


PATCH = "@@ -1 +1 @@\n-a\n+b"


def test_reads_the_pull_request_and_pages_through_its_files() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/files"):
            page = int(request.url.params["page"])
            count = FILES_PER_PAGE if page == 1 else 2
            return httpx.Response(200, json=[_file(page * 1000 + i) for i in range(count)])
        return httpx.Response(200, json=PULL)

    client = GitHubClient("tok", client=httpx.Client(transport=httpx.MockTransport(handler)))
    pr = client.pull_request("octocat/shop", 7)

    assert (pr.number, pr.title, pr.body, pr.author) == (7, "Add fees", "", "octocat")
    assert pr.head_sha == "d" * 40
    assert len(pr.files) == FILES_PER_PAGE + 2
    assert pr.files[0].patch == PATCH
    assert requests[0].url.path == "/repos/octocat/shop/pulls/7"
    assert requests[0].headers["authorization"] == "Bearer tok"


def test_anonymous_requests_send_no_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[] if request.url.path.endswith("/files") else PULL)

    GitHubClient(client=httpx.Client(transport=httpx.MockTransport(handler))).pull_request(
        "octocat/shop", 7
    )
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(404), "#7 wasn't found in octocat/shop. Only public"),
        (
            httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}),
            "API limit is used up.*GITHUB_TOKEN",
        ),
        (httpx.Response(401), "rejected GITHUB_TOKEN"),
        (httpx.Response(502), r"error \(502\)"),
    ],
)
def test_failures_become_readable_errors(response: httpx.Response, expected: str) -> None:
    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(lambda r: response)))
    with pytest.raises(GitHubError, match=expected):
        client.pull_request("octocat/shop", 7)


def test_network_failure_becomes_a_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(GitHubError, match="Could not reach GitHub"):
        client.pull_request("octocat/shop", 7)


def test_reads_a_file_at_a_commit_from_raw_github() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="print('hi')\n")

    client = GitHubClient("tok", client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert client.file_text("octocat/shop", "d" * 40, "src/my app.py") == "print('hi')\n"
    expected = f"https://raw.githubusercontent.com/octocat/shop/{'d' * 40}/src/my%20app.py"
    assert str(seen[0].url) == expected


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, content=b"\xff\xfe\x00binary"),
        httpx.Response(200, content=b"x" * 600_000),
    ],
)
def test_unreadable_files_give_none(response: httpx.Response) -> None:
    client = GitHubClient(client=httpx.Client(transport=httpx.MockTransport(lambda r: response)))
    assert client.file_text("octocat/shop", "abc", "a.py") is None
