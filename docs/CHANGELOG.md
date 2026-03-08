# Changelog

All notable changes to AAA Citation Auditor are documented here.

---

## v0.9.0 — 2026-03 (current)

Initial feature-complete pre-release. All core functionality is implemented and tested.

### Citation extraction

- Extract citations from pasted text, uploaded DOCX files, and uploaded PDF files
- Multi-file batch upload (up to 10 files per submission by default)
- Powered by `eyecite` for standard legal citation detection
- Context snippet captured for each citation (±80 characters of surrounding text)
- Transparent *Id.* resolution: *Id.* and similar back-references are linked to the immediately preceding full citation; unresolvable back-references are shown as-is

### Citation verification

- Live verification against CourtListener's published opinion database
- Verification statuses: `VERIFIED`, `NOT_FOUND`, `AMBIGUOUS`, `DERIVED`, `STATUTE_DETECTED`, `ERROR`, `UNVERIFIED_NO_TOKEN`
- Statute citations (`FullLawCitation`) detected and labelled separately — not sent to CourtListener
- Batch verification mode: groups citations into a single API request for significantly faster processing
- Automatic retry with exponential back-off on transient network errors
- Resolution cache: past disambiguation choices (heuristic and user-selected) are stored locally; repeat citations resolve instantly on future audits

### Disambiguation

- Heuristic auto-resolution of AMBIGUOUS citations using citation text and surrounding context
- Manual candidate selection UI for citations the heuristic cannot resolve
- User selections are saved to the resolution cache for future audits
- Selections can be changed at any time from the History detail page

### AI risk memo

- Optional advisory memo generated after each audit
- Supported providers: OpenAI (any model) and Ollama (any locally-pulled model)
- Memo contents: risk level, summary, top issues, recommended actions
- Advisory-only: clearly labelled as not a substitute for professional review
- Configurable timeout; graceful fallback if AI provider is unavailable

### Audit history

- Every completed audit is persisted to a local SQLite database
- History list page with citation count and status summary per run
- History detail page with full per-citation results and disambiguation UI
- Input text excerpt stored (first 200 characters); full document text is not retained

### Settings

- Runtime configuration via in-app Settings page (no restart required)
- All settings also configurable via environment variables or `.env` file
- Settings stored in the database; UI changes take effect immediately
- Sensitive fields (API tokens, API keys) masked in the UI after saving
- Resolution cache management: view and clear cached disambiguation choices

### Guardrails

- Maximum file size per upload (default: 50 MB)
- Maximum files per batch submission (default: 10)
- Maximum citations per run (default: 500); excess citations skipped with a warning
- Client-side guard prevents empty form submission

### Export

- Export any audit run as Markdown, CSV, or printable HTML
- Export available from both the dashboard results and the history detail page

### Error handling

- Friendly message when no citations are detected in a document
- Green success indicator when all citations resolve cleanly
- CourtListener-unreachable warning when all verification attempts return ERROR
- 404 page for missing history entries with navigation back to history
- Health endpoint at `/api/health` reporting database and CourtListener reachability

### Developer tooling

- Python 3.12, FastAPI, Jinja2 server-rendered templates
- SQLAlchemy 2.0 ORM with Alembic migrations (applied automatically on startup)
- Pytest test suite (198 tests)
- Ruff for linting and formatting
- Docker packaging: `docker compose up` for zero-config one-command installation
- Non-root container user, named volume for database persistence
- Structured logging throughout with configurable log level

---

## Planned

The following features are under consideration for future releases. Nothing below is committed or scheduled.

- Authentication and multi-user support
- Citation verification for secondary sources (law review articles, treatises)
- Integration with additional legal databases
- Bulk re-audit of historical runs after a token is added
- Email or webhook notifications for completed audits
- Dark mode
