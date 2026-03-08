# AAA (AI Agent Auditor)

AAA is a local-first prototype for citation auditing aimed at small law firms, solo practitioners, nonprofits, and local government legal offices.

## Docker quick-start (recommended)

**Prerequisites:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running.

```bash
# 1. Clone the repository
git clone https://github.com/byeonhosa/aaa-citation-auditor.git
cd aaa-citation-auditor

# 2. Start the app
docker compose up -d

# 3. Open your browser
open http://localhost:8000
```

That's it. The app runs on port 8000 and your audit history is saved automatically across restarts.

### Configure CourtListener (free — enables live citation verification)

1. Create a free account at [courtlistener.com](https://www.courtlistener.com/) and copy your API token.
2. Copy the override example and add your token:

   ```bash
   cp docker-compose.override.yml.example docker-compose.override.yml
   # Edit docker-compose.override.yml and set COURTLISTENER_TOKEN
   docker compose up -d
   ```

   Or use the in-app Settings page (no restart required).

### Enable Ollama for free local AI memos

Ollama runs AI models on your own machine — no API key or subscription needed.

1. In `docker-compose.yml`, uncomment the `ollama` service block and the `ollama-data` volume.
2. In the same file, set `AI_PROVIDER: "ollama"` and `OLLAMA_BASE_URL: "http://ollama:11434"` under `aaa-app`.
3. Start everything and pull a model:

   ```bash
   docker compose up -d
   docker compose exec ollama ollama pull llama3.2
   ```

### Access and back up your data

All audit data is stored in a Docker volume named `aaa-data`. To back it up:

```bash
# Copy the database out of the volume to your current directory
docker run --rm -v aaa-citation-auditor_aaa-data:/data -v $(pwd):/backup \
  alpine cp /data/aaa.db /backup/aaa-backup.db
```

To reset all data (start fresh), remove the volume:

```bash
docker compose down -v
```

## Current prototype scope (Phase 2)

- Server-rendered dashboard with:
  - pasted text input
  - `.docx` / `.pdf` upload input
  - audit submit action
- Citation extraction pipeline using:
  - `python-docx` for `.docx` text extraction
  - `PyMuPDF` (`fitz`) for `.pdf` text extraction
  - `eyecite` for citation detection
- Citation verification layer:
  - with no token: citations are marked `UNVERIFIED_NO_TOKEN`
  - with token: verifier returns `VERIFIED`, `NOT_FOUND`, `AMBIGUOUS`, or `ERROR`
- Minimal transparent `Id.` resolution:
  - `Id.` references the last full citation when available
  - unresolved `Id.` is shown as unresolved (no guessing)

## Tech stack

- Python 3.12
- FastAPI
- Jinja2 templates (server-rendered)
- HTMX
- SQLAlchemy + SQLite (scaffold only for now)
- pydantic-settings
- pytest
- ruff

## Project structure

```text
app/
  main.py
  settings.py
  routes/
  services/
  templates/
  static/
aaa_db/
  models.py
  session.py
tests/
scripts/
```

## Local setup

1. Ensure Python 3.12 is installed.
2. Create and activate a virtual environment:

   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:

   ```bash
   pip install --upgrade pip
   pip install -e '.[dev]'
   ```

4. (Optional) create a `.env` file from example:

   ```bash
   cp .env.example .env
   ```

## Run the app

```bash
uvicorn app.main:app --reload
```

Or:

```bash
./scripts/run_dev.sh
```

Open <http://127.0.0.1:8000>.


## Optional verification settings

Set these in `.env` to enable live verification attempts:

- `COURTLISTENER_TOKEN`
- `VERIFICATION_BASE_URL` (defaults to CourtListener citation lookup endpoint)
- `VERIFICATION_TIMEOUT_SECONDS`

If no token is provided, the app still works and marks citations as `UNVERIFIED_NO_TOKEN`.

## Routes

- `GET /` → Audit Dashboard page
- `POST /audit` → Run local audit against pasted text or uploaded file (extract + verify)
- `GET /history` → History page placeholder
- `GET /settings` → Settings page placeholder
- `GET /api/health` → `{"status": "ok"}`

## Tests

```bash
pytest
```

## Lint and format

```bash
ruff check .
ruff format .
```

## Not included yet

- CourtListener integration
- database-backed audit history
- authentication/authorization
- Docker/background workers/Alembic


## Local schema note (v0, no migrations)

This prototype currently uses `Base.metadata.create_all(...)` and does not include Alembic migrations yet.
If you pull schema changes (for example, new columns like citation snippets) and your local `aaa.db` was created by an older version, delete `aaa.db` and restart the app to recreate tables with the latest schema.
