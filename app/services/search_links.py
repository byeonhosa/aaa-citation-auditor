"""Generate pre-populated search URLs for NOT_FOUND citations.

For citations that cannot be found in CourtListener, we surface links to
external search engines so users can verify the citation manually.

Supported destinations
----------------------
courtlistener  https://www.courtlistener.com/?q=...&type=o
google_scholar https://scholar.google.com/scholar?q=...
"""

from __future__ import annotations

import urllib.parse


def build_search_links(
    raw_text: str,
    case_name: str | None = None,
) -> dict[str, str]:
    """Return a dict of labelled search URLs for a NOT_FOUND citation.

    Parameters
    ----------
    raw_text:
        The raw citation text (e.g. "Smith v. Jones, 123 F.3d 456 (9th Cir. 2001)").
    case_name:
        The extracted "Party v. Party" case name, if available.  Used as the
        primary query term on CourtListener (more precise than raw_text).

    Returns
    -------
    dict with keys "courtlistener" and "google_scholar", each a URL string.
    """
    cl_query = case_name or raw_text
    # Google Scholar benefits from the full citation text for precision
    gs_query = f"{case_name} {raw_text}" if case_name else raw_text

    return {
        "courtlistener": "https://www.courtlistener.com/?"
        + urllib.parse.urlencode({"q": cl_query, "type": "o"}),
        "google_scholar": "https://scholar.google.com/scholar?"
        + urllib.parse.urlencode({"q": gs_query}),
    }
