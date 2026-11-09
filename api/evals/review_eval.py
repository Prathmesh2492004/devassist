"""Measure how well the reviewer finds the planted bugs in review_cases.

Run inside the worker container, where Ollama is reachable:
    docker compose exec worker python -m evals.review_eval [approach ...]

Approaches, all using the production code in app/review:
    rules   Ruff's bug and security rules only (no model)
    open    the open-ended model review only, as used with Claude
    full    rules plus the focused model checks, as used with the local model

A buggy case counts as caught when a bug or risk finding lands within LINE_SLACK lines of the
planted bug. On clean cases, any bug or risk finding is a false alarm.

Results with qwen2.5-coder:3b on a GTX 1650 (12 buggy cases, 3 clean):
    open-ended prompt                    0/12 caught, 0/3 false alarms
    rules                                8/12 caught, 0/3 false alarms, 3 s
    all checks in one call               7/12 caught, 1/3 false alarms
    one call per check                   7/12 caught, 2/3 false alarms
The two check styles flag different false alarms but agree on real bugs, which is why the
reviewer keeps only the problems both of them flag.
"""

import difflib
import sys
import time
from dataclasses import dataclass

from app.config import get_settings
from app.qa.answer import build_answerer
from app.review.diff import added_lines, parse_patch
from app.review.github import PullRequest
from app.review.reviewer import (
    SYSTEM_PROMPT,
    Finding,
    ReviewResult,
    ReviewSettings,
    Strategy,
    review_file,
)
from app.review.static import check_python
from evals.review_cases import CASES, Case

LINE_SLACK = 2
SERIOUS = {"bug", "risk"}


@dataclass(frozen=True)
class Score:
    caught: int
    buggy: int
    false_alarms: int
    clean: int
    seconds: float


def make_patch(before: str, after: str) -> str:
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=3)
    return "\n".join(line for line in diff if not line.startswith(("---", "+++")))


def findings_for(approach: str, case: Case) -> list[Finding]:
    patch = make_patch(case.before, case.after)
    if approach == "rules":
        added = added_lines(parse_patch(patch))
        return [
            Finding(case.path, f.line, f.severity, f"{f.rule} {f.message}", "rule")
            for f in check_python(case.path, case.after, added)
        ]
    strategy: Strategy = "open" if approach == "open" else "checks"
    answerer = build_answerer(get_settings())
    return review_file(
        PullRequest(0, f"Update {case.path}", "", None, "", "", []),
        case.path,
        patch,
        case.after if approach == "full" else None,
        SYSTEM_PROMPT.format(repo="example/app"),
        answerer,
        lambda query: [],
        ReviewSettings(strategy=strategy),
        ReviewResult(summary="", findings=[], files=[], model=answerer.model),
    )


def evaluate(approach: str) -> Score:
    caught = alarms = 0
    started = time.monotonic()
    print(f"\n=== {approach} ===")
    for case in CASES:
        findings = findings_for(approach, case)
        serious = [f for f in findings if f.severity in SERIOUS]
        if case.bug_line is None:
            verdict = "FALSE ALARM" if serious else "ok (clean)"
            alarms += bool(serious)
        else:
            hit = any(abs(f.line - case.bug_line) <= LINE_SLACK for f in serious)
            verdict = "CAUGHT" if hit else "missed"
            caught += hit
        shown = [(f.line, f.source, f.comment[:60]) for f in findings]
        print(f"{case.name:28} {verdict:12} {shown}")
    buggy = sum(c.bug_line is not None for c in CASES)
    score = Score(caught, buggy, alarms, len(CASES) - buggy, time.monotonic() - started)
    print(
        f"-> caught {score.caught}/{score.buggy} bugs, "
        f"{score.false_alarms}/{score.clean} false alarms, {score.seconds:.0f}s"
    )
    return score


if __name__ == "__main__":
    for name in sys.argv[1:] or ["rules", "open", "full"]:
        evaluate(name)
