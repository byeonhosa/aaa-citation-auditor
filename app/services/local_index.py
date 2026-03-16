"""Local citation index built from CourtListener bulk data snapshots.

Provides fast, zero-network citation lookup by maintaining a local database
table (``local_citation_index``) populated from CourtListener's quarterly
CSV bulk data exports.

Two CourtListener file formats are supported:

Format A — citations.csv
    Each row is one citation: ``cluster_id``, ``volume``, ``reporter``, ``page``.
    The citation string is constructed as ``"{volume} {reporter} {page}"``.
    To enrich with case metadata, also pass a clusters CSV (or pre-build a
    case_name/date_filed lookup dict from it).

Format B — opinion-clusters.csv
    Each row is one cluster: ``id`` (cluster_id), ``case_name``, ``date_filed``,
    ``citations`` (JSON array or PostgreSQL array of citation strings).

The importer auto-detects which format is present from the header row.

Lookup chain position
---------------------
Local index sits between the resolution cache and the CourtListener API:

    a. Resolution cache (fastest — in-memory dict)
    b. Local citation index  ← this module
    c. CourtListener API (rate-limited network call)
    d. CourtListener search fallback
    e. CAP fallback
"""

from __future__ import annotations

import csv
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aaa_db.models import CitationResolutionCache, LocalCitationIndex

logger = logging.getLogger(__name__)

# How many rows to insert per batch commit during import
_BATCH_SIZE = 10_000

# How often to log progress during import (every N clusters processed)
_LOG_EVERY = 50_000


# ── Citation string helpers ────────────────────────────────────────────────────


def _normalize_reporter(reporter: str) -> str:
    """Collapse internal whitespace in reporter strings ("U. S." → "U.S.")."""
    return re.sub(r"\s+", "", reporter).strip()


def _build_cite_string(volume: str, reporter: str, page: str) -> str | None:
    """Return "volume reporter page" or None if any component is missing."""
    v = volume.strip()
    r = _normalize_reporter(reporter)
    p = page.strip()
    if v and r and p:
        return f"{v} {r} {p}"
    return None


# ── CSV format detection ───────────────────────────────────────────────────────

# Required columns for each format (minimum set)
_CITATIONS_CSV_COLS = {"cluster_id", "volume", "reporter", "page"}
_CLUSTERS_CSV_COLS = {"id", "citations"}


def _detect_format(header: list[str]) -> str:
    """Return 'citations', 'clusters', or raise ValueError if unknown."""
    col_set = {c.lower().strip() for c in header}
    if _CITATIONS_CSV_COLS.issubset(col_set):
        return "citations"
    if _CLUSTERS_CSV_COLS.issubset(col_set):
        return "clusters"
    raise ValueError(
        f"Unrecognised CSV format. Expected columns for 'citations' format: "
        f"{sorted(_CITATIONS_CSV_COLS)} or 'clusters' format: {sorted(_CLUSTERS_CSV_COLS)}. "
        f"Got: {sorted(col_set)}"
    )


# ── PostgreSQL / JSON array parsing ───────────────────────────────────────────

_PG_ARRAY_RE = re.compile(r"^\{(.*)\}$", re.DOTALL)


def _parse_citation_array(value: str) -> list[str]:
    """Parse a citation array cell from the CourtListener clusters CSV.

    Handles:
    - JSON array:         ``["347 U.S. 483", "74 S. Ct. 686"]``
    - PostgreSQL array:   ``{"347 U.S. 483","74 S. Ct. 686"}``
                          ``{347 U.S. 483,74 S. Ct. 686}``
    - Single string:      ``347 U.S. 483``
    - Empty / NULL:       returns ``[]``
    """
    v = value.strip()
    if not v or v in ("", "\\N", "NULL", "null", "{}"):
        return []

    # JSON array
    if v.startswith("["):
        try:
            items = json.loads(v)
            return [str(i).strip() for i in items if str(i).strip()]
        except (json.JSONDecodeError, TypeError):
            logger.debug("Could not parse citation value as JSON: %r — trying other formats", v)

    # PostgreSQL array literal: {item1,"item two",...}
    m = _PG_ARRAY_RE.match(v)
    if m:
        inner = m.group(1)
        # Use csv reader to handle quoted items properly
        try:
            row = next(csv.reader([inner]))
            return [item.strip() for item in row if item.strip()]
        except StopIteration:
            return []

    # Fallback: treat as single citation string
    return [v]


