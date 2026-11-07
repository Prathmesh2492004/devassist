"""Write an answer from retrieved code, with file:line citations, streamed as it is generated.

Two providers share one interface:
- Ollama (default): an open-source model running locally. Free and private, but less accurate.
- Claude: better answers and more reliable citations, but needs an API key with credits.
"""

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol, TypedDict

import anthropic
import httpx
from anthropic.types.beta import BetaMessageParam

from app.config import Effort, Settings
from app.qa.search import SearchHit

# Opt in to server-side fallback: if a safety classifier declines the request, the API retries
# it on the model Anthropic recommends for that kind of decline instead of returning nothing.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16_000

SYSTEM_PROMPT = """\
You answer questions about the source code of the GitHub repository {repo}. Each question \
comes with excerpts from the repository, found by a search engine. Answer from those excerpts.

Citations:
- Back every claim about the code with a citation in the form [path:start-end], using the \
path and line numbers shown on the excerpt, for example [src/app/auth.py:42-58]. Narrow the \
range to the lines that support the claim; stay within the excerpt's lines.
- Cite only excerpts you were given. Never invent paths or line numbers.

If the excerpts don't contain the answer, say what you found and what is missing instead of \
guessing. You may add general knowledge about a language or library, but say that it doesn't \
come from this repository.

Format: open with the direct answer in one or two sentences, then the details. Use Markdown: \
short paragraphs, bullet lists, and fenced code blocks with a language tag. Keep quoted code \
short, since the reader can open each citation. Aim for under 300 words unless the question \
needs more."""


# Repeated right after the question, where small local models pay the most attention.
CITATION_REMINDER = (
    "Cite the excerpt lines behind each claim as [path:start-end], "
    "for example [src/app/auth.py:42-58]."
)


@dataclass(frozen=True)
class AnswerDelta:
    text: str


@dataclass(frozen=True)
class AnswerDone:
    model: str
    input_tokens: int
    output_tokens: int


class AnswerError(Exception):
    """A failure whose message is safe and useful to show the user."""


class Answerer(Protocol):
    model: str

    def stream(
        self, system: str, messages: Sequence[BetaMessageParam]
    ) -> Iterator[AnswerDelta | AnswerDone]: ...


@dataclass(frozen=True)
class Turn:
    question: str
    answer: str


def format_excerpt(hit: SearchHit) -> str:
    chunk = hit.chunk
    attributes = f'path="{chunk.path}" lines="{chunk.start_line}-{chunk.end_line}"'
    if chunk.symbol:
        attributes += f' symbol="{chunk.symbol}"'
    # Number every line so the model can cite exact lines instead of the whole chunk.
    width = len(str(chunk.end_line))
    numbered = "\n".join(
        f"{number:>{width}} | {line}"
        for number, line in enumerate(chunk.content.split("\n"), start=chunk.start_line)
    )
    return f"<excerpt {attributes}>\n{numbered}\n</excerpt>"


def build_messages(
    question: str, hits: Sequence[SearchHit], history: Sequence[Turn]
) -> list[BetaMessageParam]:
    """Earlier turns go in as plain question/answer pairs; only the new question carries code."""
    messages: list[BetaMessageParam] = []
    for turn in history:
        messages.append({"role": "user", "content": turn.question})
        messages.append({"role": "assistant", "content": turn.answer})
    excerpts = "\n\n".join(format_excerpt(hit) for hit in hits) or "(no matching code found)"
    messages.append(
        {
            "role": "user",
            "content": (
                f"<excerpts>\n{excerpts}\n</excerpts>\n\n"
                f"Question: {question}\n\n{CITATION_REMINDER}"
            ),
        }
    )
    return messages


