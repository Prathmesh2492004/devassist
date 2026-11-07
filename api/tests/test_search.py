import pytest
from sqlalchemy.orm import Session

from app.models import Chunk, Repo, User
from app.qa.search import hybrid_search, keyword_query
from tests.fakes import fake_vector


def test_keyword_query_keeps_distinctive_words_as_prefix_terms() -> None:
    assert keyword_query("Where do we verify_jwt tokens?") == "verify:* | jwt:* | tokens:*"


def test_keyword_query_drops_duplicates_stop_words_and_short_words() -> None:
    assert keyword_query("How does the session work in the session code?") == "session:*"


def test_keyword_query_returns_none_when_nothing_is_left() -> None:
    assert keyword_query("how does it work?") is None


@pytest.mark.parametrize("question", ["a' | !b:* & (c", "x <-> y", "foo:* | bar\\"])
def test_keyword_query_never_passes_tsquery_syntax_through(question: str) -> None:
    query = keyword_query(question) or ""
    terms = query.split(" | ") if query else []
    assert all(t.endswith(":*") and t[:-2].isalnum() for t in terms)


def _chunk(repo: Repo, path: str, symbol: str, content: str, line: int = 1) -> Chunk:
    return Chunk(
        repo=repo,
        commit_sha="a" * 40,
        path=path,
        language="python",
        kind="function",
        symbol=symbol,
        start_line=line,
        end_line=line + content.count("\n"),
        content=content,
        embedding=fake_vector(content),
    )


@pytest.fixture
def repo(db: Session) -> Repo:
    repo = Repo(owner=User(github_id=7, login="octocat"), full_name="octocat/shop")
    db.add_all(
        [
            _chunk(
                repo, "auth.py", "verify_jwt", "def verify_jwt(token):\n    return decode(token)"
            ),
            _chunk(repo, "billing.py", "charge", "def charge(card):\n    return gateway.pay(card)"),
            _chunk(repo, "billing.py", "refund", "def refund(id):\n    return gateway.undo(id)", 9),
        ]
    )
    db.commit()
    return repo


def test_exact_identifier_ranks_first_even_with_an_unrelated_vector(
    repo: Repo, db: Session
) -> None:
    hits = hybrid_search(db, repo.id, "where is verify_jwt?", fake_vector("unrelated"))

    assert hits[0].chunk.symbol == "verify_jwt"
    assert hits[0].keyword_rank == 1
    assert hits[0].vector_rank is not None  # found by both searches, so it scores highest


def test_vector_match_ranks_first_when_keywords_find_nothing(repo: Repo, db: Session) -> None:
    content = "def refund(id):\n    return gateway.undo(id)"

    hits = hybrid_search(db, repo.id, "how is money returned?", fake_vector(content))

    assert hits[0].chunk.symbol == "refund"
    assert hits[0].vector_rank == 1 and hits[0].keyword_rank is None


def test_search_only_returns_chunks_from_the_requested_repo(repo: Repo, db: Session) -> None:
    other = Repo(owner=repo.owner, full_name="octocat/other")
    db.add(_chunk(other, "auth.py", "verify_jwt", "def verify_jwt(token):\n    pass"))
    db.commit()

    hits = hybrid_search(db, repo.id, "verify_jwt", fake_vector("verify_jwt"), limit=10)

    assert len(hits) == 3
    assert {hit.chunk.repo_id for hit in hits} == {repo.id}


def test_limit_caps_the_number_of_hits(repo: Repo, db: Session) -> None:
    assert len(hybrid_search(db, repo.id, "gateway", fake_vector("x"), limit=2)) == 2
