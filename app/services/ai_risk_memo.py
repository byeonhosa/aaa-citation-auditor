from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import openai

logger = logging.getLogger(__name__)

_ADVISORY_NOTE = (
    "AI analysis is advisory only. Deterministic verification statuses remain the source of truth."
)


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
        summary=reason,
        top_issues=[],
        recommended_actions=[],
        advisory_note=_ADVISORY_NOTE,
        available=False,
        unavailable_reason=reason,
    )


class MemoProvider(Protocol):
    """Interface that any AI memo provider must implement."""

    def generate_memo(self, audit_context: dict[str, Any]) -> RiskMemo: ...


# ── Prompt building ───────────────────────────────────────────────────────────


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
        advisory_note=str(data.get("advisory_note") or _ADVISORY_NOTE),
    )


def _build_prompt(run_data: dict[str, Any]) -> str:
    summary = run_data.get("verification_summary", {})
    source_type = run_data.get("source_type", "unknown")
    source_name = run_data.get("source_name") or "unnamed source"
    citation_count = run_data.get("citation_count", 0)
    warnings_present = run_data.get("warnings_present", False)

    lines = [
        "You are a legal citation audit assistant. Analyse the audit results below and produce "
        "a concise advisory risk memo. This is NOT general legal advice — it is specifically "
        "about citation verification accuracy for a legal document.",
        "",
        "Return a JSON object with exactly these keys:",
        "  risk_level: one of 'Low', 'Moderate', 'High', or 'Critical'",
        "  summary: 1-2 sentence plain-English summary of the citation risk",
        "  top_issues: list of up to 3 specific issues found (strings, may be empty)",
        "  recommended_actions: list of up to 3 concrete next steps (strings, may be empty)",
        f'  advisory_note: always set to "{_ADVISORY_NOTE}"',
        "",
        f"Source type: {source_type}",
        f"Source name: {source_name}",
        f"Total citations: {citation_count}",
        f"Verification summary: {json.dumps(summary, ensure_ascii=False)}",
        f"Warnings present: {warnings_present}",
    ]

    if run_data.get("citations"):
        lines.append(
            f"Citations (sample): {json.dumps(run_data['citations'][:10], ensure_ascii=False)}"
        )
    if run_data.get("warnings"):
        lines.append(f"Warning messages: {json.dumps(run_data['warnings'], ensure_ascii=False)}")

    return "\n".join(lines)


# ── OpenAI provider ───────────────────────────────────────────────────────────


class OpenAIProvider:
    """Generates risk memos using the OpenAI chat completions API."""

    def __init__(self, api_key: str, model: str, timeout_seconds: int) -> None:
        self._model = model
        self._client = openai.OpenAI(
            api_key=api_key,
            timeout=float(timeout_seconds),
            max_retries=2,
        )

    def generate_memo(self, audit_context: dict[str, Any]) -> RiskMemo:
        t0 = time.perf_counter()
        prompt = _build_prompt(audit_context)

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You produce concise advisory legal citation risk memos. "
                            "Always return valid JSON with the exact keys requested."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
        except openai.AuthenticationError:
            logger.error("OpenAI authentication error — invalid API key.")
            return unavailable_memo("Invalid OpenAI API key.")
        except openai.RateLimitError:
            logger.warning("OpenAI rate limit / quota exceeded.")
            return unavailable_memo("OpenAI quota exceeded — check your billing.")
        except openai.APITimeoutError:
            logger.warning("OpenAI request timed out.")
            return unavailable_memo("OpenAI request timed out.")
        except openai.APIConnectionError:
            logger.warning("Could not connect to OpenAI.")
            return unavailable_memo("Could not connect to OpenAI.")
        except openai.APIError as exc:
            logger.warning("OpenAI API error: %s", exc)
            return unavailable_memo(f"OpenAI API error: {exc}")
        except Exception:
            logger.exception("Unexpected error generating AI risk memo.")
            return unavailable_memo("AI memo generation failed unexpectedly.")

        try:
            content = response.choices[0].message.content
            parsed = json.loads(content or "{}")
            memo = _normalize_payload(parsed)
        except Exception:
            logger.exception("Failed to parse AI risk memo response.")
            return unavailable_memo("Could not parse AI memo response.")

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "AI risk memo generated in %dms (model=%s, risk_level=%s)",
            elapsed_ms,
            self._model,
            memo.risk_level,
        )
        return memo


# ── Public entry point ────────────────────────────────────────────────────────


def generate_risk_memo(
    run_data: dict[str, Any],
    *,
    enabled: bool,
    api_key: str | None,
    model: str,
    timeout_seconds: int,
    provider: MemoProvider | None = None,
) -> RiskMemo:
    """Generate an AI risk memo, returning an unavailable memo on any failure."""
    if not enabled:
        return unavailable_memo("AI memo is disabled in settings.")

    if not api_key:
        return unavailable_memo("OpenAI API key is not configured.")

    logger.info("Generating AI risk memo using model: %s", model)

    active_provider: MemoProvider = provider or OpenAIProvider(
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )
    return active_provider.generate_memo(run_data)
