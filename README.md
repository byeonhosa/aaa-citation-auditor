# AAA (AI Agent Auditor)

AAA is a local-first prototype scaffold for a citation-verification app aimed at small law firms, solo practitioners, nonprofits, and local government legal offices.

This initial version includes only the base app structure, routes, settings, templates, and tests.

## Tech stack

- Python 3.12
- FastAPI
- Jinja2 templates (server-rendered)
- HTMX
- SQLAlchemy + SQLite
- pydantic-settings
- pytest
- ruff

## Project structure

```text
app/
  main.py
  settings.py
  routes/
  templates/
  static/
aaa_db/
  models.py
  session.py
tests/
scripts/
```

The layout is intentionally simple and local-first so it can run on a laptop, Raspberry Pi, or small VPS.

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

## Routes

- `GET /` → Audit Dashboard page
- `GET /history` → History page
- `GET /settings` → Settings page
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

## Next phase hooks

The scaffold is prepared for adding:

- paste text input
- docx/pdf upload
- citation extraction
- Id. resolution
- CourtListener verification
- audit history persistence
