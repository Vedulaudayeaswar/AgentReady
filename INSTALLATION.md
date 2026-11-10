# Installation

Two ways to run AgentReady: the full Docker stack (recommended; production-like) or local development with SQLite
and in-process jobs. For a public server use [DEPLOYMENT.md](DEPLOYMENT.md).

## Requirements

| | Docker stack | Local development |
|---|---|---|
| Software | Docker Engine 24+ with Compose v2 | Python 3.12, Node 20, (optional) Docker for PostgreSQL/Redis |
| Memory | 4 GB free | 2 GB |
| Ports (localhost only) | 8080 web, 8000 API, 5432 PostgreSQL (`POSTGRES_HOST_PORT`) | 5173 web, 8000 API |
| Optional | `GROQ_API_KEY` for AI extraction/assistant | same |

## 1. Docker stack

```bash
git clone <repository> agentready && cd agentready
cp .env.example .env
```

Edit `.env` and set at least:

```bash
SECRET_KEY=<python -c "import secrets;print(secrets.token_urlsafe(48))">
CREDENTIALS_ENCRYPTION_KEY=<python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())">
GROQ_API_KEY=gsk_...          # optional: without it extraction is deterministic only and the assistant uses its fallback
```

Start (Windows PowerShell shown; on macOS/Linux use `export POSTGRES_HOST_PORT=55432`):

```powershell
$env:POSTGRES_HOST_PORT="55432"   # only if port 5432 is already used on your machine
docker compose up -d --build
docker compose ps                 # all six services "Up"; api, web, worker "(healthy)"
curl.exe http://localhost:8000/health
```

`/health` should report `"status":"ok"` with `database`, `redis`, `worker` and `scheduler` all `ok` (the scheduler
check needs about one minute after start for the first tick).

| What | URL |
|---|---|
| Web app (sign up here) | http://localhost:8080/signup |
| API reference | http://localhost:8000/docs |
| Health | http://localhost:8000/health |

The API container waits for PostgreSQL, applies migrations (`alembic upgrade head`) and then starts. Data lives
in the `pgdata` volume (and Redis' queue in `redisdata`): `docker compose down` keeps it, `docker compose down -v`
**deletes it**.

## 2. Local development (no Docker)

Backend:

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate                 # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
```

Create `backend/.env` (the backend reads only this file; the root `.env` is for Docker Compose):

```bash
ENVIRONMENT=development
DATABASE_URL=sqlite+aiosqlite:///./agentready.db
JOB_BACKEND=inline
SECRET_KEY=<random string>
CREDENTIALS_ENCRYPTION_KEY=<fernet key>
GROQ_API_KEY=<optional>
```

```bash
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Frontend:

```bash
cd frontend
npm ci
npm run dev                            # http://localhost:5173, proxies /api to localhost:8000
```

`JOB_BACKEND=inline` runs scans and other jobs inside the API process — development only (the API refuses it in
production). To use PostgreSQL + Celery locally, start `docker compose up -d postgres redis`, set
`DATABASE_URL=postgresql+asyncpg://agentready:agentready@localhost:55432/agentready`, `JOB_BACKEND=celery`,
`REDIS_URL=redis://localhost:6379/0` (publish Redis' port first) and run
`celery -A app.workers.celery_app worker` and `celery -A app.workers.celery_app beat` in two more terminals.

## 3. Verify the installation

```bash
cd backend
pytest -q                                            # backend suite on SQLite
TEST_DATABASE_URL=postgresql+asyncpg://agentready:agentready@localhost:55432/agentready_test pytest -q
cd ../frontend && npm run typecheck && npm test && npm run build
```

The PostgreSQL run needs an **empty, dedicated** database (the tests drop and recreate its tables):
`docker compose exec postgres createdb -U agentready agentready_test`. Never point `TEST_DATABASE_URL` at the
application database.

Browser walkthroughs (Playwright; they create their own merchants):

```bash
pip install -r requirements-playwright.txt && python -m playwright install chromium
AR_WEB=http://localhost:8080 python eval/ui_walkthrough_phase5.py      # every page, desktop + phone, CSP, headers
AR_WEB=http://localhost:8080 python eval/ui_walkthrough.py             # Phase 1 merchant journey
```

Phase 2 and 3 walkthroughs need dev-only network overrides; see [README.md](README.md#verifying-a-deployment).

## 4. Configure Groq (optional)

1. Set `GROQ_API_KEY` in `.env` (Docker) or `backend/.env` (local) and restart the API and worker
   (`docker compose up -d api worker`).
2. `GET /health` shows `"ai_configured": true`.
3. In the app: **Settings → AI usage → Verify model** confirms the key and that `GROQ_MODEL` is available.

AgentReady calls Groq's REST API with `httpx`; no Groq or OpenAI SDK is installed.

## 5. Stop / reset

```bash
docker compose stop            # stop, keep data
docker compose up -d           # start again
docker compose down -v         # DELETE all data (fresh start) — take a backup first: scripts/backup.sh
```
