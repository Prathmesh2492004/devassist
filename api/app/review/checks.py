"""Focused logic checks for small local models, filtered by agreement.

Asked "find any problem in this diff", a 3B model almost always answers "none" (it caught 0
of 12 planted bugs in evals/review_cases.py). It does better with narrow questions, but its
yes/no answers are noisy. So each file gets two independent passes:

1. One call answers every check at once.
2. One call per check, asked on its own.

A problem is reported only when both passes flag the same check within a line of each other.
The two prompt styles make different mistakes, so their agreement filters out most false
alarms while keeping bugs both of them see.
"""

import json
import logging
from dataclasses import dataclass

from app.qa.answer import AnswerDone, Answerer

log = logging.getLogger(__name__)

AGREEMENT_LINES = 1


@dataclass(frozen=True)
class Check:
    question: str
    comment: str  # shown to the reader; written by hand, since the model's reasons are unreliable


CHECKS: dict[str, Check] = {
    "empty_or_none": Check(
        question=(
            "Can a value used in the added lines be None, empty or zero where the code assumes "
            "it is not? For example dividing by len() of a list that may be empty, or reading "
            "an attribute of a lookup result that may be None."
        ),
        comment=(
            "This line may fail when a value is empty, zero or None (for example dividing by "
            "the length of an empty list, or using the result of a lookup that found nothing). "
            "Check that this case is handled."
        ),
    ),
    "out_of_range": Check(
        question="Can an index or a range in the added lines go past the end of a list or string?",
        comment=(
            "An index or range here may go past the end of the list or string, which raises "
            "IndexError. Check the loop bounds."
        ),
    ),
    "wrong_condition": Check(
        question=(
            "Is an if-condition in the added lines inverted or wrong, so the code does the "
            "opposite of what its names and messages say? Check permission checks carefully."
        ),
        comment=(
            "This condition may be inverted or wrong: the code may do the opposite of what its "
            "names and messages say. Check it, especially if it guards a permission."
        ),
    ),
}

ALL_AT_ONCE_SYSTEM = """\
You review one file of a pull request. Answer each check about the added lines (marked +). \
For each check, first reason about the specific added lines in one or two sentences, then \
set problem to true only if you found a concrete case where the code fails. Give the line \
number from the left column of the diff."""

ONE_CHECK_SYSTEM = """\
You check one file of a pull request for one specific kind of bug, in the added lines \
(marked +) only. First reason about the added lines in one or two sentences. Then set \
problem to true only if you can name a concrete input where the added code fails in this \
specific way; anything else, including other kinds of problems, is false. Give the line \
number from the left column of the diff."""

ANSWER_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},  # first, so the model thinks before it decides
        "problem": {"type": "boolean"},
        "line": {"type": "integer"},
    },
    "required": ["reasoning", "problem", "line"],
    "additionalProperties": False,
}

ALL_AT_ONCE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": dict.fromkeys(CHECKS, ANSWER_SCHEMA),
    "required": list(CHECKS),
    "additionalProperties": False,
}


@dataclass(frozen=True)
class LogicFlag:
    line: int
    check: str

    @property
    def comment(self) -> str:
        return CHECKS[self.check].comment


def _flag(answer: object, added: set[int]) -> int | None:
    """The flagged line, if the answer says there is a problem on an added line."""
    if not isinstance(answer, dict) or answer.get("problem") is not True:
        return None
    line = answer.get("line")
    return line if isinstance(line, int) and line in added else None


def _parse(text: str) -> dict[str, object]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Logic check reply was not valid JSON")
        return {}
    return data if isinstance(data, dict) else {}


def logic_checks(
    answerer: Answerer, path: str, diff: str, added: set[int]
) -> tuple[list[LogicFlag], list[AnswerDone]]:
    """Flags both passes agree on, and the token usage of every call."""
    usage: list[AnswerDone] = []
    diff_block = f'<diff path="{path}">\n{diff}\n</diff>'

    questions = "\n".join(f"- {name}: {check.question}" for name, check in CHECKS.items())
    text, done = answerer.complete_json(
        ALL_AT_ONCE_SYSTEM, f"{diff_block}\n\nChecks:\n{questions}", ALL_AT_ONCE_SCHEMA
    )
    usage.append(done)
    answers = _parse(text)
    first_pass = {
        name: line for name in CHECKS if (line := _flag(answers.get(name), added)) is not None
    }

    flags: list[LogicFlag] = []
    # Ask again only about the checks the first pass flagged: the second pass can only
    # confirm a problem, so a clean file costs a single call.
    for name, first_line in first_pass.items():
        text, done = answerer.complete_json(
            ONE_CHECK_SYSTEM, f"{diff_block}\n\nCheck: {CHECKS[name].question}", ANSWER_SCHEMA
        )
        usage.append(done)
        line = _flag(_parse(text), added)
        if line is not None and abs(line - first_line) <= AGREEMENT_LINES:
            flags.append(LogicFlag(line, name))
    return flags, usage