# ── Row parsers ───────────────────────────────────────────────────────────────


def _parse_citations_row(
    row: dict[str, str],
    case_lookup: dict[int, dict[str, str]],
) -> LocalCitationIndex | None:
    """Parse one row from citations.csv format."""
    try:
        cluster_id = int(row.get("cluster_id", ""))
    except (ValueError, TypeError):
        return None

    cite = _build_cite_string(
        row.get("volume", ""),
        row.get("reporter", ""),
        row.get("page", ""),
    )
    if not cite:
        return None

    meta = case_lookup.get(cluster_id, {})
    reporter_raw = _normalize_reporter(row.get("reporter", ""))
    volume_raw = row.get("volume", "").strip()
    volume_int: int | None = None
    try:
        volume_int = int(volume_raw)
    except (ValueError, TypeError):
        volume_int = None

    return LocalCitationIndex(
        normalized_cite=cite,
        cluster_id=cluster_id,
        case_name=meta.get("case_name"),
        court_id=meta.get("court_id"),
        date_filed=meta.get("date_filed"),
        reporter=reporter_raw,
        volume=volume_int,
        page=row.get("page", "").strip() or None,
        source="courtlistener_bulk",
    )


def _parse_clusters_row(row: dict[str, str]) -> list[LocalCitationIndex]:
    """Parse one row from opinion-clusters.csv format, returning one entry per citation."""
    try:
        cluster_id = int(row.get("id", ""))
    except (ValueError, TypeError):
        return []

    citations_raw = row.get("citations", "")
    cites = _parse_citation_array(citations_raw)
    if not cites:
        return []

    case_name = (row.get("case_name") or row.get("case_name_full") or "").strip() or None
    date_filed = (row.get("date_filed") or "").strip()[:10] or None
    # court_id is often in the related docket, not in clusters CSV directly
    court_id = (row.get("docket__court_id") or row.get("court_id") or "").strip() or None

    entries: list[LocalCitationIndex] = []
    for cite in cites:
        # Try to extract volume/reporter/page from the cite string for storage
        vol, rep, pg = None, None, None
        parts = cite.split()
        if len(parts) >= 3:
            try:
                vol = int(parts[0])
            except (ValueError, TypeError):
                vol = None
            rep = _normalize_reporter(parts[1]) if len(parts) > 1 else None
            pg = parts[-1] if len(parts) > 1 else None

        entries.append(
            LocalCitationIndex(
                normalized_cite=cite,
                cluster_id=cluster_id,
                case_name=case_name,
                court_id=court_id,
                date_filed=date_filed,
                reporter=rep,
                volume=vol,
                page=pg,
                source="courtlistener_bulk",
            )
        )
    return entries


# ── Import function ────────────────────────────────────────────────────────────


class ImportStats:
    clusters_processed: int = 0
    citations_indexed: int = 0
    citations_skipped: int = 0
    duplicates_skipped: int = 0
    started_at: float = 0.0

    def elapsed_seconds(self) -> float:
        return time.perf_counter() - self.started_at


@dataclass
class IncrementalImportStats:
    """Statistics for an incremental (update-mode) import."""

    inserted: int = 0
    corrected: int = 0
    unchanged: int = 0
    total_processed: int = 0
    upgraded_to_authoritative: int = 0
    elapsed_start: float = field(default_factory=time.perf_counter)

    def elapsed_seconds(self) -> float:
        return time.perf_counter() - self.elapsed_start


