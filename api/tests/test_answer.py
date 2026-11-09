"""Answer generation. The Claude client runs for real, against a mocked HTTP transport."""

import json
from collections.abc import Callable

import anthropic
import httpx
import httpx2
import pytest

from app.config import Settings
from app.models import Chunk
from app.qa.answer import (
    CITATION_EXAMPLE,
    CITATION_REMINDER,
    FALLBACK_BETA,
    AnswerDelta,
    AnswerDone,
    AnswerError,
    ClaudeAnswerer,
    MissingAnthropicKeyError,
    OllamaAnswerer,
    Turn,
    build_answerer,
    build_messages,
    format_excerpt,
    select_sources,
)
from app.qa.search import SearchHit


def _hit(content: str, start_line: int, symbol: str | None = "charge") -> SearchHit:
    chunk = Chunk(
        path="src/billing.py",
        symbol=symbol,
        start_line=start_line,
        end_line=start_line + content.count("\n"),
        content=content,
    )
    return SearchHit(chunk=chunk, score=0.03, vector_rank=1, keyword_rank=None)


def test_excerpt_numbers_every_line_with_its_real_line_number() -> None:
    excerpt = format_excerpt(_hit("def charge(card):\n    return 1", start_line=9))

    assert excerpt == (
        '<excerpt path="src/billing.py" lines="9-10" symbol="charge">\n'
        " 9 | def charge(card):\n"
        "10 |     return 1\n"
        "</excerpt>"
    )


def test_excerpt_omits_symbol_when_there_is_none() -> None:
    assert 'symbol="' not in format_excerpt(_hit("x = 1", start_line=1, symbol=None))


def test_only_the_new_question_carries_code() -> None:
    history = [Turn("What does charge do?", "It charges a card [src/billing.py:9-10].")]

    messages = build_messages("And refunds?", [_hit("def refund(): ...", 20)], history)

    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "What does charge do?"
    last = messages[2]["content"]
    assert isinstance(last, str)
    assert last.startswith("<excerpts>\n<excerpt ")
    assert last.endswith(f"Question: And refunds?\n\n{CITATION_REMINDER}")


def test_says_so_when_search_found_nothing() -> None:
    content = build_messages("Anything?", [], [])[0]["content"]
    assert isinstance(content, str) and "(no matching code found)" in content


def _sse(*events: dict[str, object]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def _claude_stream(texts: list[str], stop_reason: str = "end_turn") -> bytes:
    message: dict[str, object] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 1, "cache_read_input_tokens": 300},
    }
    return _sse(
        {"type": "message_start", "message": message},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        *(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}}
            for t in texts
        ),
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 42},
        },
        {"type": "message_stop"},
    )


def _answerer(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> ClaudeAnswerer:
    client = anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return ClaudeAnswerer(client, "claude-sonnet-5-5", "low")


def test_streams_text_then_reports_usage() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        body = _claude_stream(["Charges ", "happen in [src/billing.py:9-10]."])
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    events = list(_answerer(handler).stream("system prompt", [{"role": "user", "content": "q"}]))

    assert events == [
        AnswerDelta("Charges "),
        AnswerDelta("happen in [src/billing.py:9-10]."),
        AnswerDone(model="claude-sonnet-5-5", input_tokens=1500, output_tokens=42),
    ]
    sent = json.loads(requests[0].content)
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["stream"] is True
    assert sent["system"] == "system prompt"
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "low"}
    assert sent["fallbacks"] == "default"
    assert FALLBACK_BETA in requests[0].headers["anthropic-beta"]


