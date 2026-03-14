"""Tests for Virginia Code statute verification.

Covers:
- Citation parsing (various Va. Code formats)
- Successful API verification (mock)
- API failure falls back to STATUTE_DETECTED gracefully
- Non-Virginia statutes are not sent to the Virginia API
- Setting disabled → stays STATUTE_DETECTED
- Summary counts include statute_verified_count
- Statute cache prevents redundant API calls
- Settings page shows virginia_statute_verification fields
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aaa_db.models import Base
from app.services.audit import CitationResult
from app.services.statute_verification import (
    VirginiaStatuteVerifier,
    parse_virginia_section,
    verify_virginia_section,
)
from app.services.verification import verify_citations


@pytest.fixture()
def db_session():
    """Fresh in-memory SQLite session for each test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_citation(raw_text: str, citation_type: str = "FullLawCitation") -> CitationResult:
    return CitationResult(
        raw_text=raw_text,
        citation_type=citation_type,
        normalized_text=raw_text,
        resolved_from=None,
        verification_status=None,
        verification_detail=None,
        snippet=None,
        candidate_cluster_ids=None,
        candidate_metadata=None,
        selected_cluster_id=None,
        resolution_method=None,
    )


def _make_va_api_response(chapter_list: list) -> httpx.Response:
    req = httpx.Request("GET", "https://law.lis.virginia.gov/api/test/")
    return httpx.Response(
        200,
        json={
            "TitleNumber": "15.2",
            "TitleName": "Counties, Cities and Towns",
            "ChapterList": chapter_list,
        },
        request=req,
    )


def _make_va_not_found_response() -> httpx.Response:
    req = httpx.Request("GET", "https://law.lis.virginia.gov/api/test/")
    return httpx.Response(
        200,
        json={"TitleNumber": None, "TitleName": None, "ChapterList": []},
        request=req,
    )


# ── parse_virginia_section ────────────────────────────────────────────────────


class TestParseVirginiaSection:
    def test_standard_format(self):
        assert parse_virginia_section("Va. Code § 15.2-3400") == "15.2-3400"

    def test_annotated_format(self):
        assert parse_virginia_section("Va. Code Ann. § 15.2-3400") == "15.2-3400"

    def test_code_of_virginia(self):
        assert parse_virginia_section("Code of Virginia § 18.2-308") == "18.2-308"

    def test_code_of_virginia_with_year(self):
        assert (
            parse_virginia_section("Code of Virginia, 1950, as amended, § 15.2-1300") == "15.2-1300"
        )

    def test_virginia_code_full(self):
        assert parse_virginia_section("Virginia Code § 46.2-100") == "46.2-100"

    def test_code_of_va(self):
        assert parse_virginia_section("Code of Va. § 18.2-308") == "18.2-308"

    def test_single_part_title(self):
        assert parse_virginia_section("Va. Code § 1-200") == "1-200"

    def test_three_part_title(self):
        assert parse_virginia_section("Va. Code § 3.2-6500") == "3.2-6500"

    def test_en_dash_normalised(self):
        # en-dash → hyphen
        result = parse_virginia_section("Va. Code § 15.2\u20133400")
        assert result == "15.2-3400"

    def test_lowercase_va_code(self):
        assert parse_virginia_section("va. code § 18.2-308") == "18.2-308"

    def test_in_sentence(self):
        text = "The defendant violated Va. Code § 18.2-308 by carrying a concealed weapon."
        assert parse_virginia_section(text) == "18.2-308"

    def test_non_virginia_returns_none(self):
        assert parse_virginia_section("Cal. Penal Code § 459") is None

    def test_bare_section_symbol_returns_none(self):
        # Without explicit Virginia prefix, don't attempt
        assert parse_virginia_section("§ 15.2-3400") is None

    def test_federal_statute_returns_none(self):
        assert parse_virginia_section("42 U.S.C. § 1983") is None

    def test_empty_returns_none(self):
        assert parse_virginia_section("") is None

    def test_case_law_citation_returns_none(self):
        assert parse_virginia_section("597 U.S. 1 (2022)") is None


