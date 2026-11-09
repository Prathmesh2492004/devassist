"""Review a pull request file by file, with related code from the index as context.

Each changed code file gets its own model call, so even a small local model with an 8K
window sees one whole diff at a time. The model must return JSON matching a schema, and
every finding is checked against the diff: a comment on a line the diff doesn't show is
dropped, because GitHub would reject it and it is usually a hallucination.
"""

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, TypedDict, cast

from app.ingest.files import detect_language
from app.qa.answer import Answerer, format_excerpt
from app.qa.search import SearchHit
from app.review.diff import commentable_lines, format_for_review, parse_patch
from app.review.github import ChangedFile, PullRequest

log = logging.getLogger(__name__)

Severity = Literal["bug", "risk", "suggestion"]
SEVERITIES: tuple[Severity, ...] = ("bug", "risk", "suggestion")

# Prose and data files: reviewing them costs a model call and rarely finds a real problem.
SKIPPED_LANGUAGES = {"markdown", "json"}
BODY_CHARS = 1_500
QUERY_CHARS = 1_500
COMMENT_CHARS = 1_000

SYSTEM_PROMPT = """\
You are a senior engineer reviewing one file of a pull request to {repo}.

Report only real problems in the changed lines: bugs, crashes, wrong logic, security holes, \
data loss, race conditions, resource leaks, and missing error handling that will bite. Do \
not comment on style, naming, formatting or missing comments. Do not praise the code. Most \
files have no problems; then return an empty findings list.

Each finding has:
- line: the line number shown in the left column of the diff, on a line marked + where possible
- severity: "bug" (wrong behaviour), "risk" (could fail in some cases) or "suggestion"
- comment: one to three sentences saying what is wrong and how to fix it

The related code comes from the repository before this change. Use it to understand how the \
changed code is called; do not review it."""

