"""Turn a finished review into what's posted on the pull request.

One GitHub review per run, with one comment per finding on its line, so the author sees
each problem next to the code. Every comment says whether a rule or the AI found it: rule
findings are reliable, AI findings are hints worth a second look.
"""

from collections import Counter

from app.review.github import ReviewComment
from app.review.reviewer import Finding, ReviewResult

SEVERITY_LABELS = {"bug": "Bug", "risk": "Risk", "suggestion": "Suggestion"}
SOURCE_LABELS = {"rule": "Rule check", "model": "AI check"}
FOOTER = (
    "<sub>Automated review by DevAssist. Rule checks come from Ruff's bug and security "
    "rules; AI checks are a model's judgement, so verify them before changing code.</sub>"
)


def comment_for(finding: Finding) -> ReviewComment:
    severity = SEVERITY_LABELS.get(finding.severity, finding.severity.title())
    source = SOURCE_LABELS.get(finding.source, finding.source)
    return ReviewComment(
        path=finding.path,
        line=finding.line,
        body=f"**{severity}** · {source}\n\n{finding.comment}",
    )


def review_body(result: ReviewResult, head_sha: str) -> str:
    counts: Counter[str] = Counter(f.severity for f in result.findings)
    if result.findings:
        found = ", ".join(
            f"{counts[s]} {SEVERITY_LABELS[s].lower()}{'s' if counts[s] > 1 else ''}"
            for s in SEVERITY_LABELS
            if counts[s]
        )
    else:
        found = "no problems"
    reviewed = sum(1 for f in result.files if f.skipped is None)
    lines = [
        f"### DevAssist review of `{head_sha[:7]}`",
        "",
        result.summary or "",
        "",
        f"Found {found} in {reviewed} reviewed file{'s' if reviewed != 1 else ''}.",
        "",
        FOOTER,
    ]
    return "\n".join(lines)
