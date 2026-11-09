from app.review.diff import (
    DiffLine,
    added_lines,
    commentable_lines,
    format_for_review,
    parse_patch,
)

PATCH = """@@ -10,4 +10,5 @@ def charge(card):
     total = card.total
-    fee = 0
+    fee = total * 0.03
+    log(fee)
     return total + fee
\\ No newline at end of file
@@ -40,2 +41,2 @@ class Refund:
-    pass
+    raise NotImplementedError
     # end"""


def test_numbers_lines_on_both_sides_of_the_change() -> None:
    lines = parse_patch(PATCH)

    assert lines[1] == DiffLine("context", "    total = card.total", old_line=10, new_line=10)
    assert lines[2] == DiffLine("remove", "    fee = 0", old_line=11)
    assert lines[3] == DiffLine("add", "    fee = total * 0.03", new_line=11)
    assert lines[4] == DiffLine("add", "    log(fee)", new_line=12)
    assert lines[5] == DiffLine("context", "    return total + fee", old_line=12, new_line=13)
    # the no-newline marker is not a line; the second hunk restarts the counters
    assert lines[6].kind == "hunk"
    assert lines[8] == DiffLine("add", "    raise NotImplementedError", new_line=41)


def test_commentable_lines_are_added_and_context_lines() -> None:
    lines = parse_patch(PATCH)

    assert added_lines(lines) == {11, 12, 41}
    assert commentable_lines(lines) == {10, 11, 12, 13, 41, 42}


def test_review_format_shows_new_line_numbers_and_markers() -> None:
    text = format_for_review(parse_patch(PATCH))

    assert "11 +     fee = total * 0.03" in text
    assert "   -     fee = 0" in text
    assert "13       return total + fee" in text
    assert text.startswith("@@ -10,4 +10,5 @@")
