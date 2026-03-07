"""Shared HTTP client with retry logic for transient failures."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRY_MAX_ATTEMPTS = 3
RETRY_DELAYS = (1, 2, 4)  # seconds between attempts
RETRYABLE_STATUS_CODES = frozenset({429, 503})


def post_with_retry(
    url: str,
    *,
    data: dict[str, str] | None = None,
    json_body: Any | None = None,
    headers: dict[str, str],
    timeout_seconds: int = 30,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    retry_delays: tuple[int, ...] = RETRY_DELAYS,
) -> httpx.Response:
    """Send an HTTP POST with exponential-backoff retry on transient failures.

    Retries on HTTP 429/503 responses and on timeout/connection errors.

    Returns the final ``httpx.Response`` on success or after exhausting
    retries on a retryable HTTP status.  Raises the underlying
    ``httpx.TimeoutException`` or ``httpx.ConnectError`` if all attempts
    fail with a transport-level error.
    """
    last_response: httpx.Response | None = None

    with httpx.Client(timeout=timeout_seconds) as client:
        for attempt in range(max_attempts):
            try:
                response = client.post(
                    url,
                    data=data,
                    json=json_body,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                if attempt < max_attempts - 1:
                    delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                    logger.warning(
                        "%s for %s (attempt %d/%d, retrying in %ds)",
                        type(exc).__name__,
                        url,
                        attempt + 1,
                        max_attempts,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise  # last attempt — propagate

            # Retryable HTTP status (e.g. 429 rate-limit, 503 unavailable)
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < max_attempts - 1:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                logger.warning(
                    "HTTP %d from %s (attempt %d/%d, retrying in %ds)",
                    response.status_code,
                    url,
                    attempt + 1,
                    max_attempts,
                    delay,
                )
                last_response = response
                time.sleep(delay)
                continue

            return response

    # All retries exhausted on a retryable status code
    if last_response is not None:
        return last_response
    raise RuntimeError("Retry loop exited unexpectedly")
