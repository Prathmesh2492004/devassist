<div align="center">

# DevAssist

**Ask questions about any codebase. Get answers with file-and-line citations. Get every pull request reviewed automatically.**

[![CI](https://github.com/Prathmesh2492004/devassist/actions/workflows/ci.yml/badge.svg)](https://github.com/Prathmesh2492004/devassist/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Next.js 15](https://img.shields.io/badge/Next.js-15-000000?logo=nextdotjs&logoColor=white)
![PostgreSQL + pgvector](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?logo=postgresql&logoColor=white)

<img src="docs/images/dashboard.jpg" alt="DevAssist dashboard showing the API and database online" width="800">

</div>

> **Status:** 🚧 Week 4 in progress: ask questions about an indexed repository with cited answers, and get an AI review of any public pull request with comments on the exact lines. Posting reviews to GitHub automatically (a GitHub App) comes next. See the [roadmap](#roadmap).

## Features

| | Feature | Status |
|---|---|---|
| 💬 | **Codebase Q&A with citations.** Ask "Where do we validate JWTs?" and get a streamed answer that links to the exact files and lines it used. | ✅ Done |
| 🔍 | **PR reviews.** Enter a pull request and each changed file is reviewed with related code from the index; findings appear inline on the diff. Next: a GitHub App that does this for every new PR and posts the comments on GitHub. | 🚧 In the app; GitHub App next |
| 📊 | **Quality and cost dashboard.** Tracks answer accuracy on a test set, latency, tokens and cost per question. | Week 8 |

## Architecture

```mermaid
flowchart LR
    U[User] --> W[Next.js web app]
    GH[GitHub webhooks] --> A
    W --> A[FastAPI]
    A --> P[(PostgreSQL + pgvector)]
    A --> R[(Redis)]
    R --> C[Celery workers]
    C --> P
    C --> L[Claude API]
    C --> O[Ollama: local embedding model]
    A --> L
```

Slow work (indexing repositories, reviewing pull requests) runs in background workers so the API stays fast. PostgreSQL stores both app data and vector embeddings, so there is one database to run and filters by repo or user are plain SQL joins.

## Tech stack

| Layer | Technology |
|---|---|
| **Backend** | Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Celery |
| **Data** | PostgreSQL 16 with pgvector, Redis 7 |
| **Frontend** | Next.js 15 (App Router), TypeScript, Tailwind CSS |
| **AI** | Local models via Ollama (Qwen2.5-Coder, Qwen3-Embedding) with optional Claude API or Voyage AI, tree-sitter parsing |
| **DevOps** | Docker Compose, GitHub Actions, AWS with Terraform |
| **Quality** | pytest, Ruff, mypy (strict), ESLint, Dependabot |

## Getting started

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)

### Run the full stack

```bash
git clone https://github.com/Prathmesh2492004/devassist.git
cd devassist
cp .env.example .env
docker compose up --build
```

Open http://localhost:3000/dashboard, paste a public repo such as `pallets/flask`, and watch it index. When it's ready, click **Ask questions**.

| Service | URL |
|---|---|
| Web app | http://localhost:3000 |
| API docs (Swagger) | http://localhost:8000/docs |
| Readiness check | http://localhost:8000/health/ready |

<details>
<summary><b>Run without Docker</b></summary>

You need Python 3.12+, Node 22+, and Postgres (with pgvector) and Redis running locally.

```bash
# API
cd api
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload

# Web (in a second terminal)
cd web
npm install
npm run dev
```

</details>

## Development

Every pull request runs these checks in [GitHub Actions](.github/workflows/ci.yml). Run them locally before pushing:

```bash
# API: lint, format, strict type check, tests
cd api && ruff check . && ruff format --check . && mypy app tests && pytest

# Web: lint, type check, production build
cd web && npm run lint && npx tsc --noEmit && npm run build
```

Database tests run against real Postgres with pgvector (start it with `docker compose up -d db`). Each test runs in a transaction that is rolled back, and the test database is built by running every migration down and up, so broken migrations never reach `main`.

## How indexing works

1. **Clone.** A shallow clone (`--depth 1`) of the public repo fetches only the latest commit.
2. **Filter.** Dependencies, build output, lockfiles, minified, generated and binary files are skipped. Symlinks are never followed.
3. **Chunk.** [tree-sitter](https://tree-sitter.github.io/) parses Python, JavaScript, TypeScript and Java and splits them at function, class and method boundaries, keeping the comments and decorators above each one. Large classes are split into a header plus one chunk per method. Other files fall back to overlapping 60-line windows.
4. **Embed.** Chunks are turned into 1024-dimensional vectors in batches, with retries and backoff. See [Embedding providers](#embedding-providers).
5. **Store.** New chunks replace the old ones in a single transaction, so search never sees a half-indexed repo. Each chunk keeps its path, exact line range and commit SHA for citations.

Each chunk is searchable two ways: an **HNSW vector index** for meaning ("where do we retry payments?") and a **Postgres full-text index** for exact identifiers (`verify_jwt`).

| Repo | Lines | Files | Chunks | Clone + parse |
|---|---|---|---|---|
| pallets/flask | 34,573 | 204 | 1,255 | 5.8 s |
| encode/httpx | 23,515 | 95 | 1,187 | 13.0 s |

<sub>Measured locally before embedding.</sub>

### Embedding providers

| | Ollama (default) | Voyage AI |
|---|---|---|
| Model | `qwen3-embedding:0.6b` | `voyage-code-3` |
| Cost | Free | 200M free tokens, then paid |
| Limits | None | 3 requests/min until a payment method is added |
| Privacy | Code never leaves your machine | Code is sent to Voyage |
| Speed (Flask, 1,255 chunks) | About 7 min on a GTX 1650 GPU; over an hour on a laptop CPU | Seconds to minutes |

Switch with `EMBEDDING_PROVIDER=voyage` and `VOYAGE_API_KEY=...` in `.env`, then re-index. Both produce 1024-dimensional vectors, so no database change is needed. The first `docker compose up` downloads the Ollama image and the 639 MB model once. With an NVIDIA GPU, add `COMPOSE_FILE=docker-compose.yml;docker-compose.gpu.yml` to `.env` (use `:` instead of `;` on macOS/Linux).

## How answering works

1. **Search two ways.** The question is embedded with the same model as the code and matched against the HNSW index, while its distinctive words (`verify_jwt` becomes `verify | jwt`) go to Postgres full-text search. Each search returns its top 40 chunks for this repo.
2. **Merge the rankings.** [Reciprocal rank fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf) scores each chunk by `1 / (60 + rank)` in each list, so a chunk found by both searches rises to the top without comparing their very different score scales.
3. **Answer with citations.** The model gets the best chunks (up to 6, and at most 16,000 characters of code) with every line numbered, and is told to back each claim with `[path:start-end]`, citing only code it was shown. The answer streams to the browser over Server-Sent Events.
4. **Check the citations.** The web app turns each citation into a chip. Clicking one opens the cited lines beside the answer, with a GitHub link pinned to the indexed commit, so links never drift as the repo changes. A citation that doesn't match any retrieved chunk is shown struck through.
5. **Follow up.** Conversations are saved. A follow-up question is searched together with the previous one ("and where is it tested?" means little alone), and the last 6 turns are sent as context.

### Answer providers

| | Ollama (default) | Claude API |
|---|---|---|
| Model | `qwen2.5-coder:3b` | `claude-sonnet-5-5` |
| Cost | Free | About $0.02 per question (prepaid credits) |
| Privacy | Code never leaves your machine | Code excerpts are sent to Anthropic |
| Quality | Good for "where is X?" questions; sometimes cites the wrong lines | Better explanations and more reliable citations |

Switch with `ANSWER_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=...` in `.env`, then restart the API. Citations that don't match the retrieved code are struck through in the UI, so weaker answers are easy to spot.

## How reviewing works

1. **Fetch the pull request.** The worker reads the PR and its per-file diffs from the GitHub REST API. Public repos need no token; a free `GITHUB_TOKEN` raises GitHub's limit from 60 to 5,000 requests an hour.
2. **Pick the files.** Only code files are reviewed: deleted files, binaries, lockfiles, docs and JSON are skipped (and listed as skipped), up to 15 files per PR.
3. **Add context.** For each file, the added lines are used as a search query against the index, and the 3 best chunks from the rest of the repo go to the model, so it can see how the changed code is called.
4. **Review one file at a time.** Each diff is shown with new-file line numbers. The model must reply with JSON matching a schema (`{line, severity, comment}` findings), enforced by Ollama's structured output or Claude's `json_schema` output format, so a small model can't return malformed text.
5. **Check every finding.** A finding on a line the diff doesn't show is dropped, since it is usually invented and GitHub would reject it as a review comment. Duplicates go too, and at most 3 findings per file are kept, to keep the signal high.
6. **Summarize.** A last call writes a short summary from the findings. The page polls while the review runs, then shows the diff with each finding under its line.

With the local model a review takes about 10 to 30 seconds per file on a 4 GB GPU, and costs nothing.

## API

| Method | Path | What it does |
|---|---|---|
| `POST` | `/repos` | Connect a public repo (`{"full_name": "owner/name"}`) and start indexing |
| `GET` | `/repos` | List repos with status, progress and stats |
| `GET` | `/repos/{id}` | One repo |
| `POST` | `/repos/{id}/reindex` | Index again from the latest commit |
| `DELETE` | `/repos/{id}` | Remove a repo and its index |
| `POST` | `/repos/{id}/ask` | Ask a question (`{"question": "...", "conversation_id": 1}`); the answer streams as Server-Sent Events |
| `GET` | `/repos/{id}/conversations` | List conversations about a repo |
| `GET` | `/conversations/{id}` | A conversation with its messages, sources and token counts |
| `DELETE` | `/conversations/{id}` | Delete a conversation |
| `POST` | `/repos/{id}/reviews` | Review a pull request (`{"pr_number": 6096}`); runs in the background |
| `GET` | `/repos/{id}/reviews` | List reviews of a repo's pull requests, with status and finding counts |
| `GET` | `/reviews/{id}` | A review with its summary, findings and the reviewed diffs |
| `DELETE` | `/reviews/{id}` | Delete a review |

Interactive docs are at http://localhost:8000/docs.

## Project structure

```
devassist/
├── api/                    FastAPI backend
│   ├── app/
│   │   ├── ingest/         Clone, filter, chunk (tree-sitter), embed, store
│   │   ├── qa/             Hybrid search and cited answers
│   │   ├── review/         Pull request fetching, diff parsing and file-by-file review
│   │   ├── routes/         HTTP endpoints
│   │   ├── models.py       SQLAlchemy models
│   │   ├── worker.py       Celery app and tasks
│   │   └── main.py         App factory
│   ├── migrations/         Alembic migrations
│   └── tests/
├── web/                    Next.js frontend
│   └── src/
│       ├── app/            Pages (landing, dashboard, chat, reviews)
│       └── components/
├── infra/                  Terraform for AWS (Week 7)
├── docs/                   Screenshots and design notes
├── .github/                CI, Dependabot, issue and PR templates
└── docker-compose.yml      Full local stack
```

## Roadmap

- [x] **Week 1: Foundations.** Monorepo, FastAPI and Next.js skeletons, Docker Compose, CI
- [x] **Week 2: Ingestion.** Clone repos, chunk code with tree-sitter, store embeddings in pgvector
- [x] **Week 3: Retrieval.** Hybrid search, streamed answers with citations, saved conversations
- [ ] **Week 4: PR reviews.** ✅ Review any public PR with inline findings · ⬜ GitHub App: webhooks and posting reviews to GitHub
- [ ] **Week 5: Product.** GitHub sign-in, multiple repos, rate limiting, incremental re-indexing
- [ ] **Week 6: Quality.** 50-question evaluation set, accuracy gate in CI, 80% test coverage
- [ ] **Week 7: Deploy.** AWS with Terraform, HTTPS, CI/CD, monitoring
- [ ] **Week 8: Launch.** Metrics dashboard, demo video, real users

## Screenshots

| Landing page | Dashboard |
|---|---|
| <img src="docs/images/landing.jpg" alt="DevAssist landing page" width="400"> | <img src="docs/images/dashboard.jpg" alt="DevAssist dashboard" width="400"> |

## License

[MIT](LICENSE) © 2026 Prathmesh Bhilegaonkar