def import_from_csv(
    filepath: str | Path,
    db: Session,
    *,
    case_lookup_filepath: str | Path | None = None,
) -> ImportStats:
    """Import citations from a CourtListener bulk CSV into the local index.

    Supports both ``citations.csv`` (Format A) and ``opinion-clusters.csv``
    (Format B) — the format is auto-detected from the header row.

    Parameters
    ----------
    filepath:
        Path to the CourtListener CSV file to import.
    db:
        SQLAlchemy session.  The caller is responsible for closing it.
    case_lookup_filepath:
        Optional path to an ``opinion-clusters.csv`` file used to enrich
        citations-format rows with case_name / date_filed / court_id.
        Ignored for clusters-format files (which already contain case data).

    Returns
    -------
    ImportStats with counters and timing.
    """
    filepath = Path(filepath)
    stats = ImportStats()
    stats.started_at = time.perf_counter()

    logger.info("Starting import from %s", filepath)

    # Pre-load case metadata if a lookup file was given
    case_lookup: dict[int, dict[str, str]] = {}
    if case_lookup_filepath:
        case_lookup = _load_case_lookup(Path(case_lookup_filepath))

    batch: list[LocalCitationIndex] = []

    # Track already-seen normalized_cite values in this import run
    # to avoid hitting the DB unique constraint on the same batch
    seen_in_batch: set[str] = set()

    with open(filepath, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"CSV file has no header row: {filepath}")

        fmt = _detect_format(list(reader.fieldnames))
        logger.info("Detected format: %r from %s", fmt, filepath.name)

        for row_num, row in enumerate(reader, start=2):  # start=2 because row 1 is header
            try:
                if fmt == "citations":
                    entry = _parse_citations_row(row, case_lookup)
                    entries = [entry] if entry is not None else []
                else:
                    entries = _parse_clusters_row(row)
            except Exception as exc:
                logger.debug("Row %d: skipping malformed row (%s): %s", row_num, exc, row)
                stats.citations_skipped += 1
                continue

            stats.clusters_processed += 1

            for entry in entries:
                if entry.normalized_cite in seen_in_batch:
                    stats.duplicates_skipped += 1
                    continue
                seen_in_batch.add(entry.normalized_cite)
                batch.append(entry)
                stats.citations_indexed += 1

            if len(batch) >= _BATCH_SIZE:
                _flush_batch(db, batch)
                batch.clear()
                seen_in_batch.clear()

            if stats.clusters_processed % _LOG_EVERY == 0:
                elapsed = stats.elapsed_seconds()
                logger.info(
                    "Import progress: %d clusters, %d citations indexed (%.1fs)",
                    stats.clusters_processed,
                    stats.citations_indexed,
                    elapsed,
                )

    if batch:
        _flush_batch(db, batch)

    elapsed = stats.elapsed_seconds()
    logger.info(
        "Import complete: %d clusters, %d citations indexed, %d skipped, "
        "%d duplicates, %.1fs total",
        stats.clusters_processed,
        stats.citations_indexed,
        stats.citations_skipped,
        stats.duplicates_skipped,
        elapsed,
    )
    return stats


