#!/bin/bash
set -euo pipefail

echo "[entrypoint] Running database migrations…"
alembic upgrade head

echo "[entrypoint] Starting AAA Citation Auditor on port 8000…"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
