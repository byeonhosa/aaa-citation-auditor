from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.services.http_client import post_with_retry

logger = logging.getLogger(__name__)


@dataclass
class RiskMemo:
    risk_level: str
    summary: str
    top_issues: list[str]
    recommended_actions: list[str]
    advisory_note: str
    available: bool = True
    unavailable_reason: str | None = None


def unavailable_memo(reason: str) -> RiskMemo:
    return RiskMemo(
        risk_level="Unavailable",
        summary="AI risk memo is unavailable.",
        top_issues=[],
        recommended_actions=[],
        advisory_note=(
            "AI analysis is advisory only. Deterministic verification statuses "
            "remain the source of truth."
        ),
        available=False,
        unavailable_reason=reason,
    )


def _coerce_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_payload(data: Any) -> RiskMemo:
    if not isinstance(data, dict):
        return unavailable_memo("AI response was not structured as expected.")

    return RiskMemo(
        risk_level=str(data.get("risk_level") or "Moderate"),
        summary=str(data.get("summary") or "No summary returned."),
        top_issues=_coerce_list(data.get("top_issues")),
        recommended_actions=_coerce_list(data.get("recommended_actions")),
        advisory_note=str(
            data.get("advisory_note")
            or "AI analysis is advisory. Deterministic statuses remain the source of truth."
        ),
    )


def _build_prompt(run_data: dict[str, Any]) -> str:
    return (
        "You are drafting a concise legal citation audit risk memo. "
        "Use only provided structured audit data. "
        "Do not override deterministic verification statuses. "
        "Return JSON object with keys: risk_level, summary, top_issues, recommended_actions, "
        "advisory_note.\n\n"
        f"Audit data:\n{json.dumps(run_data, ensure_ascii=False)}"
    )


def generate_risk_memo(
    run_data: dict[str, Any],
    *,
    enabled: bool,
    api_key: str | None,
    model: str,
    timeout_seconds: int,
) -> RiskMemo:
    if not enabled:
        return unavailable_memo("AI memo is disabled in settings.")

    if not api_key:
        return unavailable_memo("OpenAI API key is not configured.")

    logger.info("Generating AI risk memo using model: %s", model)
    prompt = _build_prompt(run_data)
    payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You produce concise advisory legal risk memos from structured "
                    "citation audit data."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }

    t0 = time.perf_counter()
    try:
        response = post_with_retry(
            "https://api.openai.com/v1/chat/completions",
            json_body=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
            },
            timeout_seconds=timeout_seconds,
        )
    except httpx.TimeoutException:
        logger.warning("OpenAI request timed out after retries.")
        return unavailable_memo("AI memo request timed out after retries.")
    except Exception:
        logger.warning("OpenAI request failed after retries.", exc_info=True)
        return unavailable_memo("AI memo generation failed.")

    if response.status_code != 200:
        logger.warning("OpenAI returned HTTP %d.", response.status_code)
        return unavailable_memo("AI memo generation failed.")

    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        memo = _normalize_payload(parsed)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("AI risk memo generated in %dms (risk_level=%s)", elapsed_ms, memo.risk_level)
        return memo
    except Exception:
        logger.exception("Failed to parse AI risk memo response.")
        return unavailable_memo("AI memo generation failed.")
