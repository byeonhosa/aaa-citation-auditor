# FinalVerify — Claude Code Project Instructions

## Session Start (MANDATORY)
`git pull` to sync any changes pushed from the droplet or other sessions.

## Session End (MANDATORY)
1. Commit with a descriptive message and `git push`.
2. Update the Obsidian vault (see Obsidian section below).

## Product
FinalVerify — an AI-powered legal citation auditor that verifies case citations, statutes, and regulatory references in legal briefs. Features a three-tier trust architecture and PDF verification reports with SHA-256 fingerprinting.

- **Repo:** https://github.com/byeonhosa/aaa-citation-auditor (public)
- **Droplet:** 159.89.224.101 (4GB RAM, Ubuntu 24.04, NYC1)
- **Live URL:** finalverify.com
- **Vault context:** `improved-vault/10-Products/FinalVerify/Current-Context.md` in the `byeonhosa/dryden-vault` repo

## Workflow
- Claude Code creates/edits files, commits, and pushes (typically via PR; can push directly to main for trivial changes).
- **Sensitive or large changes go through a PR** so John can review before merge. Email, secrets, and auth changes always count as sensitive.
- **Tests are not run on the laptop.** Windows + no Python deps installed. Test execution happens in CI (GitHub Actions runs pytest, ruff, and an Alembic migration smoke test on every push and PR to main) or directly on the droplet via SSH (see "Direct droplet access" below). `pytest -q` from inside the container runs the full test suite and is the authoritative gate.

## Direct droplet access
SSH key auth is configured. Password auth has been **disabled** on the droplet (Ed25519 key only).

- **SSH alias** (defined in laptop `~/.ssh/config`): `ssh finalverify`
  Resolves to `root@159.89.224.101` using `~/.ssh/id_ed25519`.
- **Repo path on droplet:** `/opt/aaa-citation-auditor` (the historical repo name; the product is FinalVerify but the directory keeps the original).
- **Running container:** `aaa-citation-auditor-aaa-app-1` (compose service name `aaa-app`).
- **Persistent data:** `aaa-data` named volume mounted at `/data` in the container; SQLite DB at `/data/aaa.db`.
- **Database migrations** run automatically on container start via `docker-entrypoint.sh` (Alembic `upgrade head`).
- **Production secrets** live in `/opt/aaa-citation-auditor/.env` on the droplet only. `.env` is gitignored. `.env.example` in the repo lists the core keys; `app/settings.py` is the full list of settings.

### Common droplet commands

Run from the laptop (or from this Claude session):

    ssh finalverify "cd /opt/aaa-citation-auditor && docker compose ps"
    ssh finalverify "cd /opt/aaa-citation-auditor && docker compose logs --tail 50 aaa-app"
    ssh finalverify "cd /opt/aaa-citation-auditor && git pull && docker compose up -d --build"
    ssh finalverify "cd /opt/aaa-citation-auditor && docker compose exec -T aaa-app pytest -q"

### Direct Python inside the container

For verifying integrations end-to-end without going through the HTTP layer:

    ssh finalverify "cd /opt/aaa-citation-auditor && docker compose exec -T aaa-app python -c '
    from app.services.notifications import send_test_email
    print(send_test_email(\"recipient@example.com\"))
    '"

Note: `docker compose exec` spawns a fresh Python process whose default log
level is `WARNING`; the integration's INFO-level log lines are suppressed
unless you `import logging; logging.basicConfig(level=logging.INFO)` at the
top of the script. The function's return value is the source of truth.

## Tech Stack
- **Backend:** FastAPI (Python 3.12)
- **Database:** SQLite (`sqlite:////data/aaa.db` on droplet, `sqlite:///./aaa.db` locally)
- **ORM:** SQLAlchemy 2.0 + Alembic migrations
- **Auth:** bcrypt password hashing + Starlette `SessionMiddleware` (no JWT). Admin convention: `user_id == 1`.
- **AI:** Currently `AI_PROVIDER=openai` on the droplet for AI Risk Memos. Ollama is supported as a free local alternative (set `AI_PROVIDER=ollama`).
- **Email:** Resend HTTPS API. App refuses to start without `RESEND_API_KEY`. Do not send mail with smtplib: DigitalOcean blocks outbound SMTP and the messages are dropped silently.
- **Citation index:** 15.5M CourtListener citations (~88% cache hit rate).
- **Hosting:** DigitalOcean droplet, Ubuntu 24.04, single Docker container.

## Key Features
- Three-tier trust architecture (authoritative / algorithmic / user-submitted)
- Provenance class split: direct, heuristic, user-confirmed, short-cite, search, CAP, local-index, supra-ref, parallel-cite
- PDF verification reports with SHA-256 fingerprinting
- Opposing Counsel Check
- Full test suite, run in CI on every push and PR to main

## Known Issues
- GovInfo API returns 500s on transient upstream issues; the code logs and gives up rather than retrying with backoff.
- Branding retirement is partial: templates clean, but `app/settings.py:14` (`app_name`), `app/services/exporters.py:43` (Markdown export header), README, USER_GUIDE, CHANGELOG, and `pyproject.toml` still carry "AAA". The `aaa_db/` package directory rename is a larger refactor.
- `docs/USER_GUIDE.md:268` still claims the product "runs entirely on your own computer" — contradicts the hosted reality. Landing page is correct.
- Monetization is a green field: no `User.plan` field, no Stripe, no gating.

## Conventions
- Pydantic v2: use `model_config = ConfigDict(from_attributes=True)`, not deprecated `class Config`.
- Run tests on the droplet (`docker compose exec aaa-app pytest -q`) before merging changes to core verification logic.
- Commit messages should be descriptive.
- Never commit `.env`, `.env.*`, secrets, or database files. Add any new core variable to `.env.example`.

## Obsidian Vault Update (MANDATORY — end of every session)

The vault is the Git repo `byeonhosa/dryden-vault`. In a cloud session it is
available only if it was attached to the session. If the vault is not
reachable, say so in the final message; do not skip silently and do not
create these paths inside this repo.

For the editing rules, structure overview, and commit-and-push workflow, see
the vault's own CLAUDE.md at `improved-vault/CLAUDE.md` — it is the source of
truth. This section names only the FinalVerify-specific files to touch. Paths
are relative to the vault repo root.

**File 1 — overwrite each time:**
`improved-vault/10-Products/FinalVerify/Current-Context.md`

Follow the canonical template at
`improved-vault/50-Templates/Current-Context-Template.md`.
The template is the source of truth — do not enumerate sections here, because
that's what created drift in the first place. Required vs. optional sections
are marked inside the template.

**File 2 — new file each session (append-only history):**
`improved-vault/10-Products/FinalVerify/Development-Status/FinalVerify_Development_Status_[YYYYMMDD_HHMMSS].md`

Sections: Session Summary (commit, duration), Changes Made, Test Results,
Decisions Made, Backlog Items Discovered.

Create the Development-Status directory if it doesn't exist.

**Commit + push the vault changes** with a message of the form
`vault: FinalVerify: <what changed and why>` (per the convention in the
vault's CLAUDE.md). Direct edits without commit-and-push create drift — the
hourly Pi auto-backup cron is a safety net, not the workflow.
