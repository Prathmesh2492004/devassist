from app.review.publish import FOOTER, comment_for, review_body
from app.review.reviewer import FileReview, Finding, ReviewResult


def _file(path: str, skipped: str | None = None) -> FileReview:
    return FileReview(path, "modified", 1, 0, "@@ -1 +1 @@\n-a\n+b", skipped)


def test_each_comment_says_how_serious_and_who_found_it() -> None:
    rule = comment_for(Finding("a.py", 4, "bug", "Uses eval. (Ruff rule S307)", "rule"))
    model = comment_for(Finding("a.py", 9, "risk", "May divide by zero.", "model"))

    assert (rule.path, rule.line) == ("a.py", 4)
    assert rule.body == "**Bug** · Rule check\n\nUses eval. (Ruff rule S307)"
    assert model.body == "**Risk** · AI check\n\nMay divide by zero."


def test_the_review_text_counts_findings_by_severity() -> None:
    result = ReviewResult(
        summary="Adds fees.",
        findings=[
            Finding("a.py", 1, "bug", "x", "rule"),
            Finding("a.py", 2, "bug", "y", "rule"),
            Finding("b.py", 3, "suggestion", "z", "model"),
        ],
        files=[_file("a.py"), _file("b.py"), _file("README.md", skipped="documentation")],
        model="qwen2.5-coder:3b",
    )

    body = review_body(result, "abcdef1234")

    assert body.startswith("### DevAssist review of `abcdef1`\n\nAdds fees.")
    assert "Found 2 bugs, 1 suggestion in 2 reviewed files." in body
    assert body.endswith(FOOTER)


def test_a_clean_review_says_so() -> None:
    result = ReviewResult("Renames a helper.", [], [_file("a.py")], "m")
    assert "Found no problems in 1 reviewed file." in review_body(result, "abc1234")