# ── verify_virginia_section ───────────────────────────────────────────────────


class TestVerifyVirginiaSection:
    def test_found_section(self):
        chapter_list = [{"SectionTitle": "Voluntary settlements among local governments"}]
        mock_response = _make_va_api_response(chapter_list)
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        status, title = verify_virginia_section("15.2-3400", _client=mock_client)
        assert status == "STATUTE_VERIFIED"
        assert title == "Voluntary settlements among local governments"
        mock_client.get.assert_called_once_with(
            "https://law.lis.virginia.gov/api/CoVSectionsGetSectionDetailsJson/15.2-3400"
        )

    def test_not_found_section(self):
        mock_client = MagicMock()
        mock_client.get.return_value = _make_va_not_found_response()

        status, title = verify_virginia_section("99.99-9999", _client=mock_client)
        assert status == "STATUTE_NOT_FOUND"
        assert title is None

    def test_http_error_returns_statute_error(self):
        req = httpx.Request("GET", "https://law.lis.virginia.gov/api/test/")
        mock_response = httpx.Response(500, request=req)
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        status, title = verify_virginia_section("15.2-3400", _client=mock_client)
        assert status == "STATUTE_ERROR"
        assert title is None

    def test_timeout_returns_statute_error(self):
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.ReadTimeout("timed out")

        status, title = verify_virginia_section("15.2-3400", _client=mock_client)
        assert status == "STATUTE_ERROR"
        assert title is None

    def test_connect_error_returns_statute_error(self):
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.ConnectError("refused")

        status, title = verify_virginia_section("15.2-3400", _client=mock_client)
        assert status == "STATUTE_ERROR"
        assert title is None

    def test_section_title_none_when_missing_in_response(self):
        chapter_list = [{"SectionTitle": None}]
        mock_client = MagicMock()
        mock_client.get.return_value = _make_va_api_response(chapter_list)

        status, title = verify_virginia_section("15.2-3400", _client=mock_client)
        assert status == "STATUTE_VERIFIED"
        assert title is None


# ── VirginiaStatuteVerifier ───────────────────────────────────────────────────


class TestVirginiaStatuteVerifier:
    def test_delegates_to_verify_function(self):
        verifier = VirginiaStatuteVerifier(timeout_seconds=5)
        chapter_list = [{"SectionTitle": "The common law"}]
        mock_client = MagicMock()
        mock_client.get.return_value = _make_va_api_response(chapter_list)

        with patch(
            "app.services.statute_verification.httpx.Client",
        ) as mock_cls:
            mock_cls.return_value.__enter__ = lambda s: mock_client
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            status, title = verifier.verify("1-200")

        assert status == "STATUTE_VERIFIED"
        assert title == "The common law"


# ── verify_citations pipeline integration ────────────────────────────────────


def _base_verify_kwargs(**overrides):
    """Return minimal kwargs for verify_citations (no CourtListener)."""
    defaults = {
        "courtlistener_token": None,
        "verification_base_url": "https://www.courtlistener.com/api/rest/v4/citation-lookup/",
    }
    defaults.update(overrides)
    return defaults


class _MockVAVerifier:
    """Scriptable mock for VirginiaStatuteVerifier."""

    def __init__(self, responses: dict[str, tuple[str, str | None]]) -> None:
        self._responses = responses
        self.call_count = 0

    def verify(self, section_number: str) -> tuple[str, str | None]:
        self.call_count += 1
        return self._responses.get(section_number, ("STATUTE_ERROR", None))


