"""Re-verification service for user-submitted cache entries."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aaa_db.models import CitationResolutionCache

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_REVERIFY_URL = "https://www.courtlistener.com/api/rest/v4/citation-lookup/"


def find_reverifiable_citations(
    db: Session, days_threshold: int = 7
) -> list[CitationResolutionCache]:
    """Return user_submitted cache entries that need re-verification.

    Entries are eligible if they have never been reverified or were last
    reverified more than ``days_threshold`` days ago.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_threshold)
    return list(
        db.scalars(
            select(CitationResolutionCache).where(
                CitationResolutionCache.trust_tier == "user_submitted",
                CitationResolutionCache.disputed.is_(False),
                or_(
                    CitationResolutionCache.last_reverified_at.is_(None),
                    CitationResolutionCache.last_reverified_at < cutoff,
                ),
            )
        ).all()
    )


def reverify_citation(
    db: Session,
    entry: CitationResolutionCache,
    *,
    courtlistener_token: str | None = None,
    timeout: int = 30,
    _client: httpx.Client | None = None,
) -> str:
    """Re-verify a user_submitted cache entry against CourtListener.

    Returns one of:
    - ``"confirmed"``  – CourtListener agrees with the user's cluster choice;
                         entry upgraded to ``authoritative``.
    - ``"disputed"``   – CourtListener returns a different single result or
                         the user's choice is absent from multiple results;
                         entry flagged as ``disputed``.
    - ``"ambiguous"``  – Multiple results returned and the user's choice is
                         among them; no change.
    - ``"not_found"``  – No results returned by CourtListener.
    - ``"error"``      – Network or API error; no change to entry.
    """
    headers: dict[str, str] = {}
    if courtlistener_token:
        headers["Authorization"] = f"Token {courtlistener_token}"

    params = {"citation": entry.normalized_cite}
    try:
        if _client is not None:
            resp = _client.get(_REVERIFY_URL, params=params, headers=headers)
        else:
            with httpx.Client(timeout=timeout) as c:
                resp = c.get(_REVERIFY_URL, params=params, headers=headers)

        if resp.status_code == 401:
            logger.warning(
                "CourtListener auth failed for reverification of %r", entry.normalized_cite
            )
            return "error"
        if resp.status_code != 200:
            logger.warning(
                "CourtListener returned %d for reverification of %r",
                resp.status_code,
                entry.normalized_cite,
            )
            return "error"

        data = resp.json()
        clusters = data if isinstance(data, list) else data.get("results", [])

        entry.last_reverified_at = datetime.now(timezone.utc)

        if not clusters:
            db.commit()
            return "not_found"

        if len(clusters) == 1:
            found_cluster_id = clusters[0].get("id") or clusters[0].get("cluster_id")
            if found_cluster_id == entry.selected_cluster_id:
                entry.trust_tier = "authoritative"
                entry.disputed = False
                logger.info("Reverification confirmed %r as authoritative", entry.normalized_cite)
                db.commit()
                return "confirmed"
            else:
                entry.disputed = True
                logger.warning(
                    "Reverification dispute for %r: user picked %d, CourtListener returns %s",
                    entry.normalized_cite,
                    entry.selected_cluster_id,
                    found_cluster_id,
                )
                db.commit()
                return "disputed"

        # Ambiguous: multiple results — check if user's choice is among them
        found_ids = {c.get("id") or c.get("cluster_id") for c in clusters}
        if entry.selected_cluster_id in found_ids:
            # User's choice is reasonable but not definitive
            db.commit()
            return "ambiguous"

        # User's choice not in results at all — flag as disputed
        entry.disputed = True
        db.commit()
        return "disputed"

    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        logger.warning("Reverification network error for %r: %s", entry.normalized_cite, exc)
        return "error"
    except Exception as exc:
        logger.warning("Reverification unexpected error for %r: %s", entry.normalized_cite, exc)
        return "error"
