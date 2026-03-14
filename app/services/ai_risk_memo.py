from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import openai

if TYPE_CHECKING:
    from app.settings import Settings

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
    generated_by: str | None = None


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


def _call_openai_client(
    client: openai.OpenAI, model: str, prompt: str
) -> openai.types.chat.ChatCompletion:
    """Shared chat completion call used by both providers."""
    return client.chat.completions.create(
        model=model,
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


def _parse_completion(response: openai.types.chat.ChatCompletion) -> RiskMemo:
    content = response.choices[0].message.content
    parsed = json.loads(content or "{}")
    return _normalize_payload(parsed)


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
            response = _call_openai_client(self._client, self._model, prompt)
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
            memo = _parse_completion(response)
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
        memo.generated_by = f"OpenAI {self._model}"
        return memo


# ── Ollama provider ───────────────────────────────────────────────────────────


class OllamaProvider:
    """Generates risk memos using a local Ollama instance via its OpenAI-compatible API."""

    def __init__(self, base_url: str, model: str, timeout_seconds: int) -> None:
        self._model = model
        self._base_url = base_url
        # Ollama doesn't require an API key; use a dummy value to satisfy the SDK
        self._client = openai.OpenAI(
            api_key="ollama",
            base_url=f"{base_url.rstrip('/')}/v1",
            timeout=float(timeout_seconds),
            max_retries=0,  # don't retry — slow local models shouldn't double the wait
        )

    def generate_memo(self, audit_context: dict[str, Any]) -> RiskMemo:
        t0 = time.perf_counter()
        prompt = _build_prompt(audit_context)

        try:
            response = _call_openai_client(self._client, self._model, prompt)
        except openai.APITimeoutError:
            logger.warning("Ollama request timed out (model=%s)", self._model)
            return unavailable_memo(
                "Ollama request timed out. The model may be loading or your machine "
                "may be under heavy load."
            )
        except openai.APIConnectionError:
            logger.warning("Could not connect to Ollama at %s", self._base_url)
            return unavailable_memo(
                "Ollama is not running. Start it with 'ollama serve' and try again."
            )
        except openai.NotFoundError:
            logger.warning("Ollama model not found: %s", self._model)
            return unavailable_memo(
                f"Model '{self._model}' not found in Ollama. "
                f"Run 'ollama pull {self._model}' to download it."
            )
        except openai.APIError as exc:
            logger.warning("Ollama API error: %s", exc)
            return unavailable_memo(f"Ollama API error: {exc}")
        except Exception:
            logger.exception("Unexpected error calling Ollama.")
            return unavailable_memo("Ollama memo generation failed unexpectedly.")

        try:
            memo = _parse_completion(response)
        except Exception:
            logger.exception("Failed to parse Ollama memo response.")
            return unavailable_memo("Could not parse Ollama memo response.")

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "Ollama risk memo generated in %dms (model=%s, risk_level=%s)",
            elapsed_ms,
            self._model,
            memo.risk_level,
        )
        memo.generated_by = f"Ollama {self._model}"
        return memo


# ── Provider selection ────────────────────────────────────────────────────────


def build_provider(settings: Settings) -> MemoProvider | None:
    """Return the appropriate MemoProvider based on settings, or None to skip memo generation."""
    ai_provider = (settings.ai_provider or "none").lower()

    if ai_provider == "openai":
        if not settings.openai_api_key:
            return None  # generate_risk_memo will return the "no key" unavailable memo
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=settings.ai_memo_model,
            timeout_seconds=settings.ai_request_timeout_seconds,
        )

    if ai_provider == "ollama":
        return OllamaProvider(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            timeout_seconds=settings.ai_request_timeout_seconds,
        )

    if ai_provider != "none":
        logger.warning(
            "Unknown AI_PROVIDER value %r — AI memo disabled. "
            "Valid values: 'none', 'openai', 'ollama'.",
            settings.ai_provider,
        )

    return None


# ── Memo serialization ────────────────────────────────────────────────────────


def memo_to_json(memo: RiskMemo) -> str:
    """Serialize a RiskMemo to a JSON string for DB persistence."""
    from dataclasses import asdict

    return json.dumps(asdict(memo))


def memo_from_json(json_str: str) -> RiskMemo:
    """Deserialize a RiskMemo from a JSON string stored in the DB."""
    data = json.loads(json_str)
    return RiskMemo(
        risk_level=data.get("risk_level", "Unavailable"),
        summary=data.get("summary", ""),
        top_issues=data.get("top_issues") or [],
        recommended_actions=data.get("recommended_actions") or [],
        advisory_note=data.get("advisory_note") or _ADVISORY_NOTE,
        available=bool(data.get("available", True)),
        unavailable_reason=data.get("unavailable_reason"),
        generated_by=data.get("generated_by"),
    )


# ── Public entry point ────────────────────────────────────────────────────────


def generate_risk_memo(
    run_data: dict[str, Any],
    *,
    enabled: bool = True,
    api_key: str | None = None,
    model: str = "gpt-4o-mini",
    timeout_seconds: int = 60,
    provider: MemoProvider | None = None,
) -> RiskMemo:
    """Generate an AI risk memo, returning an unavailable memo on any failure.

    When *provider* is given it is used directly (api_key/model are ignored).
    When *provider* is None an OpenAIProvider is created from api_key/model.
    """
    if not enabled:
        return unavailable_memo("AI memo is disabled in settings.")

    # api_key check only applies when no explicit provider is given
    if not api_key and provider is None:
        return unavailable_memo("OpenAI API key is not configured.")

    logger.info("Generating AI risk memo using model: %s", model)

    active_provider: MemoProvider = provider or OpenAIProvider(
        api_key=api_key,  # type: ignore[arg-type]
        model=model,
        timeout_seconds=timeout_seconds,
    )
    return active_provider.generate_memo(run_data)
