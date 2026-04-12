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
- Claude Code creates/edits files, commits, and pushes directly to main.
- John pulls on the droplet and tests there. Do NOT run servers, tests, or database commands locally.
- When working with Codex/GitHub, always confirm whether the Codex PR has been created and merged before requesting a local run.

## Tech Stack
- Backend: FastAPI (Python)
- Database: PostgreSQL
- Auth: JWT user accounts
- AI: Ollama llama3.2 (server-side, free AI memos)
- Citation index: 15.5M CourtListener citations (88% cache hit rate)
- Hosting: DigitalOcean droplet, Ubuntu 24.04

## Key Features
- Three-tier trust architecture
- PDF verification reports with SHA-256 fingerprinting
- Opposing Counsel Check
- ~700 passing tests

## Known Issues
- GovInfo API returning 500 errors (federal statutes detected but unverified)
- DigitalOcean blocking SMTP from Docker containers

## Conventions
- Pydantic v2: use model_config = ConfigDict(from_attributes=True), not deprecated class Config.
- Run tests before committing changes to core verification logic.
- Commit messages should be descriptive.

## Obsidian Vault Update (MANDATORY — end of every session)

File 1 — overwrite each time:
C:\Knowledge\dryden-vault\improved-vault\10-Products\FinalVerify\Current-Context.md

Sections: Last updated, Last commit, What Works Right Now, What Was Done This Session, Known Issues / Tech Debt, Next Up, Architecture Notes, Environment Setup.

File 2 — new file each session:
C:\Knowledge\dryden-vault\improved-vault\10-Products\FinalVerify\Development-Status\FinalVerify_Development_Status_[YYYYMMDD_HHMMSS].md

Sections: Session Summary (commit, duration), Changes Made, Test Results, Decisions Made, Backlog Items Discovered.

Create the Development-Status directory if it doesn't exist.
