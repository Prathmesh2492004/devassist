"""Find the chunks of a repo that best answer a question.

Two searches run side by side and their rankings are merged:
- Vector search finds code with the same meaning ("where do we retry payments?").
- Keyword search finds exact identifiers that embeddings blur (`verify_jwt`, `MAX_RETRIES`).

Reciprocal rank fusion (RRF) merges them using only each result's rank, so the two very
different score scales never have to be compared.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import Chunk

# Each search returns this many candidates before fusion.
CANDIDATES = 40
# Standard RRF constant: dampens the gap between rank 1 and rank 2.
RRF_K = 60
MAX_KEYWORDS = 12

# Words that appear in most questions and almost all code, so they only add noise.
STOP_WORDS = frozenset(
    """and are can does did for from how its that the there their this was what when where
    which who why will with you your code file files function functions method work works
    """.split()  # noqa: SIM905 - a word list reads better than a list of quoted strings
)
WORD = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float
    vector_rank: int | None
    keyword_rank: int | None


def keyword_query(question: str) -> str | None:
    """Turn a question into an OR tsquery of its distinctive words, or None if there are none.

    Splits on anything that isn't a letter or digit, the same way Postgres' parser splits
    snake_case, so `verify_jwt` matches as `verify | jwt`. Only [a-z0-9] reaches the query
    string, so user input can't inject tsquery syntax.
    """
    words: list[str] = []
    for word in WORD.findall(question.lower()):
        if len(word) < 3 or word in STOP_WORDS or word in words:
            continue
        words.append(word)
    if not words:
        return None
    # Prefix match so "cookie" also finds "cookies" and "cookie_name".
    return " | ".join(f"{w}:*" for w in words[:MAX_KEYWORDS])


def hybrid_search(
    db: Session,
    repo_id: int,
    question: str,
    query_vector: Sequence[float],
    *,
    limit: int = 8,
) -> list[SearchHit]:
    # HNSW indexes the whole table; filtering by repo afterwards can leave too few rows.
    # Iterative scan keeps walking the graph until enough rows pass the filter (pgvector 0.8+).
    db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    db.execute(text("SET LOCAL hnsw.ef_search = 100"))

    distance = Chunk.embedding.cosine_distance(query_vector)
    vector_ids = db.scalars(
        select(Chunk.id).where(Chunk.repo_id == repo_id).order_by(distance).limit(CANDIDATES)
    ).all()

    keyword_ids: Sequence[int] = []
    tsquery_text = keyword_query(question)
    if tsquery_text is not None:
        tsquery = func.to_tsquery("simple", tsquery_text)
        keyword_ids = db.scalars(
            select(Chunk.id)
            .where(Chunk.repo_id == repo_id, Chunk.search.bool_op("@@")(tsquery))
            .order_by(func.ts_rank_cd(Chunk.search, tsquery).desc(), Chunk.id)
            .limit(CANDIDATES)
        ).all()

    vector_rank = {chunk_id: rank for rank, chunk_id in enumerate(vector_ids, start=1)}
    keyword_rank = {chunk_id: rank for rank, chunk_id in enumerate(keyword_ids, start=1)}
    scores: dict[int, float] = {}
    for ranks in (vector_rank, keyword_rank):
        for chunk_id, rank in ranks.items():
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)

    top = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:limit]
    chunks = {c.id: c for c in db.scalars(select(Chunk).where(Chunk.id.in_(top)))}
    return [
        SearchHit(
            chunk=chunks[chunk_id],
            score=scores[chunk_id],
            vector_rank=vector_rank.get(chunk_id),
            keyword_rank=keyword_rank.get(chunk_id),
        )
        for chunk_id in top
    ]
