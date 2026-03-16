import secrets
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
TEMPLATES_DIR = APP_DIR / "templates"


class Settings(BaseSettings):
    app_name: str = "AAA - AI Agent Auditor"
    debug: bool = False
    app_version: str | None = "0.1.0"
    database_url: str = "sqlite:///./aaa.db"

    courtlistener_token: str | None = None
    verification_base_url: str = "https://www.courtlistener.com/api/rest/v4/citation-lookup/"
    courtlistener_timeout_seconds: int = 30
    batch_verification: bool = True
    search_fallback_enabled: bool = True

    ai_provider: str = "none"  # "none" | "openai" | "ollama"
    ai_memo_include_content: bool = False
    openai_api_key: str | None = None
    ai_memo_model: str = "gpt-4o-mini"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    ai_request_timeout_seconds: int = 60

    virginia_statute_verification: bool = True
    virginia_statute_timeout_seconds: int = 10

    govinfo_api_key: str | None = None
    federal_statute_verification: bool = True
    federal_statute_timeout_seconds: int = 15

    cap_api_key: str | None = None
    cap_fallback_enabled: bool = True
    cap_timeout_seconds: int = 15

    local_index_enabled: bool = True

    max_file_size_mb: int = 50
    max_files_per_batch: int = 10
    max_citations_per_run: int = 500

    log_level: str = "INFO"

    secret_key: str = Field(default_factory=lambda: secrets.token_hex(32))

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
