import json

import pytest

from app.models import Chunk
from app.qa.search import SearchHit
from app.review.github import ChangedFile, PullRequest
from app.review.reviewer import (
    FILE_SCHEMA,
    Finding,
    ReviewSettings,
    parse_findings,
    review_pull_request,
    skip_reason,
)
from tests.fakes import FakeAnswerer

PATCH = (
    "@@ -1,2 +1,3 @@\n def charge(card):\n-    return 1\n"
    "+    fee = card.total * 0.03\n+    return fee"
)


def _file(path: str, patch: str | None = PATCH, status: str = "modified") -> ChangedFile:
    return ChangedFile(path=path, status=status, additions=2, deletions=1, patch=patch)


def _pr(*files: ChangedFile) -> PullRequest:
    return PullRequest(
        number=7,
        title="Add card fees",
        body="Charges a 3% fee.",
        author="octocat",
        html_url="https://github.com/octocat/shop/pull/7",
        head_sha="d" * 40,
        files=list(files),
    )


def _findings(*items: dict[str, object]) -> str:
    return json.dumps({"findings": list(items)})


@pytest.mark.parametrize(
    ("file", "reason"),
    [
        (_file("src/billing.py"), None),
        (_file("src/old.py", status="removed"), "deleted"),
        (_file("logo.png", patch=None), "binary or too large to show"),
        (_file("package-lock.json"), "documentation or data"),
        (_file("README.md"), "documentation or data"),
        (_file("LICENSE"), "not a code file"),
    ],
)
def test_only_code_files_with_a_diff_are_reviewed(file: ChangedFile, reason: str | None) -> None:
    assert skip_reason(file) == reason


def test_keeps_only_findings_on_lines_the_diff_shows() -> None:
    text = _findings(
        {"line": 2, "severity": "bug", "comment": "Fee is charged twice."},
        {"line": 99, "severity": "bug", "comment": "Not in the diff."},
        {"line": 3, "severity": "nitpick", "comment": "Unknown severity."},
        {"line": 3, "severity": "risk", "comment": "  "},
        {"line": 2, "severity": "bug", "comment": "fee is charged twice."},  # duplicate
        {"line": 3, "severity": "risk", "comment": "Float money math."},
    )

    assert parse_findings(text, "a.py", {1, 2, 3}, limit=5) == [
        Finding("a.py", 2, "bug", "Fee is charged twice."),
        Finding("a.py", 3, "risk", "Float money math."),
    ]


def test_caps_findings_per_file() -> None:
    text = _findings(*({"line": 1, "severity": "risk", "comment": f"c{i}"} for i in range(5)))
    assert len(parse_findings(text, "a.py", {1}, limit=3)) == 3


@pytest.mark.parametrize("text", ["not json", "[]", '{"findings": "none"}', '{"findings": [1]}'])
def test_malformed_replies_give_no_findings(text: str) -> None:
    assert parse_findings(text, "a.py", {1}, limit=3) == []


def test_reviews_each_code_file_with_related_code_then_summarizes() -> None:
    answerer = FakeAnswerer()
    answerer.json_replies = [
        _findings({"line": 2, "severity": "bug", "comment": "Rounds money with floats."}),
        json.dumps({"summary": "Adds a card fee; one bug to fix."}),
    ]
    related = Chunk(path="src/cart.py", symbol="checkout", start_line=8, end_line=8,
                    content="total = charge(card)")  # fmt: skip
    queries: list[str] = []
    progress: list[tuple[int, int]] = []

    def related_code(query: str) -> list[SearchHit]:
        queries.append(query)
        return [SearchHit(chunk=related, score=0.1, vector_rank=1, keyword_rank=None)]

    result = review_pull_request(
        _pr(_file("src/billing.py"), _file("README.md")),
        "octocat/shop",
        answerer=answerer,
        related_code=related_code,
        settings=ReviewSettings(),
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert result.summary == "Adds a card fee; one bug to fix."
    assert result.findings == [Finding("src/billing.py", 2, "bug", "Rounds money with floats.")]
    assert [(f.path, f.skipped) for f in result.files] == [
        ("src/billing.py", None),
        ("README.md", "documentation or data"),
    ]
    assert (result.model, result.input_tokens, result.output_tokens) == ("fake-model", 1000, 100)
    assert progress == [(0, 1), (1, 1)]

    # the search looks for code like the added lines
    assert queries[0].startswith("src/billing.py\n    fee = card.total * 0.03")
    system, prompt = answerer.json_calls[0]
    assert "octocat/shop" in system
    assert '<excerpt path="src/cart.py" lines="8-8"' in prompt
    assert '<diff path="src/billing.py">' in prompt
    assert "2 +     fee = card.total * 0.03" in prompt
    assert "[bug] src/billing.py:2: Rounds money with floats." in answerer.json_calls[1][1]


def test_stops_at_the_file_limit() -> None:
    answerer = FakeAnswerer()
    files = [_file(f"src/f{i}.py") for i in range(3)]

    result = review_pull_request(
        _pr(*files),
        "octocat/shop",
        answerer=answerer,
        related_code=lambda query: [],
        settings=ReviewSettings(max_files=2),
    )

    assert [f.skipped for f in result.files] == [None, None, "over the 2-file limit"]
    assert len(answerer.json_calls) == 3  # two files and the summary


def test_no_model_call_when_nothing_is_reviewable() -> None:
    answerer = FakeAnswerer()

    result = review_pull_request(
        _pr(_file("docs/guide.md")),
        "octocat/shop",
        answerer=answerer,
        related_code=lambda query: [],
        settings=ReviewSettings(),
    )

    assert result.summary == "No code files to review in this pull request."
    assert answerer.json_calls == []


def test_schema_forbids_extra_fields() -> None:
    assert FILE_SCHEMA["additionalProperties"] is False
