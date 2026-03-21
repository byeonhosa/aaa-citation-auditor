#!/usr/bin/env python
"""Re-verify user-submitted cache entries against CourtListener.

Usage:
    python scripts/reverify_user_resolutions.py [--days N] [--dry-run]

Options:
    --days N     Only re-verify entries older than N days (default: 7)
    --dry-run    Show what would be re-verified without making API calls
    --limit N    Maximum entries to process (default: 500)

Rate limit: max 30 requests per minute (CourtListener limit).
Suitable for running as a weekly or monthly cron job.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aaa_db.models import CitationResolutionCache  # noqa: E402
from aaa_db.session import SessionLocal  # noqa: E402
from app.services.reverification import (  # noqa: E402
    find_reverifiable_citations,
    reverify_citation,
)
from app.settings import settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-verify user-submitted citation resolutions")
    parser.add_argument("--days", type=int, default=7, help="Min days since last reverification")
    parser.add_argument("--dry-run", action="store_true", help="Print without making API calls")
    parser.add_argument("--limit", type=int, default=500, help="Max entries to process")
    args = parser.parse_args()

    with SessionLocal() as db:
        entries = find_reverifiable_citations(db, days_threshold=args.days)

    total = len(entries)
    if total == 0:
        print("No entries need re-verification.")
        return

    entries = entries[: args.limit]
    print(f"Found {total} entries needing re-verification, processing {len(entries)}.")

    if args.dry_run:
        for e in entries:
            print(f"  Would reverify: {e.normalized_cite!r} (cluster {e.selected_cluster_id})")
        return

    token = settings.courtlistener_token
    counts: dict[str, int] = {
        "confirmed": 0,
        "disputed": 0,
        "ambiguous": 0,
        "not_found": 0,
        "error": 0,
    }

    RATE_LIMIT = 30  # requests per minute
    interval = 60.0 / RATE_LIMIT

    # Collect IDs first (entries were loaded in a closed session above)
    entry_ids = [e.id for e in entries]

    with SessionLocal() as db:
        from sqlalchemy import select  # noqa: PLC0415

        for entry_id in entry_ids:
            t0 = time.monotonic()
            fresh = db.scalar(
                select(CitationResolutionCache).where(CitationResolutionCache.id == entry_id)
            )
            if fresh is None:
                continue
            outcome = reverify_citation(db, fresh, courtlistener_token=token)
            counts[outcome] = counts.get(outcome, 0) + 1
            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, interval - elapsed)
            if sleep_for > 0:
                time.sleep(sleep_for)

    print(
        f"Done. Confirmed: {counts['confirmed']}, "
        f"Disputed: {counts['disputed']}, "
        f"Ambiguous: {counts['ambiguous']}, "
        f"Not found: {counts['not_found']}, "
        f"Errors: {counts['error']}"
    )


if __name__ == "__main__":
    main()
