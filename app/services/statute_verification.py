"""Statute verification against public APIs.

Virginia Code — Virginia LIS public API
========================================
API reference: https://law.lis.virginia.gov/developers/

Endpoint used:
    GET https://law.lis.virginia.gov/api/CoVSectionsGetSectionDetailsJson/{sectionNumber}

Response behaviour:
    - Always returns HTTP 200 (even for missing sections)
    - ``ChapterList`` is non-empty → section exists
    - ``ChapterList`` is empty / null → section does not exist in the Code

No authentication required.  The API is open and free to use.

Federal U.S. Code — GovInfo API
=================================
API reference: https://api.govinfo.gov/docs/

Endpoint used:
    GET https://api.govinfo.gov/search
    Query: collection:USCODE title:{title} section:{section}

Response behaviour:
    - HTTP 200 with JSON body; ``count`` > 0 → section found
    - HTTP 200 with ``count`` == 0 → section not found
    - HTTP 403 / 429 → key issue or rate limit → treat as transient error

Requires a free API key from api.data.gov.  Without a key the section is
left as STATUTE_DETECTED rather than producing an error.
"""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)

_VA_BASE_URL = "https://law.lis.virginia.gov/api/CoVSectionsGetSectionDetailsJson/"
_DEFAULT_TIMEOUT = 10  # seconds

# ---------------------------------------------------------------------------
# Citation parsing
# ---------------------------------------------------------------------------

# Matches Virginia Code references in legal text.  Captures the bare section
# number (e.g. "15.2-3400") as group 1.
#
# Accepted prefixes (case-insensitive):
#   Va. Code §                 Va. Code Ann. §
#   Code of Virginia §         Code of Virginia, 1950, as amended, §
#   Virginia Code §            Code of Va. §
#
# Section number format:
#   TITLE-SECTION where TITLE is like "15.2", "18.2", "1", "46.2"
#   and SECTION is digits with optional .N or :N sub-section suffixes.

