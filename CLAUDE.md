# FinalVerify — Claude Code Project Instructions

## Session Start (MANDATORY)
1. `git pull` to sync any changes pushed from the droplet or other sessions.
2. Read this file.

## Session End (MANDATORY)
1. Commit with a descriptive message and `git push`.
2. Update the Obsidian vault (see Obsidian section below).

## Product
FinalVerify — an AI-powered legal citation auditor that verifies case citations, statutes, and regulatory references in legal briefs. Features a three-tier trust architecture and PDF verification reports with SHA-256 fingerprinting.

- **Repo:** https://github.com/byeonhosa/aaa-citation-auditor (public)
- **Droplet:** 159.89.224.101 (4GB RAM, Ubuntu 24.04, NYC1)
- **Live URL:** finalverify.com
- **Vault context:** C:\Knowledge\dryden-vault\improved-vault\10-Products\FinalVerify\Current-Context.md

## Workflow
- Claude Code creates/edits files, commits, and pushes (typically via PR; can push directly to main for trivial changes).
- **Sensitive or large changes go through a PR** so John can review before merge. Email/secrets/auth changes have followed this pattern.
- **Tests are not run on the laptop.** Windows + no Python deps installed. Test execution happens either in CI (GitHub Actions, when configured) or directly on the droplet via SSH (see "Direct droplet access" below). The static test count is ~720 — `pytest -q` from inside the container is the authoritative gate.
- When working with Codex/GitHub, always confirm whether the Codex PR has been created and merged before requesting a local run.

## Direct droplet access
SSH key auth is configured. Password auth has been **disabled** on the droplet (Ed25519 key only).

- **SSH alias** (defined in laptop `~/.ssh/config`): `ssh finalverify`
  Resolves to `root@159.89.224.101` using `~/.ssh/id_ed25519`.
- **Repo path on droplet:** `/opt/aaa-citation-auditor` (the historical repo name; the product is FinalVerify but the directory keeps the original).
- **Running container:** `aaa-citation-auditor-aaa-app-1` (compose service name `aaa-app`).
- **Persistent data:** `aaa-data` named volume mounted at `/data` in the container; SQLite DB at `/data/aaa.db`.
- **Database migrations** run automatically on container start via `docker-entrypoint.sh` (Alembic `upgrade head`).
- **Production secrets** live in `/opt/aaa-citation-auditor/.env` on the droplet only. `.env` is gitignored. `.env.example` in the repo lists every required key.

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
- **Email:** Resend HTTPS API. App refuses to start without `RESEND_API_KEY`. Replaced an earlier smtplib path that was being silently dropped by DigitalOcean's outbound-SMTP block.
- **Citation index:** 15.5M CourtListener citations (~88% cache hit rate).
- **Hosting:** DigitalOcean droplet, Ubuntu 24.04, single Docker container.

## Key Features
- Three-tier trust architecture (authoritative / algorithmic / user-submitted)
- Provenance class split: direct, heuristic, user-confirmed, short-cite, search, CAP, local-index, supra-ref, parallel-cite
- PDF verification reports with SHA-256 fingerprinting
- Opposing Counsel Check
- ~720 passing tests

## Known Issues
- GovInfo API returns 500s on transient upstream issues; the code logs and gives up rather than retrying with backoff.
- Branding retirement is partial: templates clean, but `app/settings.py:14` (`app_name`), `app/services/exporters.py:43` (Markdown export header), README, USER_GUIDE, CHANGELOG, and `pyproject.toml` still carry "AAA". The `aaa_db/` package directory rename is a larger refactor.
- `docs/USER_GUIDE.md:268` still claims the product "runs entirely on your own computer" — contradicts the hosted reality. Landing page is correct.
- Monetization is a green field: no `User.plan` field, no Stripe, no gating.

## Conventions
- Pydantic v2: use `model_config = ConfigDict(from_attributes=True)`, not deprecated `class Config`.
- Run tests on the droplet (`docker compose exec aaa-app pytest -q`) before merging changes to core verification logic.
- Commit messages should be descriptive.
- Never commit `.env`, `.env.*`, secrets, or database files. `.env.example` documents every variable.

## Obsidian Vault Update (MANDATORY — end of every session)

File 1 — overwrite each time:
C:\Knowledge\dryden-vault\improved-vault\10-Products\FinalVerify\Current-Context.md

Sections: Last updated, Last commit, What Works Right Now, What Was Done This Session, Known Issues / Tech Debt, Next Up, Architecture Notes, Environment Setup.

File 2 — new file each session:
C:\Knowledge\dryden-vault\improved-vault\10-Products\FinalVerify\Development-Status\FinalVerify_Development_Status_[YYYYMMDD_HHMMSS].md

Sections: Session Summary (commit, duration), Changes Made, Test Results, Decisions Made, Backlog Items Discovered.

Create the Development-Status directory if it doesn't exist.
