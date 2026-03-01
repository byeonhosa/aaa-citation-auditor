from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import request


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

    req = request.Request(
        "https://api.openai.com/v1/chat/completions",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return _normalize_payload(parsed)
    except Exception:
        return unavailable_memo("AI memo generation failed.")