_VA_CITATION_RE = re.compile(
    r"""
    (?:
        (?:Va\.?\s+|Virginia\s+)Code(?:\.?\s+Ann\.?)?   # Va. Code [Ann.] | Virginia Code
        | Code\s+of\s+(?:Va\.?|Virginia)                # Code of Virginia | Code of Va.
          (?:[^§\n]{0,50})?                              # optional year / parenthetical
    )
    \s*,?\s*(?:§|Sec\.|Section)\s*                       # § symbol or Sec. or Section
    (                                                    # group 1: bare section number
        \d+(?:\.\d+)?                                    # title: 1 | 15.2 | 46.2
        [-\u2013]                                        # hyphen or en-dash
        \d[\dA-Z]*                                       # section start
        (?:[.:\-]\d[\dA-Z]*)*                            # optional .N or :N or -N parts
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Virginia Code section numbers use X.Y-ZZZZ format.  This regex validates
# that the extracted group actually looks like a real section number.
_SECTION_RE = re.compile(r"^\d+(?:\.\d+)?-\d")


def parse_virginia_section(raw_text: str) -> str | None:
    """Extract the bare section number from a Virginia Code citation.

    Returns a normalized section number such as ``"15.2-3400"``, or ``None``
    if the text does not clearly reference the Virginia Code.

    Only citations with an explicit Virginia Code prefix (e.g. "Va. Code §",
    "Code of Virginia §") are matched — bare "§ X-Y" patterns are ignored to
    avoid false positives from non-Virginia statutes.
    """
    m = _VA_CITATION_RE.search(raw_text)
    if not m:
        return None
    # Normalise en-dash → ASCII hyphen
    section = m.group(1).replace("\u2013", "-")
    if not _SECTION_RE.match(section):
        return None
    return section


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


def verify_virginia_section(
    section_number: str,
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    _client: httpx.Client | None = None,
) -> tuple[str, str | None]:
    """Query the Virginia LIS API and return *(status, section_title)*.

    status is one of:

    ``"STATUTE_VERIFIED"``
        The section was confirmed to exist in the Code of Virginia.
    ``"STATUTE_NOT_FOUND"``
        The API returned an empty ``ChapterList`` — section not in the Code.
    ``"STATUTE_ERROR"``
        A network or parse error occurred; the caller should keep the existing
        ``STATUTE_DETECTED`` status rather than downgrading.

    section_title is the human-readable heading from the API (e.g. ``"Voluntary
    settlements among local governments"``), or ``None`` when unavailable.
    """
    url = _VA_BASE_URL + section_number + "/"
    try:
        if _client is not None:
            response = _client.get(url)
        else:
            with httpx.Client(timeout=timeout_seconds) as c:
                response = c.get(url)

        if response.status_code != 200:
            logger.warning(
                "Virginia LIS API returned HTTP %d for section %s",
                response.status_code,
                section_number,
            )
            return "STATUTE_ERROR", None

        data = response.json()
        chapter_list = data.get("ChapterList") or []
        if not chapter_list:
            logger.debug("Virginia LIS: section %s not found (empty ChapterList)", section_number)
            return "STATUTE_NOT_FOUND", None

        section_title: str | None = chapter_list[0].get("SectionTitle") or None
        logger.debug("Virginia LIS: section %s verified — %r", section_number, section_title)
        return "STATUTE_VERIFIED", section_title

    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        logger.warning("Virginia LIS API unreachable for %s: %s", section_number, exc)
        return "STATUTE_ERROR", None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Virginia LIS API unexpected error for %s: %s", section_number, exc)
        return "STATUTE_ERROR", None


class VirginiaStatuteVerifier:
    """Thin wrapper around :func:`verify_virginia_section` for dependency injection."""

    def __init__(self, timeout_seconds: int = _DEFAULT_TIMEOUT) -> None:
        self.timeout_seconds = timeout_seconds

    def verify(self, section_number: str) -> tuple[str, str | None]:
        return verify_virginia_section(section_number, timeout_seconds=self.timeout_seconds)


# ---------------------------------------------------------------------------
# Federal U.S. Code verification (GovInfo API)
# ---------------------------------------------------------------------------

_GOVINFO_SEARCH_URL = "https://api.govinfo.gov/search"
_GOVINFO_DEFAULT_TIMEOUT = 15  # seconds

# Matches U.S. Code citations in multiple formats.
#
# Accepted forms:
#   "42 U.S.C. § 1983"                          (standard dotted)
#   "28 U.S.C. § 1331"                          (standard dotted)
#   "42 U.S.C. Section 1983"                    (Section keyword)
#   "42 U.S.C. Sec. 1983"                       (Sec. keyword)
#   "42 USC § 1983"                             (no periods)
#   "Title 42, United States Code, § 1983"      (verbose, § symbol)
#   "Title 42, United States Code, Section 1983" (verbose, keyword)
#
# Captured groups:
#   group 1 — title number (standard/no-period forms)
#   group 2 — title number (verbose form)
#   group 3 — raw section number (may include subsection indicators)
_USC_CITATION_RE = re.compile(
    r"""
    (?:
        (?:Title\s+)?                          # optional "Title " prefix
        (\d+)                                  # group 1: title number (standard)
        \s*,?\s*
        (?:U\.S\.C\.?|USC)                     # "U.S.C." / "U.S.C" / "USC"
        (?:\s+Ann\.)?                          # optional " Ann."
        |
        Title\s+(\d+)\s*,?\s*                  # group 2: title (verbose form)
        United\s+States\s+Code\s*,?\s*
    )
    [\s,]*                                     # optional whitespace/comma between prefix and §
    (?:§|Sec\.|Section)\s*                     # section indicator
    (                                          # group 3: raw section number
        \d[\dA-Za-z]*                          # e.g. 1983, 1234a
        (?:\([^)]*\))?                         # optional parenthetical sub: (a)
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Validates that group 3 looks like a real section number (starts with a digit).
_USC_SECTION_RE = re.compile(r"^\d")


def parse_federal_section(raw_text: str) -> tuple[str, str] | None:
    """Extract *(title, section)* from a U.S. Code citation.

    Returns a tuple such as ``("42", "1983")``, or ``None`` if the text does
    not clearly reference the United States Code.

    Subsection indicators (e.g. the ``(a)`` in ``§ 1983(a)``) are stripped —
    the API lookup targets the base section.  Letter-suffixed sections such as
    ``§ 1234a`` are kept intact, as those are distinct real sections.
    """
    m = _USC_CITATION_RE.search(raw_text)
    if not m:
        return None
    title = (m.group(1) or m.group(2) or "").strip()
    section_raw = (m.group(3) or "").strip()
    if not title or not section_raw:
        return None
    if not _USC_SECTION_RE.match(section_raw):
        return None
    # Strip trailing parenthetical subsection: "1983(a)" → "1983"
    section = re.sub(r"\(.*", "", section_raw).strip()
    return title, section


def verify_federal_section(
    title: str,
    section: str,
    *,
    api_key: str,
    timeout_seconds: int = _GOVINFO_DEFAULT_TIMEOUT,
    _client: httpx.Client | None = None,
) -> tuple[str, str | None]:
    """Query the GovInfo search API and return *(status, section_title)*.

    status is one of:

    ``"STATUTE_VERIFIED"``
        The section was confirmed to exist in the United States Code.
    ``"STATUTE_NOT_FOUND"``
        The search returned zero results — section not in the U.S. Code.
    ``"STATUTE_ERROR"``
        A network, rate-limit, or parse error occurred; caller should keep
        the existing ``STATUTE_DETECTED`` status rather than downgrading.

    section_title is the human-readable heading from GovInfo, or ``None``.
    """
    params = {
        "query": f"collection:USCODE title:{title} section:{section}",
        "pageSize": "1",
        "offsetMark": "*",
        "api_key": api_key,
    }
    try:
        if _client is not None:
            response = _client.get(_GOVINFO_SEARCH_URL, params=params)
        else:
            with httpx.Client(timeout=timeout_seconds) as c:
                response = c.get(_GOVINFO_SEARCH_URL, params=params)

        if response.status_code == 429:
            logger.warning("GovInfo API rate limit hit for %s U.S.C. § %s", title, section)
            return "STATUTE_ERROR", None

        if response.status_code == 403:
            logger.warning(
                "GovInfo API returned 403 for %s U.S.C. § %s — check API key", title, section
            )
            return "STATUTE_ERROR", None

        if response.status_code != 200:
            logger.warning(
                "GovInfo API returned HTTP %d for %s U.S.C. § %s",
                response.status_code,
                title,
                section,
            )
            return "STATUTE_ERROR", None

        data = response.json()
        count = data.get("count", 0)
        if not count:
            logger.debug("GovInfo: %s U.S.C. § %s not found (count=0)", title, section)
            return "STATUTE_NOT_FOUND", None

        # Try to extract a human-readable title from the first result
        results = data.get("results") or []
        section_title: str | None = None
        if results and isinstance(results[0], dict):
            section_title = results[0].get("title") or None
        logger.debug("GovInfo: %s U.S.C. § %s verified — %r", title, section, section_title)
        return "STATUTE_VERIFIED", section_title

    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        logger.warning("GovInfo API unreachable for %s U.S.C. § %s: %s", title, section, exc)
        return "STATUTE_ERROR", None
    except Exception as exc:  # noqa: BLE001
        logger.warning("GovInfo API unexpected error for %s U.S.C. § %s: %s", title, section, exc)
        return "STATUTE_ERROR", None


class FederalStatuteVerifier:
    """Thin wrapper around :func:`verify_federal_section` for dependency injection."""

    def __init__(
        self,
        api_key: str,
        timeout_seconds: int = _GOVINFO_DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def verify(self, title: str, section: str) -> tuple[str, str | None]:
        return verify_federal_section(
            title,
            section,
            api_key=self.api_key,
            timeout_seconds=self.timeout_seconds,
        )
