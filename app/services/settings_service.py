"""Settings service: read/write app settings stored in the database.

DB values override .env / pydantic defaults at runtime.  Sensitive fields
(API keys, tokens) are masked for display as ••••<last4>.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from aaa_db.models import AppSettings

if TYPE_CHECKING:
    pass

# Keys treated as sensitive — displayed masked in the UI
_SENSITIVE_KEYS = {"courtlistener_token", "openai_api_key", "govinfo_api_key", "cap_api_key"}

# Settings exposed in the UI, in the order they appear in the form
_UI_KEYS: list[str] = [
    "courtlistener_token",
    "verification_base_url",
    "courtlistener_timeout_seconds",
    "search_fallback_enabled",
    "ai_provider",
    "openai_api_key",
    "ai_memo_model",
    "ai_request_timeout_seconds",
    "ai_memo_include_content",
    "ollama_base_url",
    "ollama_model",
    "virginia_statute_verification",
    "virginia_statute_timeout_seconds",
    "govinfo_api_key",
    "federal_statute_verification",
    "federal_statute_timeout_seconds",
    "cap_api_key",
    "cap_fallback_enabled",
    "cap_timeout_seconds",
    "local_index_enabled",
    "max_file_size_mb",
    "max_files_per_batch",
    "max_citations_per_run",
    "log_level",
]


# ── DB helpers ────────────────────────────────────────────────────────────────


def get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    """Return the stored value for *key*, or *default* if not set."""
    row = db.query(AppSettings).filter(AppSettings.key == key).first()
    return row.value if row is not None else default


def save_setting(db: Session, key: str, value: str | None) -> None:
    """Upsert *key* → *value* in the app_settings table."""
    row = db.query(AppSettings).filter(AppSettings.key == key).first()
    if row is None:
        row = AppSettings(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()


# ── Masking ───────────────────────────────────────────────────────────────────


def _mask(value: str | None) -> str:
    """Return ••••<last4> for non-empty values, empty string otherwise."""
    if not value:
        return ""
    tail = value[-4:] if len(value) >= 4 else value
    return f"••••{tail}"


def _is_masked(value: str) -> bool:
    return value.startswith("••••")


# ── UI helpers ────────────────────────────────────────────────────────────────


def get_all_ui_settings(db: Session) -> dict[str, Any]:
    """Return all UI-visible settings as a flat dict of display strings.

    Sensitive values are masked.  Used to pre-populate the settings form.
    """
    from app.settings import settings as _defaults  # local import to avoid circular

    rows: dict[str, str | None] = {
        row.key: row.value
        for row in db.query(AppSettings).filter(AppSettings.key.in_(_UI_KEYS)).all()
    }

    result: dict[str, Any] = {}
    for key in _UI_KEYS:
        # Real value: DB first, then pydantic default
        raw = rows.get(key)
        if raw is None:
            default_val = getattr(_defaults, key, None)
            raw = str(default_val) if default_val is not None else ""
        # Mask sensitive fields
        if key in _SENSITIVE_KEYS:
            result[key] = _mask(raw)
        else:
            result[key] = raw
    return result


# ── Effective settings (runtime duck-type) ────────────────────────────────────


class _EffectiveSettings:
    """Merges DB overrides on top of pydantic settings defaults.

    Exposes the same attribute names as ``app.settings.Settings`` so it can
    be passed to ``build_provider()``, ``verify_citations()``, etc. without
    changing those function signatures.
    """

    def __init__(self, db_values: dict[str, str | None]) -> None:
        self._db = db_values

    def _get_str(self, key: str, default: str | None) -> str | None:
        v = self._db.get(key)
        return v if v is not None else default

    def _get_int(self, key: str, default: int) -> int:
        v = self._db.get(key)
        if v is not None:
            try:
                return int(v)
            except (ValueError, TypeError):
                return default
        return default

    def _get_bool(self, key: str, default: bool) -> bool:
        v = self._db.get(key)
        if v is not None:
            return v.lower() in ("true", "1", "yes", "on")
        return default

    # ── Settings attributes ───────────────────────────────────────────────────

    @property
    def app_version(self) -> str | None:
        from app.settings import settings as _s

        return _s.app_version

    @property
    def courtlistener_token(self) -> str | None:
        from app.settings import settings as _s

        return self._get_str("courtlistener_token", _s.courtlistener_token)

    @property
    def verification_base_url(self) -> str:
        from app.settings import settings as _s

        v = self._get_str("verification_base_url", _s.verification_base_url)
        return v or _s.verification_base_url

    @property
    def courtlistener_timeout_seconds(self) -> int:
        from app.settings import settings as _s

        return self._get_int("courtlistener_timeout_seconds", _s.courtlistener_timeout_seconds)

    @property
    def batch_verification(self) -> bool:
        from app.settings import settings as _s

        return self._get_bool("batch_verification", _s.batch_verification)

    @property
    def search_fallback_enabled(self) -> bool:
        from app.settings import settings as _s

        return self._get_bool("search_fallback_enabled", _s.search_fallback_enabled)

    @property
    def ai_provider(self) -> str:
        from app.settings import settings as _s

        return self._get_str("ai_provider", _s.ai_provider) or _s.ai_provider

    @property
    def ai_memo_include_content(self) -> bool:
        from app.settings import settings as _s

        return self._get_bool("ai_memo_include_content", _s.ai_memo_include_content)

    @property
    def openai_api_key(self) -> str | None:
        from app.settings import settings as _s

        return self._get_str("openai_api_key", _s.openai_api_key)

    @property
    def ai_memo_model(self) -> str:
        from app.settings import settings as _s

        return self._get_str("ai_memo_model", _s.ai_memo_model) or _s.ai_memo_model

    @property
    def ollama_base_url(self) -> str:
        from app.settings import settings as _s

        return self._get_str("ollama_base_url", _s.ollama_base_url) or _s.ollama_base_url

    @property
    def ollama_model(self) -> str:
        from app.settings import settings as _s

        return self._get_str("ollama_model", _s.ollama_model) or _s.ollama_model

    @property
    def ai_request_timeout_seconds(self) -> int:
        from app.settings import settings as _s

        return self._get_int("ai_request_timeout_seconds", _s.ai_request_timeout_seconds)

    @property
    def virginia_statute_verification(self) -> bool:
        from app.settings import settings as _s

        return self._get_bool("virginia_statute_verification", _s.virginia_statute_verification)

    @property
    def virginia_statute_timeout_seconds(self) -> int:
        from app.settings import settings as _s

        return self._get_int(
            "virginia_statute_timeout_seconds", _s.virginia_statute_timeout_seconds
        )

    @property
    def govinfo_api_key(self) -> str | None:
        from app.settings import settings as _s

        return self._get_str("govinfo_api_key", _s.govinfo_api_key)

    @property
    def federal_statute_verification(self) -> bool:
        from app.settings import settings as _s

        return self._get_bool("federal_statute_verification", _s.federal_statute_verification)

    @property
    def federal_statute_timeout_seconds(self) -> int:
        from app.settings import settings as _s

        return self._get_int("federal_statute_timeout_seconds", _s.federal_statute_timeout_seconds)

    @property
    def cap_api_key(self) -> str | None:
        from app.settings import settings as _s

        return self._get_str("cap_api_key", _s.cap_api_key)

    @property
    def cap_fallback_enabled(self) -> bool:
        from app.settings import settings as _s

        return self._get_bool("cap_fallback_enabled", _s.cap_fallback_enabled)

    @property
    def cap_timeout_seconds(self) -> int:
        from app.settings import settings as _s

        return self._get_int("cap_timeout_seconds", _s.cap_timeout_seconds)

    @property
    def local_index_enabled(self) -> bool:
        from app.settings import settings as _s

        return self._get_bool("local_index_enabled", _s.local_index_enabled)

    @property
    def max_file_size_mb(self) -> int:
        from app.settings import settings as _s

        return self._get_int("max_file_size_mb", _s.max_file_size_mb)

    @property
    def max_files_per_batch(self) -> int:
        from app.settings import settings as _s

        return self._get_int("max_files_per_batch", _s.max_files_per_batch)

    @property
    def max_citations_per_run(self) -> int:
        from app.settings import settings as _s

        return self._get_int("max_citations_per_run", _s.max_citations_per_run)

    @property
    def log_level(self) -> str:
        from app.settings import settings as _s

        return self._get_str("log_level", _s.log_level) or _s.log_level


def load_effective_settings(db: Session) -> _EffectiveSettings:
    """Load all DB settings and return an ``_EffectiveSettings`` instance."""
    rows: dict[str, str | None] = {row.key: row.value for row in db.query(AppSettings).all()}
    return _EffectiveSettings(rows)