FILE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "comment": {"type": "string"},
                },
                "required": ["line", "severity", "comment"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

SUMMARY_SYSTEM_PROMPT = """\
You write the summary at the top of a pull request review. In two to four sentences, say \
what the pull request changes and whether the review found problems that should be fixed \
before merging. Be specific and plain; no greetings or praise."""

SUMMARY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

# Finds indexed code related to a query, e.g. hybrid search over the repo's chunks.
RelatedCode = Callable[[str], Sequence[SearchHit]]
Progress = Callable[[int, int], None]  # (files reviewed, files to review)


@dataclass(frozen=True)
class ReviewSettings:
    max_files: int = 15
    max_findings_per_file: int = 3
    diff_chars: int = 10_000
    context_chars: int = 6_000
    context_chunks: int = 3


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    severity: Severity
    comment: str


@dataclass(frozen=True)
class FileReview:
    path: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    skipped: str | None = None  # why the file wasn't reviewed


@dataclass
class ReviewResult:
    summary: str
    findings: list[Finding]
    files: list[FileReview]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class _RawFinding(TypedDict):
    line: int
    severity: str
    comment: str


def skip_reason(file: ChangedFile) -> str | None:
    if file.status == "removed":
        return "deleted"
    if file.patch is None:
        return "binary or too large to show"
    language = detect_language(PurePosixPath(file.path))
    if language is None:
        return "not a code file"
    if language in SKIPPED_LANGUAGES:
        return "documentation or data"
    return None


def review_pull_request(
    pr: PullRequest,
    repo: str,
    *,
    answerer: Answerer,
    related_code: RelatedCode,
    settings: ReviewSettings,
    on_progress: Progress | None = None,
) -> ReviewResult:
    result = ReviewResult(summary="", findings=[], files=[], model=answerer.model)
    to_review: list[ChangedFile] = []
    for file in pr.files:
        reason = skip_reason(file)
        if reason is None and len(to_review) >= settings.max_files:
            reason = f"over the {settings.max_files}-file limit"
        result.files.append(_file_review(file, reason))
        if reason is None:
            to_review.append(file)

    system = SYSTEM_PROMPT.format(repo=repo)
    for done, file in enumerate(to_review):
        if on_progress:
            on_progress(done, len(to_review))
        findings = _review_file(pr, file, system, answerer, related_code, settings, result)
        result.findings.extend(findings)
    if on_progress:
        on_progress(len(to_review), len(to_review))

    result.summary = _summarize(pr, result, answerer)
    return result


def _file_review(file: ChangedFile, skipped: str | None) -> FileReview:
    return FileReview(
        path=file.path,
        status=file.status,
        additions=file.additions,
        deletions=file.deletions,
        patch=file.patch,
        skipped=skipped,
    )


def _review_file(
    pr: PullRequest,
    file: ChangedFile,
    system: str,
    answerer: Answerer,
    related_code: RelatedCode,
    settings: ReviewSettings,
    result: ReviewResult,
) -> list[Finding]:
    assert file.patch is not None  # skip_reason filtered these out
    lines = parse_patch(file.patch)
    diff = format_for_review(lines)
    if len(diff) > settings.diff_chars:
        diff = diff[: settings.diff_chars] + "\n... (rest of the diff cut off)"

    added = "\n".join(line.text for line in lines if line.kind == "add")
    hits = related_code(f"{file.path}\n{added}"[:QUERY_CHARS])
    context = _context(hits, settings)

    prompt = _file_prompt(pr, file.path, diff, context)
    text, usage = answerer.complete_json(system, prompt, FILE_SCHEMA)
    _add_usage(result, usage.input_tokens, usage.output_tokens)
    return parse_findings(text, file.path, commentable_lines(lines), settings.max_findings_per_file)


def _context(hits: Sequence[SearchHit], settings: ReviewSettings) -> str:
    excerpts: list[str] = []
    total = 0
    for hit in hits[: settings.context_chunks]:
        excerpt = format_excerpt(hit)
        if total + len(excerpt) > settings.context_chars:
            break
        excerpts.append(excerpt)
        total += len(excerpt)
    return "\n\n".join(excerpts)


def _file_prompt(pr: PullRequest, path: str, diff: str, context: str) -> str:
    body = pr.body.strip()
    if len(body) > BODY_CHARS:
        body = body[:BODY_CHARS] + " ..."
    parts = [f"Pull request #{pr.number}: {pr.title}"]
    if body:
        parts.append(f"<description>\n{body}\n</description>")
    if context:
        parts.append(f"<related_code>\n{context}\n</related_code>")
    parts.append(f'<diff path="{path}">\n{diff}\n</diff>')
    parts.append(
        f"Review the changes to {path}. Use line numbers from the left column of the diff. "
        "Return an empty findings list if the changes look correct."
    )
    return "\n\n".join(parts)


def parse_findings(text: str, path: str, valid_lines: set[int], limit: int) -> list[Finding]:
    """Keep findings that are well formed and point at a line the diff shows."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Review of %s was not valid JSON", path)
        return []
    raw = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []

    findings: list[Finding] = []
    seen: set[tuple[int, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry = cast(_RawFinding, item)
        line, severity, comment = entry.get("line"), entry.get("severity"), entry.get("comment")
        if not isinstance(line, int) or line not in valid_lines:
            continue
        if severity not in SEVERITIES or not isinstance(comment, str) or not comment.strip():
            continue
        comment = comment.strip()[:COMMENT_CHARS]
        key = (line, comment.lower())
        if key in seen:
            continue
        seen.add(key)
        findings.append(Finding(path, line, severity, comment))
        if len(findings) >= limit:
            break
    return findings


def _summarize(pr: PullRequest, result: ReviewResult, answerer: Answerer) -> str:
    reviewed = [f for f in result.files if f.skipped is None]
    if not reviewed:
        return "No code files to review in this pull request."

    files = "\n".join(f"- {f.path} (+{f.additions} -{f.deletions})" for f in result.files)
    if result.findings:
        found = "\n".join(
            f"- [{f.severity}] {f.path}:{f.line}: {f.comment}" for f in result.findings
        )
    else:
        found = "(none)"
    prompt = (
        f"Pull request #{pr.number}: {pr.title}\n\n"
        f"<description>\n{pr.body.strip()[:BODY_CHARS]}\n</description>\n\n"
        f"Changed files:\n{files}\n\nReview findings:\n{found}"
    )
    text, usage = answerer.complete_json(SUMMARY_SYSTEM_PROMPT, prompt, SUMMARY_SCHEMA)
    _add_usage(result, usage.input_tokens, usage.output_tokens)
    try:
        summary = json.loads(text).get("summary")
    except (json.JSONDecodeError, AttributeError):
        summary = None
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    count = len(result.findings)
    return f"Reviewed {len(reviewed)} files and found {count} possible problems."


def _add_usage(result: ReviewResult, input_tokens: int, output_tokens: int) -> None:
    result.input_tokens += input_tokens
    result.output_tokens += output_tokens
