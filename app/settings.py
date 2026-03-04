from pathlib import Path

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
    verification_timeout_seconds: int = 8

    ai_memo_enabled: bool = False
    ai_memo_include_content: bool = False
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    ai_timeout_seconds: int = 10

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
