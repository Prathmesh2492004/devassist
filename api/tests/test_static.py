"""Rule-based checks. These run the real Ruff binary, which takes milliseconds."""

import pytest

from app.review.static import RULES, StaticFinding, check_python, is_test_file, severity_for

SOURCE = """\
import sqlite3


def find(db: sqlite3.Connection, name: str):
    query = f"SELECT * FROM users WHERE name = '{name}'"
    return db.execute(query).fetchone()


def old(items=[]):
    return items
"""


def test_reports_problems_on_added_lines() -> None:
    findings = check_python("app/users.py", SOURCE, added={5, 6})

    message = "Possible SQL injection vector through string-based query construction"
    assert findings == [StaticFinding(5, "bug", "S608", message)]


def test_ignores_problems_in_lines_the_pr_did_not_add() -> None:
    # line 9 has a mutable default argument, but it was already there
    assert check_python("app/users.py", SOURCE, added={5, 6}) == check_python(
        "app/users.py", SOURCE, added={5, 6, 2}
    )
    assert [f.rule for f in check_python("app/users.py", SOURCE, added={9})] == ["B006"]


def test_skips_style_rules() -> None:
    messy = "import os\nx=1\n"  # unused import and missing spaces are style, not bugs
    assert check_python("a.py", messy, added={1, 2}) == []


def test_only_checks_python_files() -> None:
    assert check_python("app/users.js", "eval(input)", added={1}) == []


def test_a_file_that_does_not_parse_gives_no_findings() -> None:
    assert check_python("a.py", "def broken(:\n", added={1}) == []


def test_rule_groups_match_by_prefix() -> None:
    assert severity_for("S608") == "bug"
    assert severity_for("PLE1142") == RULES["PLE"]
    assert severity_for("E501") is None


def test_handles_text_outside_ascii() -> None:
    source = 'GREETING = "héllo 👋"\nrows = eval(input())\n'
    assert [f.rule for f in check_python("a.py", source, added={1, 2})] == ["S307"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_app.py", True),
        ("src/flask/testing.py", False),
        ("pkg/test/helpers.py", True),
        ("app/conftest.py", True),
        ("app/user_test.py", True),
        ("app/contest.py", False),
    ],
)
def test_recognizes_test_files(path: str, expected: bool) -> None:
    assert is_test_file(path) is expected


def test_security_rules_are_skipped_in_tests_but_bug_rules_are_not() -> None:
    source = 'SECRET_KEY = "test-secret"\n\n\ndef make(items=[]):\n    return items\n'
    findings = check_python("tests/test_app.py", source, added={1, 4})
    assert [f.rule for f in findings] == ["B006"]
    assert [f.rule for f in check_python("app/config.py", source, added={1, 4})] == [
        "S105",
        "B006",
    ]