class TestVerifyCitationsPipelineVA:
    def test_virginia_citation_verified(self):
        citations = [_make_citation("Va. Code § 15.2-3400")]
        va_verifier = _MockVAVerifier({"15.2-3400": ("STATUTE_VERIFIED", "Voluntary settlements")})

        result = verify_citations(
            citations,
            virginia_statute_verifier=va_verifier,
            **_base_verify_kwargs(),
        )
        assert result[0].verification_status == "STATUTE_VERIFIED"
        assert "15.2-3400" in result[0].verification_detail
        assert "Voluntary settlements" in result[0].verification_detail

    def test_virginia_citation_not_found_stays_statute_detected(self):
        citations = [_make_citation("Va. Code § 99.99-9999")]
        va_verifier = _MockVAVerifier({"99.99-9999": ("STATUTE_NOT_FOUND", None)})

        result = verify_citations(
            citations,
            virginia_statute_verifier=va_verifier,
            **_base_verify_kwargs(),
        )
        assert result[0].verification_status == "STATUTE_DETECTED"
        assert "not found" in result[0].verification_detail.lower()

    def test_api_error_keeps_statute_detected(self):
        citations = [_make_citation("Va. Code § 15.2-3400")]
        va_verifier = _MockVAVerifier({"15.2-3400": ("STATUTE_ERROR", None)})

        result = verify_citations(
            citations,
            virginia_statute_verifier=va_verifier,
            **_base_verify_kwargs(),
        )
        # Error must not downgrade — detail unchanged from default
        assert result[0].verification_status == "STATUTE_DETECTED"
        assert "not verified" in result[0].verification_detail.lower()

    def test_non_virginia_statute_not_sent_to_va_api(self):
        citations = [_make_citation("Cal. Penal Code § 459")]
        va_verifier = _MockVAVerifier({})

        result = verify_citations(
            citations,
            virginia_statute_verifier=va_verifier,
            **_base_verify_kwargs(),
        )
        assert result[0].verification_status == "STATUTE_DETECTED"
        assert va_verifier.call_count == 0

    def test_non_statute_citation_not_sent_to_va_api(self):
        citations = [_make_citation("597 U.S. 1 (2022)", citation_type="FullCaseCitation")]
        va_verifier = _MockVAVerifier({})

        result = verify_citations(
            citations,
            virginia_statute_verifier=va_verifier,
            **_base_verify_kwargs(),
        )
        # Case law without token → UNVERIFIED_NO_TOKEN, not sent to VA API
        assert result[0].verification_status == "UNVERIFIED_NO_TOKEN"
        assert va_verifier.call_count == 0

    def test_setting_disabled_skips_verification(self):
        citations = [_make_citation("Va. Code § 15.2-3400")]
        va_verifier = _MockVAVerifier({"15.2-3400": ("STATUTE_VERIFIED", "Title")})

        result = verify_citations(
            citations,
            virginia_statute_verification=False,
            virginia_statute_verifier=va_verifier,
            **_base_verify_kwargs(),
        )
        assert result[0].verification_status == "STATUTE_DETECTED"
        assert va_verifier.call_count == 0

    def test_statute_cache_hit_skips_api(self):
        citations = [_make_citation("Va. Code § 15.2-3400")]
        va_verifier = _MockVAVerifier({})  # no responses — would error if called
        cache = {"15.2-3400": {"status": "STATUTE_VERIFIED", "section_title": "Cached title"}}

        result = verify_citations(
            citations,
            virginia_statute_verifier=va_verifier,
            statute_cache=cache,
            **_base_verify_kwargs(),
        )
        assert result[0].verification_status == "STATUTE_VERIFIED"
        assert "Cached title" in result[0].verification_detail
        assert va_verifier.call_count == 0

    def test_not_found_result_written_to_cache(self):
        citations = [_make_citation("Va. Code § 99.99-9999")]
        va_verifier = _MockVAVerifier({"99.99-9999": ("STATUTE_NOT_FOUND", None)})
        cache: dict = {}

        verify_citations(
            citations,
            virginia_statute_verifier=va_verifier,
            statute_cache=cache,
            **_base_verify_kwargs(),
        )
        assert "99.99-9999" in cache
        assert cache["99.99-9999"]["status"] == "STATUTE_NOT_FOUND"

    def test_error_result_not_written_to_cache(self):
        citations = [_make_citation("Va. Code § 15.2-3400")]
        va_verifier = _MockVAVerifier({"15.2-3400": ("STATUTE_ERROR", None)})
        cache: dict = {}

        verify_citations(
            citations,
            virginia_statute_verifier=va_verifier,
            statute_cache=cache,
            **_base_verify_kwargs(),
        )
        assert "15.2-3400" not in cache


