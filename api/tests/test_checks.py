import json

from app.review.checks import ALL_AT_ONCE_SCHEMA, ANSWER_SCHEMA, CHECKS, LogicFlag, logic_checks
from tests.fakes import FakeAnswerer

ADDED = {4, 5, 6}


def _answer(problem: bool, line: int = 5) -> dict[str, object]:
    return {"reasoning": "Because.", "problem": problem, "line": line}


def _first_pass(**flagged: int) -> str:
    return json.dumps({name: _answer(name in flagged, flagged.get(name, 5)) for name in CHECKS})


def test_reports_a_problem_both_passes_agree_on() -> None:
    answerer = FakeAnswerer()
    answerer.json_replies = [_first_pass(empty_or_none=5), json.dumps(_answer(True, 6))]

    flags, usage = logic_checks(answerer, "a.py", "diff", ADDED)

    assert flags == [LogicFlag(6, "empty_or_none")]
    assert flags[0].comment == CHECKS["empty_or_none"].comment
    assert len(usage) == 2
    second_prompt = answerer.json_calls[1][1]
    assert CHECKS["empty_or_none"].question in second_prompt


def test_drops_a_problem_the_second_pass_does_not_confirm() -> None:
    answerer = FakeAnswerer()
    answerer.json_replies = [
        _first_pass(empty_or_none=5, out_of_range=4),
        json.dumps(_answer(False)),  # empty_or_none: not confirmed
        json.dumps(_answer(True, 6)),  # out_of_range: too far from line 4
    ]

    flags, _ = logic_checks(answerer, "a.py", "diff", ADDED)

    assert flags == []


def test_a_clean_file_costs_one_call() -> None:
    answerer = FakeAnswerer()
    answerer.json_replies = [_first_pass()]

    flags, usage = logic_checks(answerer, "a.py", "diff", ADDED)

    assert (flags, len(usage)) == ([], 1)


def test_ignores_lines_the_pr_did_not_add_and_bad_replies() -> None:
    answerer = FakeAnswerer()
    answerer.json_replies = [_first_pass(empty_or_none=1), "not json"]

    flags, usage = logic_checks(answerer, "a.py", "diff", ADDED)

    assert (flags, len(usage)) == ([], 1)  # line 1 is context, so nothing to confirm


def test_schemas_ask_for_reasoning_before_the_verdict() -> None:
    assert list(ANSWER_SCHEMA["properties"]) == ["reasoning", "problem", "line"]  # type: ignore[call-overload]
    assert ALL_AT_ONCE_SCHEMA["required"] == list(CHECKS)
