# AAA Citation Auditor

**Verify court case citations in legal documents — affordable, private, AI-powered.**

AAA Citation Auditor is a self-hosted tool for small law firms, solo practitioners, nonprofits, and local government legal offices. It extracts citations from uploaded documents or pasted text, verifies them against CourtListener's database of published opinions, and optionally generates an AI-powered risk memo — all without your documents ever leaving your network.

---

## Features

- **Citation extraction** from DOCX, PDF, or pasted text
- **Case law verification** against CourtListener (the largest free legal database in the world)
- **Statute detection** — automatically identifies and labels statutory citations
- **Smart disambiguation** — resolves ambiguous citations with heuristics and lets you confirm manually
- **AI risk memos** — advisory summaries powered by OpenAI or a free local model via Ollama
- **Audit history** — every run is saved and searchable; export at any time
- **Export** to Markdown, CSV, and printable HTML
- **Privacy-first** — runs entirely on your own machine; no documents are sent to any cloud service

---

## Quick Start (Docker — recommended)

**Prerequisite:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running.

```bash
# 1. Clone the repository
git clone https://github.com/byeonhosa/aaa-citation-auditor.git
cd aaa-citation-auditor

# 2. Start the app
docker compose up -d

# 3. Open your browser
open http://localhost:8000
```

The app is ready. Audit history is saved automatically and persists across restarts.

### Add a CourtListener token (free — enables live verification)

Without a token, citations are marked `UNVERIFIED_NO_TOKEN` but the app still runs.

1. Create a free account at [courtlistener.com](https://www.courtlistener.com/) and copy your API token.
2. In the app, go to **Settings → CourtListener Integration** and paste the token. No restart needed.

### Enable Ollama for free local AI memos

1. In `docker-compose.yml`, uncomment the `ollama` service block and the `ollama-data` volume.
2. Set `AI_PROVIDER: "ollama"` and `OLLAMA_BASE_URL: "http://ollama:11434"` under `aaa-app`.
3. Start and pull a model:

   ```bash
   docker compose up -d
   docker compose exec ollama ollama pull llama3.2
   ```

4. In **Settings → AI Risk Memo**, select **Ollama** and set the model to `llama3.2`.

### Back up your data

All data lives in a Docker volume (`aaa-data`). To copy it to your current directory:

```bash
docker run --rm \
  -v aaa-citation-auditor_aaa-data:/data \
  -v $(pwd):/backup \
  alpine cp /data/aaa.db /backup/aaa-backup.db
```

---

## Manual Installation (Python)

For developers or users who prefer not to use Docker.

**Requirements:** Python 3.12+

```bash
# 1. Clone and enter the repository
git clone https://github.com/byeonhosa/aaa-citation-auditor.git
cd aaa-citation-auditor

# 2. Create and activate a virtual environment
python3.12 -m venv .venv
source .venv/bin/activate        # macOS/Linux
.venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install --upgrade pip
pip install -e '.[dev]'

# 4. Run the app
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>.

Optionally create a `.env` file to set configuration values (see the table below).

---

## Configuration Reference

All settings can be changed at runtime via **Settings** in the app UI. Environment variables and `.env` file values are used as defaults on first run.

| Setting | Environment variable | Default | Description |
|---------|---------------------|---------|-------------|
| CourtListener token | `COURTLISTENER_TOKEN` | _(none)_ | API token from courtlistener.com. Without it, citations are marked `UNVERIFIED_NO_TOKEN`. |
| Verification URL | `VERIFICATION_BASE_URL` | CourtListener endpoint | Override only if self-hosting CourtListener. |
| Request timeout | `COURTLISTENER_TIMEOUT_SECONDS` | `30` | Seconds before a CourtListener request is abandoned. |
| AI provider | `AI_PROVIDER` | `none` | `none`, `openai`, or `ollama`. |
| OpenAI API key | `OPENAI_API_KEY` | _(none)_ | Required when AI provider is `openai`. |
| AI memo model | `AI_MEMO_MODEL` | `gpt-4o-mini` | OpenAI model name. |
| Ollama base URL | `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL. Use `http://ollama:11434` in Docker Compose. |
| Ollama model | `OLLAMA_MODEL` | `llama3.2` | Model name as shown in `ollama list`. |
| AI timeout | `AI_REQUEST_TIMEOUT_SECONDS` | `60` | Seconds before an AI memo request is abandoned. |
| Max file size | `MAX_FILE_SIZE_MB` | `50` | Largest single file the app will accept. |
| Max files per batch | `MAX_FILES_PER_BATCH` | `10` | Maximum files in a single audit submission. |
| Max citations per run | `MAX_CITATIONS_PER_RUN` | `500` | Citations beyond this limit are skipped (with a warning). |
| Log level | `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| Database URL | `DATABASE_URL` | `sqlite:///./aaa.db` | SQLite path or a SQLAlchemy-compatible URL. |
| Resend API key | `RESEND_API_KEY` | _(none)_ | **Required in production.** App refuses to start without it. Get a key at [resend.com](https://resend.com/) and verify the sending domain. |
| From address | `FINALVERIFY_FROM_EMAIL` | `john@finalverify.com` | Address used in the From: header. Must be on a Resend-verified domain. |
| Admin notify email | `NOTIFY_EMAIL` | _(none)_ | Admin recipient for contact-form and waitlist alerts. When unset, those alerts are skipped silently. |

---

## Screenshots

_Screenshots will be added in a future release._

---

## User Guide

A plain-English guide written for lawyers and legal staff is available at [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md).

---

## Contributing

This project is in active development. Bug reports and pull requests are welcome. Please open an issue before starting significant new work.

```bash
# Run the test suite
pytest

# Lint and format
ruff check .
ruff format .
```

---

## License

License to be determined. All rights reserved until a license is chosen.

---

## Developer Notes

**Tech stack:** Python 3.12 · FastAPI · Jinja2 · SQLAlchemy + SQLite · Alembic · pydantic-settings · eyecite · PyMuPDF · python-docx · httpx · openai SDK

**Project structure:**

```
app/
  main.py          ← FastAPI app factory, startup, logging
  settings.py      ← pydantic-settings configuration
  routes/          ← page and API route handlers
  services/        ← citation extraction, verification, AI memo, exporters
  templates/       ← Jinja2 HTML templates
  static/          ← CSS and favicon
aaa_db/
  models.py        ← SQLAlchemy ORM models
  session.py       ← database session factory
  repository.py    ← database read/write helpers
alembic/           ← database migration scripts
docs/              ← user and developer documentation
tests/             ← pytest test suite
```