def import_incremental(
    filepath: str | Path,
    db: Session,
    *,
    case_lookup_filepath: str | Path | None = None,
) -> IncrementalImportStats:
    """Incrementally update the local citation index from a CourtListener bulk CSV.

    Unlike ``import_from_csv`` (which upserts every row unconditionally), this
    function distinguishes between:

    - **inserted** – new citations not previously in the index
    - **corrected** – citations whose cluster_id changed (data correction)
    - **unchanged** – citations whose cluster_id already matches

    After processing all rows it also checks whether any ``user_submitted``
    cache entries can be upgraded to ``authoritative`` because the bulk data
    confirms the same cluster_id.

    Parameters
    ----------
    filepath:
        Path to the CourtListener CSV file.
    db:
        SQLAlchemy session.  The caller is responsible for closing it.
    case_lookup_filepath:
        Optional path to ``opinion-clusters.csv`` for metadata enrichment.

    Returns
    -------
    IncrementalImportStats with counters and timing.
    """
    filepath = Path(filepath)
    stats = IncrementalImportStats()

    logger.info("Starting incremental import from %s", filepath)

    case_lookup: dict[int, dict[str, str]] = {}
    if case_lookup_filepath:
        case_lookup = _load_case_lookup(Path(case_lookup_filepath))

    # Track already-seen normalized_cite values in this run
    seen_in_run: set[str] = set()

    batch_entries: list[LocalCitationIndex] = []

    def _process_entry(entry: LocalCitationIndex) -> None:
        if entry.normalized_cite in seen_in_run:
            return
        seen_in_run.add(entry.normalized_cite)

        existing = db.scalar(
            select(LocalCitationIndex).where(
                LocalCitationIndex.normalized_cite == entry.normalized_cite
            )
        )
        if existing is None:
            db.add(entry)
            stats.inserted += 1
        elif existing.cluster_id != entry.cluster_id:
            existing.cluster_id = entry.cluster_id
            existing.case_name = entry.case_name
            existing.court_id = entry.court_id
            existing.date_filed = entry.date_filed
            existing.reporter = entry.reporter
            existing.volume = entry.volume
            existing.page = entry.page
            existing.source = entry.source
            existing.imported_at = datetime.now(timezone.utc)
            stats.corrected += 1
        else:
            stats.unchanged += 1
        stats.total_processed += 1

    with open(filepath, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"CSV file has no header row: {filepath}")

        fmt = _detect_format(list(reader.fieldnames))
        logger.info("Detected format: %r from %s", fmt, filepath.name)

        for row_num, row in enumerate(reader, start=2):
            try:
                if fmt == "citations":
                    entry = _parse_citations_row(row, case_lookup)
                    entries = [entry] if entry is not None else []
                else:
                    entries = _parse_clusters_row(row)
            except Exception as exc:
                logger.debug("Row %d: skipping malformed row (%s): %s", row_num, exc, row)
                continue

            for entry in entries:
                batch_entries.append(entry)

            if len(batch_entries) >= _BATCH_SIZE:
                for e in batch_entries:
                    _process_entry(e)
                db.commit()
                batch_entries.clear()

            if stats.total_processed % _LOG_EVERY == 0 and stats.total_processed > 0:
                elapsed = stats.elapsed_seconds()
                logger.info(
                    "Incremental import progress: %d processed (%.1fs)",
                    stats.total_processed,
                    elapsed,
                )

    for e in batch_entries:
        _process_entry(e)
    if batch_entries:
        db.commit()

    # Upgrade user_submitted cache entries confirmed by bulk data
    user_entries = list(
        db.scalars(
            select(CitationResolutionCache).where(
                CitationResolutionCache.trust_tier == "user_submitted"
            )
        ).all()
    )
    upgraded = 0
    for cache_entry in user_entries:
        bulk = db.scalar(
            select(LocalCitationIndex).where(
                LocalCitationIndex.normalized_cite == cache_entry.normalized_cite,
                LocalCitationIndex.cluster_id == cache_entry.selected_cluster_id,
            )
        )
        if bulk is not None:
            cache_entry.trust_tier = "authoritative"
            upgraded += 1
            logger.info(
                "Upgraded cache entry %r to authoritative via bulk data",
                cache_entry.normalized_cite,
            )
    if upgraded:
        db.commit()
    stats.upgraded_to_authoritative = upgraded

    elapsed = stats.elapsed_seconds()
    logger.info(
        "Incremental import complete: %d inserted, %d corrected, %d unchanged, "
        "%d cache entries upgraded, %.1fs total",
        stats.inserted,
        stats.corrected,
        stats.unchanged,
        upgraded,
        elapsed,
    )
    return stats


