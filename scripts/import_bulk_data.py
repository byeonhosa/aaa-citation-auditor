#!/usr/bin/env python3
"""Import CourtListener bulk CSV data into the local citation index.

Usage
-----
    python scripts/import_bulk_data.py /path/to/citations.csv
    python scripts/import_bulk_data.py /path/to/opinion-clusters.csv
    python scripts/import_bulk_data.py /path/to/citations.csv \\
        --clusters /path/to/opinion-clusters.csv
    python scripts/import_bulk_data.py /path/to/citations.csv --clear

Supported file formats
----------------------
citations.csv
    CourtListener citations bulk export.  Each row is one citation with
    columns ``cluster_id``, ``volume``, ``reporter``, ``page``.  Pass
    ``--clusters`` to enrich with case names and dates.

opinion-clusters.csv
    CourtListener opinion clusters bulk export.  Each row is one cluster
    with a ``citations`` field (JSON array or PostgreSQL array literal)
    listing all parallel citations for that cluster.

Download the files from the CourtListener S3 bucket:
    https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/list.html?prefix=bulk-data/

After downloading, run the Alembic migration if you haven't already:
    alembic upgrade head

Then run this script to populate the local index:
    python scripts/import_bulk_data.py /path/to/citations.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Ensure the project root is on sys.path so imports work when run directly.
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from aaa_db.session import SessionLocal  # noqa: E402
from app.services.local_index import (  # noqa: E402
    ImportStats,
    IncrementalImportStats,
    clear_index,
    import_from_csv,
    import_incremental,
)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=level,
        stream=sys.stderr,
    )


def _print_stats(stats: ImportStats) -> None:
    elapsed = stats.elapsed_seconds()
    rate = stats.citations_indexed / elapsed if elapsed > 0 else 0
    print()
    print("─" * 50)
    print(f"  Clusters processed : {stats.clusters_processed:>10,}")
    print(f"  Citations indexed  : {stats.citations_indexed:>10,}")
    print(f"  Citations skipped  : {stats.citations_skipped:>10,}")
    print(f"  Duplicates skipped : {stats.duplicates_skipped:>10,}")
    print(f"  Elapsed time       : {elapsed:>9.1f}s")
    print(f"  Throughput         : {rate:>9,.0f} citations/s")
    print("─" * 50)
    print()


def _print_incremental_stats(stats: IncrementalImportStats) -> None:
    elapsed = stats.elapsed_seconds()
    print()
    print("─" * 50)
    print(f"  Total processed    : {stats.total_processed:>10,}")
    print(f"  Inserted (new)     : {stats.inserted:>10,}")
    print(f"  Corrected (updated): {stats.corrected:>10,}")
    print(f"  Unchanged          : {stats.unchanged:>10,}")
    print(f"  Cache upgraded     : {stats.upgraded_to_authoritative:>10,}")
    print(f"  Elapsed time       : {elapsed:>9.1f}s")
    print("─" * 50)
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import CourtListener bulk data into the local citation index.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "csvfile",
        metavar="CSV_FILE",
        help="Path to the CourtListener citations.csv or opinion-clusters.csv file",
    )
    parser.add_argument(
        "--clusters",
        metavar="CLUSTERS_FILE",
        default=None,
        help=(
            "Path to opinion-clusters.csv to enrich citations-format rows with "
            "case names and filing dates (only used with citations.csv format)"
        ),
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        default=False,
        help="Clear the entire local citation index before importing",
    )
    parser.add_argument(
        "--mode",
        choices=["full", "update"],
        default="full",
        help=(
            "'full' (default): upsert all rows from scratch. "
            "'update': incremental update — tracks inserted/corrected/unchanged and "
            "upgrades user_submitted cache entries confirmed by bulk data."
        ),
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

    csvfile = Path(args.csvfile)
    if not csvfile.is_file():
        print(f"ERROR: file not found: {csvfile}", file=sys.stderr)
        return 1

    clusters_file: Path | None = None
    if args.clusters:
        clusters_file = Path(args.clusters)
        if not clusters_file.is_file():
            print(f"ERROR: clusters file not found: {clusters_file}", file=sys.stderr)
            return 1

    db = SessionLocal()
    try:
        if args.clear:
            logger.info("Clearing existing local citation index…")
            removed = clear_index(db)
            print(f"Cleared {removed:,} existing rows.")

        print(f"Importing from: {csvfile}")
        if clusters_file:
            print(f"Enriching with: {clusters_file}")
        print(f"Mode: {args.mode}")
        print("This may take several minutes for large files…")
        print()

        if args.mode == "update":
            stats = import_incremental(csvfile, db, case_lookup_filepath=clusters_file)
            _print_incremental_stats(stats)
            if stats.total_processed == 0:
                print("WARNING: No citations were processed. Check the file format and content.")
            else:
                print(
                    f"Done. {stats.inserted:,} new, {stats.corrected:,} corrected, "
                    f"{stats.unchanged:,} unchanged."
                )
                if stats.upgraded_to_authoritative:
                    print(
                        f"      {stats.upgraded_to_authoritative:,} cache entr(ies) upgraded to"
                        " authoritative."
                    )
        else:
            stats = import_from_csv(csvfile, db, case_lookup_filepath=clusters_file)
            _print_stats(stats)
            if stats.citations_indexed == 0:
                print("WARNING: No citations were indexed. Check the file format and content.")
            else:
                n = stats.citations_indexed
                print(f"Done. {n:,} citations are now available for fast local lookup.")

    except KeyboardInterrupt:
        print("\nImport interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.exception("Import failed: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
