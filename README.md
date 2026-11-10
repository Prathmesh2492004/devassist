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

> **Status:** ✅ Week 4 done: ask questions about an indexed repository with cited answers, and get an AI review of any pull request with comments on the exact lines, either from the web app or automatically on every push through a GitHub App. See the [roadmap](#roadmap).

## Features

| | Feature | Status |
|---|---|---|
| 💬 | **Codebase Q&A with citations.** Ask "Where do we validate JWTs?" and get a streamed answer that links to the exact files and lines it used. | ✅ Done |
| 🔍 | **PR reviews.** Each changed file is checked by bug and security rules plus a model, and findings appear on the exact lines. Start one from the web app, or install the GitHub App and every new commit on a PR is reviewed and commented on automatically. | ✅ Done |
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
    C --> O[Ollama: local models]
    C -- review comments --> GHAPI[GitHub API]
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
3. **Run the rules.** For Python files, the new version of the file is downloaded and checked with [Ruff](https://docs.astral.sh/ruff/)'s bug and security rules (SQL injection, `shell=True`, `eval`, hardcoded passwords, mutable default arguments, swallowed exceptions and more). Only problems on lines the PR added are kept. Rules are exact, instant and free.
4. **Ask the model.** How depends on the model:
   - **Local model (default): focused checks.** Asked "find any problem", the 3B model almost always answers "none". So it gets narrow questions instead (can a value here be empty or None? can an index go out of range? is a condition inverted?), and reasons before each yes/no. Its answers are noisy, so a problem is reported only when two differently worded passes flag the same check on the same line. A clean file costs one call.
   - **Claude: open review.** A strong model gets the whole diff plus the 3 most related chunks from the index, and reviews freely.

   Every reply must be JSON matching a schema, enforced by Ollama's structured output or Claude's `json_schema` output format.
5. **Check every finding.** A finding on a line the diff doesn't show is dropped, since it is usually invented and GitHub would reject it as a review comment. One finding per line (a rule beats the model), at most 3 per file, most severe first. Each finding says whether it came from a rule or from the AI.
6. **Summarize.** A last call writes a short summary from the findings. The page polls while the review runs, then shows the diff with each finding under its line.

### Measuring the reviewer

[`api/evals/review_cases.py`](api/evals/review_cases.py) holds 15 small changes: 12 with one planted bug each and 3 clean ones. Run `docker compose exec worker python -m evals.review_eval` to score each approach. With `qwen2.5-coder:3b`:

| Approach | Bugs caught | False alarms on clean code |
|---|---|---|
| Open-ended model review (the first version) | 0 of 12 | 0 of 3 |
| Rules only | 8 of 12 | 0 of 3 |
| Rules + focused model checks (current) | 9 of 12 | 1 of 3 |

The rules carry most of the weight. The model adds logic bugs such as dividing by the length of an empty list, but still misses an inverted permission check and a missing `None` check, and sometimes flags clean code, so its findings are labelled "AI check". With the local model a review takes about 10 to 30 seconds per file on a 4 GB GPU, and costs nothing.

## Automatic reviews on GitHub

Install the DevAssist GitHub App on a repository and every pull request is reviewed when it opens and again when new commits are pushed. The findings appear on GitHub as one review from `DevAssist[bot]`, with a comment on each line.

<img src="docs/images/github-review.jpg" alt="A DevAssist review posted on a pull request by the DevAssist bot" width="600">

Try it: [devassist-playground pull request #1](https://github.com/Prathmesh2492004/devassist-playground/pull/1) has three planted bugs. The rules caught the SQL injection and the mutable default argument; the local model missed dividing by the length of a possibly empty cart.

```mermaid
sequenceDiagram
    participant G as GitHub
    participant T as Cloudflare tunnel
    participant A as API
    participant W as Worker
    G->>T: pull_request event, signed with the webhook secret
    T->>A: same bytes
    A->>A: check the signature, skip drafts, repeats and unknown repos
    A-->>G: 202 Accepted (within GitHub's 10 second limit)
    A->>W: queue the review
    W->>G: sign in as the app (JWT, then an installation token)
    W->>G: read the PR, review it, post one review with line comments
```

- **Every event is verified.** GitHub signs each event with an HMAC of the body. Anything without a valid signature is rejected before it is read.
- **Each commit is reviewed once.** GitHub resends events it thinks were lost, so a review of the same commit is never queued twice. A failed review can be retried.
- **It comments, never approves or blocks.** A person decides whether to merge.
- **Comments never get lost.** If GitHub refuses a line comment (the PR changed in the meantime), the findings go into the review text instead. If posting fails, the review is kept and the page says why.

### Set it up (free, about two minutes)

1. Start the tunnel, which gives the API a public HTTPS address. It's a free Cloudflare quick tunnel and needs no account:
   ```bash
   docker compose --profile github-app up -d
   ```
2. Open http://localhost:8000/github-app/setup and click **Create on GitHub**. GitHub shows the app's permissions (read code, write pull request comments) and sends you back. The app's private key and webhook secret are saved in `api/.secrets/`, which git ignores, so no keys are copied by hand.
3. Click **Install the app on GitHub** and pick the repositories to review. Add the same repositories in DevAssist.
4. Open a pull request. The review appears on GitHub within a few minutes, and on the Reviews page marked "Automatic review".

The quick tunnel gets a new address each time it starts. The API checks it every 30 seconds and updates the app's webhook address through GitHub's API, so nothing needs changing by hand. Quick tunnels are temporary and Cloudflare can drop one, which happened during testing: the container keeps running but events stop arriving. The API notices (the tunnel reports zero connections) and logs the fix, `docker compose --profile github-app restart tunnel`. On a server with a fixed address, set `GITHUB_WEBHOOK_URL` instead.

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
| `POST` | `/webhooks/github` | Receives GitHub App events (signature required) and queues reviews |
| `GET` | `/github-app/setup` | Creates the GitHub App through GitHub's manifest flow |

Interactive docs are at http://localhost:8000/docs.

## Project structure

```
devassist/
├── api/                    FastAPI backend
│   ├── app/
│   │   ├── ingest/         Clone, filter, chunk (tree-sitter), embed, store
│   │   ├── qa/             Hybrid search and cited answers
│   │   ├── review/         Pull requests: diffs, rules, model checks, GitHub App, posting
│   │   ├── routes/         HTTP endpoints
│   │   ├── models.py       SQLAlchemy models
│   │   ├── worker.py       Celery app and tasks
│   │   └── main.py         App factory
│   ├── evals/              Test sets that measure review quality
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
- [x] **Week 4: PR reviews.** ✅ Review any public PR with inline findings · ✅ Bug rules and checked model findings · ✅ GitHub App: reviews every push and comments on GitHub
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
