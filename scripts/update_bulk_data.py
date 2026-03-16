#!/usr/bin/env python
"""Download and incrementally update the local citation index from CourtListener bulk data.

Usage
-----
    # Print usage instructions (no --url given):
    python scripts/update_bulk_data.py

    # Download from a URL and run incremental import:
    python scripts/update_bulk_data.py --url https://storage.googleapis.com/court-listener-bulk/citations.csv.gz

    # Use a local file:
    python scripts/update_bulk_data.py --url /path/to/citations.csv

Cron job example (weekly, Sunday 2 AM):
    0 2 * * 0 cd /opt/aaa-citation-auditor && python scripts/update_bulk_data.py \\
        --url https://storage.courtlistener.com/bulk-data/citations/citations-2024-01-01.csv.gz \\
        >> /var/log/aaa-update-bulk.log 2>&1

CourtListener bulk data URLs follow the pattern:
    https://storage.courtlistener.com/bulk-data/citations/citations-YYYY-MM-DD.csv.gz

Browse available snapshots at:
    https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/list.html?prefix=bulk-data/
"""

from __future__ import annotations

import argparse
import gzip
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.request import urlretrieve

# Ensure the project root is on sys.path so imports work when run directly.
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from aaa_db.session import SessionLocal  # noqa: E402
from app.services.local_index import import_incremental  # noqa: E402


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=level,
        stream=sys.stderr,
    )


def _print_usage_and_exit() -> None:
    print(__doc__)
    print(
        "ERROR: --url is required. Provide a URL or local path to the citations CSV.",
        file=sys.stderr,
    )
    sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download and incrementally update the local citation index.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--url",
        metavar="URL_OR_PATH",
        default=None,
        help="URL or local file path to the CourtListener citations CSV (optionally .gz)",
    )
    parser.add_argument(
        "--clusters",
        metavar="CLUSTERS_FILE",
        default=None,
        help="Optional path to opinion-clusters.csv for metadata enrichment",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    if not args.url:
        _print_usage_and_exit()

    url = args.url
    tmp_dir = None
    csv_path: Path

    try:
        # Determine if this is a URL or local path
        is_url = url.startswith("http://") or url.startswith("https://")

        if is_url:
            tmp_dir = tempfile.mkdtemp(prefix="aaa_bulk_")
            filename = url.split("/")[-1].split("?")[0] or "citations.csv"
            dest = Path(tmp_dir) / filename
            print(f"Downloading {url} …")
            urlretrieve(url, dest)  # noqa: S310
            logger.info("Downloaded to %s", dest)
        else:
            dest = Path(url)
            if not dest.is_file():
                print(f"ERROR: file not found: {dest}", file=sys.stderr)
                return 1

        # Decompress .gz if needed
        if dest.suffix == ".gz":
            decompressed = dest.with_suffix("")
            print(f"Decompressing {dest.name} …")
            with gzip.open(dest, "rb") as f_in, open(decompressed, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            csv_path = decompressed
        else:
            csv_path = dest

        clusters_file: Path | None = None
        if args.clusters:
            clusters_file = Path(args.clusters)
            if not clusters_file.is_file():
                print(f"ERROR: clusters file not found: {clusters_file}", file=sys.stderr)
                return 1

        print(f"Running incremental import from {csv_path} …")
        db = SessionLocal()
        try:
            stats = import_incremental(csv_path, db, case_lookup_filepath=clusters_file)
        finally:
            db.close()

        print()
        print("─" * 50)
        print(f"  Total processed    : {stats.total_processed:>10,}")
        print(f"  Inserted (new)     : {stats.inserted:>10,}")
        print(f"  Corrected (updated): {stats.corrected:>10,}")
        print(f"  Unchanged          : {stats.unchanged:>10,}")
        print(f"  Cache upgraded     : {stats.upgraded_to_authoritative:>10,}")
        print(f"  Elapsed time       : {stats.elapsed_seconds():>9.1f}s")
        print("─" * 50)
        print()

        if stats.total_processed == 0:
            print("WARNING: No citations processed. Check the file format and content.")
        else:
            print(
                f"Done. {stats.inserted:,} new, {stats.corrected:,} corrected, "
                f"{stats.unchanged:,} unchanged."
            )

    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.exception("Update failed: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
