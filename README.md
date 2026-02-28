# AAA (AI Agent Auditor)

AAA is a local-first prototype for citation auditing aimed at small law firms, solo practitioners, nonprofits, and local government legal offices.

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
