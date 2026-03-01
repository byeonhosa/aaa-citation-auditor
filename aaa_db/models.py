from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for AAA database models."""


class AuditRun(Base):
    __tablename__ = "audit_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source_type: Mapped[str] = mapped_column(String(16))
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    citation_count: Mapped[int] = mapped_column(Integer, default=0)
    verified_count: Mapped[int] = mapped_column(Integer, default=0)
    not_found_count: Mapped[int] = mapped_column(Integer, default=0)
    ambiguous_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    unverified_no_token_count: Mapped[int] = mapped_column(Integer, default=0)

    input_text_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    citations: Mapped[list["CitationResultRecord"]] = relationship(
        back_populates="audit_run",
        cascade="all, delete-orphan",
    )


class CitationResultRecord(Base):
    __tablename__ = "citation_results"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    audit_run_id: Mapped[int] = mapped_column(ForeignKey("audit_runs.id", ondelete="CASCADE"))

    raw_text: Mapped[str] = mapped_column(Text)
    citation_type: Mapped[str] = mapped_column(String(128))
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)

    audit_run: Mapped[AuditRun] = relationship(back_populates="citations")


class TelemetryEvent(Base):
    __tablename__ = "telemetry_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event_type: Mapped[str] = mapped_column(String(64))
    install_id: Mapped[str] = mapped_column(String(64))

    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(16), nullable=True)

    citation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verified_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    not_found_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ambiguous_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unverified_no_token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    had_warning: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