class ClaudeAnswerer:
    def __init__(self, client: anthropic.Anthropic, model: str, effort: Effort) -> None:
        self.model = model
        self._client = client
        self._effort = effort

    def stream(
        self, system: str, messages: Sequence[BetaMessageParam]
    ) -> Iterator[AnswerDelta | AnswerDone]:
        try:
            with self._client.beta.messages.stream(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                for event in stream:
                    if event.type == "text":
                        yield AnswerDelta(event.text)
                final = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise AnswerError("Claude rejected the API key. Check ANTHROPIC_API_KEY.") from exc
        except anthropic.RateLimitError as exc:
            raise AnswerError("Claude API rate limit reached. Try again in a minute.") from exc
        except anthropic.APIStatusError as exc:
            if "credit balance" in str(exc.message).lower():
                raise AnswerError(
                    "Your Anthropic account has no credits left. Add credits in the Claude "
                    "Console, then ask again."
                ) from exc
            raise AnswerError(f"The Claude API returned an error ({exc.status_code}).") from exc
        except anthropic.APIConnectionError as exc:
            raise AnswerError("Could not reach the Claude API. Check your connection.") from exc

        if final.stop_reason == "refusal":
            raise AnswerError("Claude declined to answer this question.")
        usage = final.usage
        yield AnswerDone(
            model=final.model,
            input_tokens=usage.input_tokens
            + (usage.cache_creation_input_tokens or 0)
            + (usage.cache_read_input_tokens or 0),
            output_tokens=usage.output_tokens,
        )


# Small local models follow an example far better than a rule, so each conversation with one
# starts with a made-up question answered in the cited style. Claude doesn't need it.
CITATION_EXAMPLE: list[dict[str, str]] = [
    {
        "role": "user",
        "content": (
            '<excerpts>\n<excerpt path="app/db.py" lines="10-13" symbol="connect">\n'
            "10 | def connect(url):\n"
            "11 |     engine = create_engine(url, pool_size=5)\n"
            "12 |     engine.connect().close()  # fail fast if the database is down\n"
            "13 |     return engine\n"
            "</excerpt>\n</excerpts>\n\n"
            f"Question: How is the database connection created?\n\n{CITATION_REMINDER}"
        ),
    },
    {
        "role": "assistant",
        "content": (
            "The `connect` function creates a SQLAlchemy engine from the database URL "
            "[app/db.py:10-11]. It keeps a pool of 5 connections [app/db.py:11] and opens one "
            "connection straight away, so a database that is down fails at startup "
            "[app/db.py:12]."
        ),
    },
]


class OllamaAnswerer:
    """A local open-source model served by Ollama. Free and private, but less accurate."""

    def __init__(
        self,
        base_url: str,
        model: str,
        context_tokens: int,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url
        self._url = f"{base_url.rstrip('/')}/api/chat"
        self._context_tokens = context_tokens
        # Loading the model into GPU memory can take a while on the first question.
        self._client = client or httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0))

    def stream(
        self, system: str, messages: Sequence[BetaMessageParam]
    ) -> Iterator[AnswerDelta | AnswerDone]:
        payload = {
            "model": self.model,
            "stream": True,
            "messages": [
                {"role": "system", "content": system},
                *CITATION_EXAMPLE,
                *({"role": m["role"], "content": _plain_text(m["content"])} for m in messages),
            ],
            # Ollama's default window is small enough to silently cut off the excerpts.
            "options": {"num_ctx": self._context_tokens, "temperature": 0.2},
        }
        try:
            with self._client.stream("POST", self._url, json=payload) as response:
                if response.status_code == 404:
                    raise AnswerError(
                        f"Ollama does not have the model {self.model}. "
                        f"Run `docker compose exec ollama ollama pull {self.model}`."
                    )
                if response.is_error:
                    response.read()
                    raise AnswerError(
                        f"Ollama returned {response.status_code}: {response.text[:200]}"
                    )
                for line in response.iter_lines():
                    if not line:
                        continue
                    data: _OllamaChatChunk = json.loads(line)
                    if "error" in data:
                        raise AnswerError(f"Ollama failed: {data['error']}")
                    text = data.get("message", {}).get("content", "")
                    if text:
                        yield AnswerDelta(text)
                    if data.get("done"):
                        yield AnswerDone(
                            model=self.model,
                            input_tokens=data.get("prompt_eval_count", 0),
                            output_tokens=data.get("eval_count", 0),
                        )
                        return
        except httpx.TransportError as exc:
            message = f"Could not reach Ollama at {self._base_url}. Is it running?"
            raise AnswerError(message) from exc
        raise AnswerError("Ollama stopped before finishing the answer.")


class _OllamaChatMessage(TypedDict, total=False):
    content: str


class _OllamaChatChunk(TypedDict, total=False):
    message: _OllamaChatMessage
    done: bool
    error: str
    prompt_eval_count: int
    eval_count: int


def _plain_text(content: object) -> str:
    if not isinstance(content, str):
        raise TypeError("Ollama messages must be plain text")
    return content


class MissingAnthropicKeyError(AnswerError):
    pass


def build_answerer(settings: Settings) -> Answerer:
    if settings.answer_provider == "ollama":
        return OllamaAnswerer(
            settings.ollama_url, settings.ollama_answer_model, settings.ollama_answer_context
        )

    key = settings.anthropic_api_key
    if key is None or not key.get_secret_value():
        raise MissingAnthropicKeyError(
            "ANTHROPIC_API_KEY is not set. Add it to .env and restart the API, "
            "or set ANSWER_PROVIDER=ollama to answer with a free local model."
        )
    client = anthropic.Anthropic(api_key=key.get_secret_value(), max_retries=3)
    return ClaudeAnswerer(client, settings.claude_answer_model, settings.claude_answer_effort)


def select_sources(hits: Sequence[SearchHit], max_chars: int) -> list[SearchHit]:
    """Keep the best-ranked hits that fit in max_chars of code. The top hit is always kept."""
    selected: list[SearchHit] = []
    total = 0
    for hit in hits:
        total += len(hit.chunk.content)
        if selected and total > max_chars:
            break
        selected.append(hit)
    return selected
