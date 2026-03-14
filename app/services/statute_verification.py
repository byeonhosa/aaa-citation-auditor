"""Virginia Code statute verification via the Virginia LIS public API.

API reference: https://law.lis.virginia.gov/developers/

Endpoint used:
    GET https://law.lis.virginia.gov/api/CoVSectionsGetSectionDetailsJson/{sectionNumber}

Response behaviour:
    - Always returns HTTP 200 (even for missing sections)
    - ``ChapterList`` is non-empty → section exists
    - ``ChapterList`` is empty / null → section does not exist in the Code

No authentication is required.  The API is open and free to use.
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
    url = _VA_BASE_URL + section_number
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
