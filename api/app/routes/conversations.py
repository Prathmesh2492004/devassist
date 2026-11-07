"""Ask questions about a repo and get streamed answers with citations.

The answer streams as Server-Sent Events, in this order:
  conversation  {"id", "title"}             which conversation the turn belongs to
  sources       [SourceOut, ...]            the code the model was given
  delta         {"text"}                    the next piece of the answer (many of these)
  done          MessageOut                  the saved assistant message, with token counts
or, if something fails after the stream has started:
  error         {"message"}
"""

import json
import logging
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Annotated

from anthropic.types.beta import BetaMessageParam
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import get_db, get_session_factory
from app.ingest.embedder import Embedder, EmbeddingError, build_embedder
from app.models import Conversation, Message, User
from app.qa.answer import (
    SYSTEM_PROMPT,
    AnswerDone,
    Answerer,
    AnswerError,
    Turn,
    build_answerer,
    build_messages,
    select_sources,
)
from app.qa.search import SearchHit, hybrid_search
from app.routes.repos import CurrentUser, get_owned_repo
from app.schemas import AskIn, ConversationOut, ConversationSummary, MessageOut, SourceOut

logger = logging.getLogger(__name__)
router = APIRouter(tags=["questions"])

Db = Annotated[Session, Depends(get_db)]
SessionFactory = Annotated[sessionmaker[Session], Depends(get_session_factory)]

# Earlier turns sent with a follow-up question. Older ones rarely matter and cost tokens.
MAX_HISTORY_TURNS = 6
TITLE_LENGTH = 80


def get_answerer() -> Answerer:
    try:
        return build_answerer(get_settings())
    except AnswerError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


def get_query_embedder() -> Embedder:
    try:
        return build_embedder(get_settings())
    except EmbeddingError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


AnswererDep = Annotated[Answerer, Depends(get_answerer)]
EmbedderDep = Annotated[Embedder, Depends(get_query_embedder)]


def _get_owned_conversation(db: Session, user: User, conversation_id: int) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


def _history(conversation: Conversation) -> list[Turn]:
    """Completed question/answer pairs, oldest first. Failed answers are left out."""
    turns: list[Turn] = []
    messages = conversation.messages
    for question, answer in zip(messages, messages[1:], strict=False):
        if question.role == "user" and answer.role == "assistant" and answer.error is None:
            turns.append(Turn(question.content, answer.content))
    return turns[-MAX_HISTORY_TURNS:]


def _source(hit: SearchHit) -> SourceOut:
    chunk = hit.chunk
    return SourceOut(
        path=chunk.path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        symbol=chunk.symbol,
        language=chunk.language,
        content=chunk.content,
    )


def _event(name: str, data: object) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


@router.post("/repos/{repo_id}/ask")
def ask(
    repo_id: int,
    body: AskIn,
    db: Db,
    user: CurrentUser,
    answerer: AnswererDep,
    embedder: EmbedderDep,
    session_factory: SessionFactory,
) -> StreamingResponse:
    """Answer a question about the repo's code, streamed as Server-Sent Events."""
    repo = get_owned_repo(db, user, repo_id)
    if repo.last_indexed_sha is None or repo.chunk_count == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "This repository hasn't been indexed yet")

    if body.conversation_id is None:
        conversation = Conversation(
            repo_id=repo.id, owner_id=user.id, title=body.question[:TITLE_LENGTH]
        )
        db.add(conversation)
        history: list[Turn] = []
    else:
        conversation = _get_owned_conversation(db, user, body.conversation_id)
        if conversation.repo_id != repo.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
        history = _history(conversation)

    # A follow-up like "and where is it tested?" means little alone, so search with the
    # previous question too.
    search_text = f"{history[-1].question}\n{body.question}" if history else body.question
    try:
        query_vector = embedder.embed_query(search_text)
    except EmbeddingError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    settings = get_settings()
    hits = select_sources(
        hybrid_search(db, repo.id, search_text, query_vector, limit=settings.answer_sources),
        settings.answer_source_chars,
    )

    db.add(Message(conversation=conversation, role="user", content=body.question))
    db.commit()

    stream = _stream_answer(
        session_factory,
        answerer,
        conversation_id=conversation.id,
        title=conversation.title,
        commit_sha=repo.last_indexed_sha,
        system=SYSTEM_PROMPT.format(repo=repo.full_name),
        messages=build_messages(body.question, hits, history),
        sources=[_source(hit) for hit in hits],
    )
    # X-Accel-Buffering stops proxies such as nginx from holding the stream back.
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _stream_answer(
    session_factory: sessionmaker[Session],
    answerer: Answerer,
    *,
    conversation_id: int,
    title: str,
    commit_sha: str,
    system: str,
    messages: Sequence[BetaMessageParam],
    sources: list[SourceOut],
) -> Iterator[str]:
    started = time.monotonic()
    yield _event("conversation", {"id": conversation_id, "title": title})
    yield _event("sources", [s.model_dump() for s in sources])

    parts: list[str] = []
    done: AnswerDone | None = None
    error: str | None = None
    try:
        for event in answerer.stream(system, messages):
            if isinstance(event, AnswerDone):
                done = event
            else:
                parts.append(event.text)
                yield _event("delta", {"text": event.text})
    except AnswerError as exc:
        error = str(exc)
    except Exception:
        logger.exception("Answering failed for conversation %s", conversation_id)
        error = "Something went wrong while writing the answer. Please try again."

    # The request's session may already be closed, so save with a fresh one.
    with session_factory() as db:
        message = Message(
            conversation_id=conversation_id,
            role="assistant",
            content="".join(parts),
            sources=[s.model_dump() for s in sources],
            commit_sha=commit_sha,
            model=done.model if done else answerer.model,
            input_tokens=done.input_tokens if done else None,
            output_tokens=done.output_tokens if done else None,
            latency_ms=round((time.monotonic() - started) * 1000),
            error=error,
        )
        db.add(message)
        conversation = db.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.updated_at = datetime.now(UTC)
        db.commit()
        saved = MessageOut.model_validate(message)

    if error is not None:
        yield _event("error", {"message": error})
    else:
        yield _event("done", saved.model_dump(mode="json"))


@router.get("/repos/{repo_id}/conversations")
def list_conversations(repo_id: int, db: Db, user: CurrentUser) -> list[ConversationSummary]:
    get_owned_repo(db, user, repo_id)
    conversations = db.scalars(
        select(Conversation)
        .where(Conversation.repo_id == repo_id, Conversation.owner_id == user.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
    )
    return [ConversationSummary.model_validate(c) for c in conversations]


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int, db: Db, user: CurrentUser) -> ConversationOut:
    return ConversationOut.model_validate(_get_owned_conversation(db, user, conversation_id))


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: int, db: Db, user: CurrentUser) -> None:
    db.delete(_get_owned_conversation(db, user, conversation_id))
    db.commit()