# ── Summary counts ────────────────────────────────────────────────────────────


class TestStatuteVerifiedCounts:
    def test_statute_verified_count_in_save_audit_run(self, db_session):
        from aaa_db.repository import save_audit_run

        citations = [
            _make_citation("Va. Code § 15.2-3400"),
            _make_citation("Va. Code § 18.2-308"),
            _make_citation("Cal. Penal Code § 459"),
        ]
        citations[0].verification_status = "STATUTE_VERIFIED"
        citations[0].verification_detail = "Confirmed"
        citations[1].verification_status = "STATUTE_VERIFIED"
        citations[1].verification_detail = "Confirmed"
        citations[2].verification_status = "STATUTE_DETECTED"
        citations[2].verification_detail = "Not verified"

        run = save_audit_run(
            db_session,
            source_type="text",
            source_name=None,
            input_text="test",
            warnings=[],
            citations=citations,
        )

        assert run.statute_verified_count == 2
        assert run.statute_count == 1

    def test_run_to_context_includes_statute_verified_count(self, db_session):
        from aaa_db.repository import save_audit_run
        from app.routes.pages import run_to_context

        c = _make_citation("Va. Code § 15.2-3400")
        c.verification_status = "STATUTE_VERIFIED"
        c.verification_detail = "Confirmed"

        run = save_audit_run(
            db_session,
            source_type="text",
            source_name=None,
            input_text="test",
            warnings=[],
            citations=[c],
        )
        ctx = run_to_context(run)
        assert ctx["statute_verified_count"] == 1


# ── Statute DB cache ──────────────────────────────────────────────────────────


class TestStatuteDBCache:
    def test_save_and_lookup(self, db_session):
        from aaa_db.repository import lookup_statute_cache, save_statute_cache_entry

        save_statute_cache_entry(
            db_session,
            section_number="15.2-3400",
            status="STATUTE_VERIFIED",
            section_title="Voluntary settlements",
        )
        cache = lookup_statute_cache(db_session)

        assert "15.2-3400" in cache
        assert cache["15.2-3400"]["status"] == "STATUTE_VERIFIED"
        assert cache["15.2-3400"]["section_title"] == "Voluntary settlements"

    def test_upsert_updates_existing(self, db_session):
        from aaa_db.repository import lookup_statute_cache, save_statute_cache_entry

        save_statute_cache_entry(
            db_session, section_number="18.2-308", status="STATUTE_NOT_FOUND", section_title=None
        )
        save_statute_cache_entry(
            db_session,
            section_number="18.2-308",
            status="STATUTE_VERIFIED",
            section_title="Firearms",
        )
        cache = lookup_statute_cache(db_session)

        assert cache["18.2-308"]["status"] == "STATUTE_VERIFIED"
        assert cache["18.2-308"]["section_title"] == "Firearms"


# ── Settings page ─────────────────────────────────────────────────────────────


class TestSettingsVirginiaFields:
    def test_virginia_fields_in_ui_keys(self):
        from app.services.settings_service import _UI_KEYS

        assert "virginia_statute_verification" in _UI_KEYS
        assert "virginia_statute_timeout_seconds" in _UI_KEYS

    def test_effective_settings_defaults(self):
        from app.services.settings_service import _EffectiveSettings

        eff = _EffectiveSettings({})
        assert eff.virginia_statute_verification is True
        assert eff.virginia_statute_timeout_seconds == 10

    def test_effective_settings_db_override(self):
        from app.services.settings_service import _EffectiveSettings

        eff = _EffectiveSettings(
            {"virginia_statute_verification": "false", "virginia_statute_timeout_seconds": "20"}
        )
        assert eff.virginia_statute_verification is False
        assert eff.virginia_statute_timeout_seconds == 20