def _load_case_lookup(filepath: Path) -> dict[int, dict[str, str]]:
    """Load id→{case_name, date_filed, court_id} from a clusters CSV."""
    lookup: dict[int, dict[str, str]] = {}
    logger.info("Loading case metadata from %s …", filepath)
    with open(filepath, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                cid = int(row.get("id", ""))
            except (ValueError, TypeError):
                continue
            lookup[cid] = {
                "case_name": (row.get("case_name") or "").strip(),
                "date_filed": (row.get("date_filed") or "").strip()[:10],
                "court_id": (row.get("docket__court_id") or row.get("court_id") or "").strip(),
            }
    logger.info("Loaded %d case metadata rows", len(lookup))
    return lookup


def _flush_batch(db: Session, batch: list[LocalCitationIndex]) -> None:
    """Upsert a batch of LocalCitationIndex rows (insert-or-update on conflict)."""
    if not batch:
        return
    for entry in batch:
        existing = db.scalar(
            select(LocalCitationIndex).where(
                LocalCitationIndex.normalized_cite == entry.normalized_cite
            )
        )
        if existing is None:
            db.add(entry)
        else:
            # Update metadata in place (keep the existing row's id)
            existing.cluster_id = entry.cluster_id
            existing.case_name = entry.case_name
            existing.court_id = entry.court_id
            existing.date_filed = entry.date_filed
            existing.reporter = entry.reporter
            existing.volume = entry.volume
            existing.page = entry.page
            existing.source = entry.source
            existing.imported_at = datetime.now(timezone.utc)
    db.commit()


def clear_index(db: Session) -> int:
    """Delete all rows from local_citation_index. Returns the count removed."""
    count = db.query(LocalCitationIndex).count()
    db.execute(text("DELETE FROM local_citation_index"))
    db.commit()
    logger.info("Local citation index cleared: %d rows removed", count)
    return count


# ── Lookup helpers ─────────────────────────────────────────────────────────────


def get_stats(db: Session) -> dict[str, Any]:
    """Return statistics about the local citation index.

    Returns a dict with:
        total       – total number of indexed citations
        last_import – ISO datetime string of the most recent import, or None
    """
    total = db.query(LocalCitationIndex).count()
    last_row = db.scalar(
        select(LocalCitationIndex.imported_at)
        .order_by(LocalCitationIndex.imported_at.desc())
        .limit(1)
    )
    last_import: str | None = None
    if last_row is not None:
        last_import = last_row.isoformat() if hasattr(last_row, "isoformat") else str(last_row)
    return {"total": total, "last_import": last_import}


class LocalIndexLookup:
    """Wraps a SQLAlchemy session to provide fast citation lookups.

    Instantiate once per request with the active DB session, then pass the
    instance to ``verify_citations()`` as the ``local_index`` parameter.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def lookup(self, normalized_cite: str) -> dict[str, Any] | None:
        """Return a match dict or None if the citation is not in the index.

        The returned dict has keys: cluster_id, case_name, court_id, date_filed.
        """
        row = self._db.scalar(
            select(LocalCitationIndex).where(LocalCitationIndex.normalized_cite == normalized_cite)
        )
        if row is None:
            return None
        return {
            "cluster_id": row.cluster_id,
            "case_name": row.case_name,
            "court_id": row.court_id,
            "date_filed": row.date_filed,
        }

    def lookup_batch(self, cites: list[str]) -> dict[str, dict[str, Any]]:
        """Return a dict mapping each found cite → match dict.

        Citations not found in the index are absent from the result.
        """
        if not cites:
            return {}
        rows = self._db.scalars(
            select(LocalCitationIndex).where(LocalCitationIndex.normalized_cite.in_(cites))
        ).all()
        return {
            row.normalized_cite: {
                "cluster_id": row.cluster_id,
                "case_name": row.case_name,
                "court_id": row.court_id,
                "date_filed": row.date_filed,
            }
            for row in rows
        }

    def is_populated(self) -> bool:
        """Return True if the index contains at least one row."""
        return self._db.query(LocalCitationIndex).limit(1).count() > 0
