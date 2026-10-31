# DevAssist

An AI assistant that reads a GitHub repository, answers questions about it with file-and-line citations, and reviews pull requests automatically.

> **Status:** Week 1 of 8 (project skeleton). See the roadmap below.

## Architecture

```
Next.js (web) ──► FastAPI (api) ──► PostgreSQL + pgvector
                      │
                      └──► Redis ──► Celery workers (indexing, PR reviews)
```

| Layer    | Tech |
|----------|------|
| Backend  | Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Celery |
| Data     | PostgreSQL 16 + pgvector, Redis 7 |
| Frontend | Next.js 15, TypeScript, Tailwind CSS |
| AI       | Claude API, Voyage AI embeddings (from Week 2) |
| DevOps   | Docker Compose, GitHub Actions, AWS + Terraform (Week 7) |

## Run it locally

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
cp .env.example .env
docker compose up --build
```

| Service | URL |
|---------|-----|
| Web app | http://localhost:3000 |
| API docs | http://localhost:8000/docs |
| Health check | http://localhost:8000/health/ready |

### Without Docker

```bash
# API (needs Postgres with pgvector and Redis running somewhere)
cd api
python -m venv .venv
.venv/Scripts/activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload

# Web
cd web
npm install
npm run dev
```

## Checks

These run on every pull request in GitHub Actions.

```bash
cd api && ruff check . && ruff format --check . && mypy app tests && pytest
cd web && npm run lint && npx tsc --noEmit && npm run build
```

## Repo layout

```
api/        FastAPI app, Celery worker, Alembic migrations, tests
web/        Next.js frontend
infra/      Terraform (Week 7)
.github/    CI workflows
```

## Roadmap

- [x] **Week 1:** Project skeleton, Docker Compose, CI
- [ ] **Week 2:** Index a repository (tree-sitter chunking, embeddings)
- [ ] **Week 3:** Q&A with citations (hybrid search, streaming answers)
- [ ] **Week 4:** GitHub App for automatic PR reviews
- [ ] **Week 5:** GitHub sign-in, multiple repos, rate limits
- [ ] **Week 6:** Evaluation set, tests, CI quality gate
- [ ] **Week 7:** Deploy to AWS with Terraform
- [ ] **Week 8:** Metrics dashboard, launch, real users
