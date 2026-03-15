"""Provenance labels for citation verification results.

Maps (verification_status, resolution_method) to a user-facing label,
plain-English description, and CSS class for colour coding.

The provenance layer sits *on top of* the existing status categories —
VERIFIED, AMBIGUOUS, etc. remain unchanged in the data model.  Provenance
adds human-readable context about *how* a citation was resolved, which is
the information lawyers actually need to assess trust.

Usage
-----
    from app.services.provenance import get_provenance, get_provenance_breakdown

    info = get_provenance("VERIFIED", "heuristic")
    # ProvenanceInfo(label="Heuristic Match", description="...", css_class="provenance-heuristic")

    breakdown = get_provenance_breakdown(citations, resolution_cache=cache)
    # [("Direct Match", 45), ("Heuristic Match", 5), ...]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProvenanceInfo:
    label: str
    description: str
    css_class: str


# ── VERIFIED resolution-method map ────────────────────────────────────────────
# Maps resolution_method → (label, description, css_class).
# "dedup" maps to "Direct Match" — the deduplication step is an internal
# implementation detail; from the lawyer's perspective the citation was found
# directly in the database.

_VERIFIED_METHOD_MAP: dict[str | None, tuple[str, str, str]] = {
    None: (
        "Direct Match",
        "Directly matched by CourtListener database.",
        "provenance-direct",
    ),
    "direct": (
        "Direct Match",
        "Directly matched by CourtListener database.",
        "provenance-direct",
    ),
    "dedup": (
        "Direct Match",
        "Directly matched by CourtListener database.",
        "provenance-direct",
    ),
    "heuristic": (
        "Heuristic Match",
        "Automatically matched using case name, court, and year found in the document.",
        "provenance-heuristic",
    ),
    "user": (
        "User Confirmed",
        "Manually selected by user from the list of candidate matches.",
        "provenance-user",
    ),
    "short_cite_match": (
        "Short Citation Match",
        "Matched to a verified full citation in this document with the same reporter and volume.",
        "provenance-short-cite",
    ),
    "search_fallback": (
        "Search Match",
        "Found via CourtListener keyword search using the case name from the document."
        " Slightly less certain than a direct database match.",
        "provenance-search",
    ),
    "cap_fallback": (
        "CAP Match",
        "Verified via Harvard Caselaw Access Project citation lookup.",
        "provenance-cap",
    ),
    "local_index": (
        "Direct Match (local)",
        "Matched from the local CourtListener citation index (bulk data).",
        "provenance-direct",
    ),
}

# ── Non-VERIFIED status map ───────────────────────────────────────────────────

_STATUS_MAP: dict[str, tuple[str, str, str]] = {
    "AMBIGUOUS": (
        "Unresolved — Multiple Matches",
        "Multiple possible cases found. Select the correct one below.",
        "provenance-ambiguous",
    ),
    "NOT_FOUND": (
        "Not Found",
        "Citation not found in available databases. It may be a recent opinion,"
        " an unpublished decision, or contain a typo.",
        "provenance-not-found",
    ),
    "DERIVED": (
        "Derived",
        "This is an Id. citation that refers back to the preceding citation.",
        "provenance-derived",
    ),
    "STATUTE_DETECTED": (
        "Statute — Not Verified",
        "Statute citation detected. Verification against a code database is"
        " not configured or not available for this jurisdiction.",
        "provenance-statute-detected",
    ),
    "STATUTE_VERIFIED": (
        "Statute — Verified",
        "Confirmed in an official statute database.",
        "provenance-statute-verified",
    ),
    "ERROR": (
        "Verification Error",
        "An error occurred during verification. The citation may still be valid;"
        " re-running the audit may resolve the issue.",
        "provenance-error",
    ),
    "UNVERIFIED_NO_TOKEN": (
        "Unverified — No API Token",
        "No CourtListener API token is configured."
        " Add a token in Settings to enable case law verification.",
        "provenance-unverified",
    ),
}

_UNKNOWN = ProvenanceInfo(
    label="Unknown",
    description="Verification status is unavailable.",
    css_class="provenance-unknown",
)


def get_provenance(
    status: str | None,
    resolution_method: str | None,
    original_method: str | None = None,
) -> ProvenanceInfo:
    """Return the :class:`ProvenanceInfo` for a citation.

    Parameters
    ----------
    status:
        The citation's ``verification_status`` (e.g. ``"VERIFIED"``).
    resolution_method:
        The citation's ``resolution_method`` (e.g. ``"direct"``, ``"cache"``).
    original_method:
        For ``resolution_method == "cache"``, the resolution method that
        originally populated the cache entry (e.g. ``"heuristic"``).  When
        provided, the provenance label is derived from the original method
        with ``" (cached)"`` appended.
    """
    if status == "VERIFIED":
        if resolution_method == "cache":
            base = original_method if (original_method and original_method != "cache") else None
            base_label, base_desc, base_class = _VERIFIED_METHOD_MAP.get(
                base, _VERIFIED_METHOD_MAP[None]
            )
            return ProvenanceInfo(
                label=f"{base_label} (cached)",
                description=f"{base_desc} Result served from local cache.",
                css_class=f"{base_class}-cached",
            )
        entry = _VERIFIED_METHOD_MAP.get(resolution_method, _VERIFIED_METHOD_MAP[None])
        label, description, css_class = entry
        return ProvenanceInfo(label=label, description=description, css_class=css_class)

    if status in _STATUS_MAP:
        label, description, css_class = _STATUS_MAP[status]
        return ProvenanceInfo(label=label, description=description, css_class=css_class)

    return _UNKNOWN


def get_provenance_breakdown(
    citations: list[Any],
    resolution_cache: dict[str, Any] | None = None,
) -> list[tuple[str, int]]:
    """Return a breakdown of provenance labels for VERIFIED citations only.

    Returns a list of ``(label, count)`` pairs, sorted by count descending,
    with zero-count labels omitted.

    Parameters
    ----------
    citations:
        Iterable of citation objects (either ``CitationResult`` or
        ``CitationResultRecord``) with ``verification_status`` and
        ``resolution_method`` attributes.
    resolution_cache:
        Optional resolution cache dict (keyed by normalized_cite) so that
        cached citations can display their original resolution method rather
        than the generic ``"cache"`` label.
    """
    counts: dict[str, int] = {}
    for citation in citations:
        status = getattr(citation, "verification_status", None)
        if status != "VERIFIED":
            continue
        method = getattr(citation, "resolution_method", None)
        original_method: str | None = None
        if method == "cache" and resolution_cache is not None:
            cache_key = getattr(citation, "normalized_text", None) or getattr(
                citation, "raw_text", ""
            )
            cached = resolution_cache.get(cache_key)
            if cached:
                original_method = cached.get("resolution_method")
        info = get_provenance("VERIFIED", method, original_method)
        counts[info.label] = counts.get(info.label, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])


# ── Human-readable help text ──────────────────────────────────────────────────
# Used by the "What do these labels mean?" section in templates.

PROVENANCE_HELP: list[tuple[str, str]] = [
    (
        "Direct Match",
        "CourtListener found an exact match in its database of published opinions."
        " Highest-confidence verification.",
    ),
    (
        "Heuristic Match",
        "The system automatically selected the best match from multiple candidates using case name,"
        " court, and year information found in the document. Review the detail to confirm the match"
        " is correct.",
    ),
    (
        "User Confirmed",
        "A user manually selected this match from a list of candidates. High confidence.",
    ),
    (
        "Short Citation Match",
        "A short-form citation (e.g., '588 U.S. at 392') was matched to a verified full citation"
        " in this same document with the same reporter and volume number.",
    ),
    (
        "Search Match",
        "The citation was not found by direct lookup, but was located via a keyword search using"
        " the case name. Slightly less certain — confirm the citation text is accurate.",
    ),
    (
        "CAP Match",
        "The citation was found in the Harvard Caselaw Access Project database as a secondary"
        " fallback. Confirm the citation text is accurate.",
    ),
    (
        "Direct Match (local)",
        "Matched from the local CourtListener citation index built from bulk data. Same"
        " confidence as a live CourtListener direct match.",
    ),
    (
        "[Any label] (cached)",
        "This result was retrieved from a local cache of previous verifications. The label shows"
        " how the citation was originally verified. Cached results are as reliable as the"
        " original verification.",
    ),
    (
        "Statute — Verified",
        "This statute citation was confirmed in an official code database"
        " (Virginia LIS or GovInfo for U.S. Code).",
    ),
    (
        "Statute — Not Verified",
        "A statute citation was detected, but no code database is configured for this jurisdiction,"
        " or verification is disabled. The citation text is shown as-is.",
    ),
    (
        "Unresolved — Multiple Matches",
        "Multiple possible cases were found. The correct one must be selected manually.",
    ),
    (
        "Not Found",
        "The citation was not found in CourtListener. It may be a recent opinion, an"
        " unpublished case, or the citation text may contain a typo.",
    ),
    (
        "Derived",
        "This is an Id. or short-form citation that refers to the immediately preceding citation.",
    ),
]