def test_refusal_becomes_a_readable_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = _claude_stream([], stop_reason="refusal")
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    with pytest.raises(AnswerError, match="declined"):
        list(_answerer(handler).stream("s", [{"role": "user", "content": "q"}]))


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (401, "invalid x-api-key", "Check ANTHROPIC_API_KEY"),
        (400, "Your credit balance is too low to access the Anthropic API.", "no credits"),
        (429, "rate limited", "rate limit"),
        (529, "overloaded", "error \\(529\\)"),
    ],
)
def test_api_errors_become_readable_errors(status: int, message: str, expected: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        error = {"type": "error", "error": {"type": "api_error", "message": message}}
        return httpx2.Response(status, json=error)

    with pytest.raises(AnswerError, match=expected):
        list(_answerer(handler).stream("s", [{"role": "user", "content": "q"}]))


def test_missing_key_is_reported_before_any_request() -> None:
    with pytest.raises(MissingAnthropicKeyError, match="ANTHROPIC_API_KEY"):
        build_answerer(Settings(answer_provider="anthropic", anthropic_api_key=None))


def test_builds_the_configured_claude_model() -> None:
    answerer = build_answerer(
        Settings(
            answer_provider="anthropic",
            anthropic_api_key="sk-test",
            claude_answer_model="claude-haiku-5-5",
        )
    )
    assert isinstance(answerer, ClaudeAnswerer)
    assert answerer.model == "claude-haiku-5-5"


def test_local_model_is_the_default_and_needs_no_key() -> None:
    answerer = build_answerer(Settings(anthropic_api_key=None))

    assert isinstance(answerer, OllamaAnswerer)
    assert answerer.model == "qwen2.5-coder:3b"


def _ollama(handler: Callable[[httpx.Request], httpx.Response]) -> OllamaAnswerer:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaAnswerer("http://ollama:11434", "qwen2.5-coder:3b", 8192, client=client)


def _ndjson(*lines: dict[str, object]) -> bytes:
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def test_ollama_streams_text_then_reports_usage() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_ndjson(
                {"message": {"role": "assistant", "content": "Charged in "}, "done": False},
                {"message": {"role": "assistant", "content": "[a.py:1-2]."}, "done": False},
                {"done": True, "prompt_eval_count": 900, "eval_count": 7},
            ),
        )

    events = list(_ollama(handler).stream("system prompt", [{"role": "user", "content": "q"}]))

    assert events == [
        AnswerDelta("Charged in "),
        AnswerDelta("[a.py:1-2]."),
        AnswerDone(model="qwen2.5-coder:3b", input_tokens=900, output_tokens=7),
    ]
    assert requests[0].url == "http://ollama:11434/api/chat"
    sent = json.loads(requests[0].content)
    assert sent["messages"] == [
        {"role": "system", "content": "system prompt"},
        *CITATION_EXAMPLE,  # one worked example teaches the small model to cite
        {"role": "user", "content": "q"},
    ]
    assert sent["options"]["num_ctx"] == 8192  # so the excerpts aren't silently cut off


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(404, json={"error": "model not found"}), "ollama pull qwen2.5-coder:3b"),
        (httpx.Response(500, json={"error": "out of memory"}), "out of memory"),
        (httpx.Response(200, content=_ndjson({"error": "model crashed"})), "model crashed"),
        (httpx.Response(200, content=_ndjson({"message": {"content": "Hal"}})), "stopped"),
    ],
)
def test_ollama_failures_become_readable_errors(response: httpx.Response, expected: str) -> None:
    with pytest.raises(AnswerError, match=expected):
        list(_ollama(lambda request: response).stream("s", [{"role": "user", "content": "q"}]))


def test_ollama_unreachable_becomes_a_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(AnswerError, match="Could not reach Ollama"):
        list(_ollama(handler).stream("s", [{"role": "user", "content": "q"}]))


def test_select_sources_keeps_best_hits_within_the_character_budget() -> None:
    hits = [_hit("a" * 60, 1), _hit("b" * 30, 10), _hit("c" * 30, 20), _hit("d", 30)]

    assert select_sources(hits, max_chars=100) == hits[:2]
    assert select_sources(hits, max_chars=10) == hits[:1]  # the top hit is always kept


SCHEMA: dict[str, object] = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def test_ollama_json_reply_is_constrained_to_the_schema() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_ndjson(
                {"message": {"content": '{"ok": '}, "done": False},
                {"message": {"content": "true}"}, "done": False},
                {"done": True, "prompt_eval_count": 50, "eval_count": 4},
            ),
        )

    text, done = _ollama(handler).complete_json("sys", "prompt", SCHEMA)

    assert text == '{"ok": true}'
    assert done == AnswerDone(model="qwen2.5-coder:3b", input_tokens=50, output_tokens=4)
    sent = json.loads(requests[0].content)
    assert sent["format"] == SCHEMA
    assert sent["options"]["temperature"] == 0.0
    # no citation example: reviews are not answers
    assert sent["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "prompt"},
    ]


def test_claude_json_reply_uses_structured_output() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        body = _claude_stream(['{"ok": true}'])
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    text, done = _answerer(handler).complete_json("sys", "prompt", SCHEMA)

    assert text == '{"ok": true}'
    assert done.output_tokens == 42
    sent = json.loads(requests[0].content)
    assert sent["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert sent["messages"] == [{"role": "user", "content": "prompt"}]
