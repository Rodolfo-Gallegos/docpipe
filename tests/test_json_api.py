"""Tests for the json_api adapter, both response shapes."""
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from docpipe import FetchedDocument, get_source
from tests.conftest import make_response

WP_MEDIA = [
    {"source_url": "https://boe.example.gov/uploads/report-2026-03.pdf", "date": "2026-03-12T10:00:00"},
    {"source_url": "https://boe.example.gov/uploads/report-2026-02.pdf", "date": "2026-02-10T10:00:00"},
    {"source_url": "https://boe.example.gov/uploads/photo.jpg", "date": "2026-02-01T10:00:00"},
]

ITEMS = {
    "items": [
        {
            "id": 41,
            "date": "2026-04-02T00:00:00",
            "department": "Procurement",
            "category": {"key": "bids", "title": "Bid Notices"},
            "location": "Room 12",
            "contact": "purchasing@example.gov",
            "subject": "Notice of intent to award the roofing contract.",
        },
        {
            "id": 40,
            "date": "2026-03-30T00:00:00",
            "department": "HR",
            "category": {"key": "hr", "title": "Personnel"},
            "subject": "Staffing update.",
        },
    ]
}


@pytest.fixture
def wp_source(settings):
    return get_source(
        "json_api",
        "test-wp",
        {"api_url": "https://boe.example.gov/wp-json/wp/v2/media", "mode": "wp_rest_media"},
        settings=settings,
    )


def _routes(json_data, pdf_bytes=b"%PDF-1.4 fake"):
    def handler(url, **kwargs):
        if "wp-json" in url or "/api/" in url:
            return make_response(json_data=json_data)
        return make_response(content=pdf_bytes, content_type="application/pdf")
    return handler


# ── wp_rest_media ───────────────────────────────────────────────────────


def test_downloads_only_pdfs(wp_source, fake_http):
    fake_http(_routes(WP_MEDIA))
    docs = wp_source.fetch_documents(limit=5)

    assert len(docs) == 2  # the .jpg is skipped
    assert all(d.content_type == "pdf" for d in docs)
    assert docs[0].source_url.endswith("report-2026-03.pdf")
    assert docs[0].doc_date == date(2026, 3, 12)
    assert docs[0].raw_content == b"%PDF-1.4 fake"
    assert docs[0].local_path.exists()


def test_respects_limit(wp_source, fake_http):
    fake_http(_routes(WP_MEDIA))
    assert len(wp_source.fetch_documents(limit=1)) == 1


def test_non_list_response_returns_empty(wp_source, fake_http):
    fake_http(_routes({"oops": 1}))
    assert wp_source.fetch_documents(limit=5) == []


def test_to_text_dispatches_to_the_pdf_extractor(wp_source, tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF")
    doc = FetchedDocument(
        source_url="u", local_path=pdf, raw_content=b"%PDF",
        content_type="pdf", fetched_at=datetime.now(timezone.utc),
    )
    with patch(
        "docpipe.sources.json_api.pdf_extract.extract_text",
        return_value=("hello report", "pdfplumber"),
    ) as extract:
        assert wp_source.to_text(doc) == ("hello report", "pdfplumber")
    extract.assert_called_once()


# ── metadata_items ──────────────────────────────────────────────────────


def test_synthesizes_one_document_per_item(settings, fake_http):
    fake_http(_routes(ITEMS))
    source = get_source(
        "json_api", "test-items",
        {"api_url": "https://mainapi.example.gov/api/v1/notices"},
        settings=settings,
    )
    docs = source.fetch_documents(limit=5)

    assert len(docs) == 2
    assert docs[0].content_type == "html"
    assert docs[0].doc_date == date(2026, 4, 2)
    assert docs[0].source_url.endswith("/41")

    text, method = source.to_text(docs[0])
    assert method == "html"
    assert "Procurement" in text
    assert "Bid Notices" in text            # nested path category.title
    assert "roofing contract" in text


def test_client_side_filter_by_nested_field(settings, fake_http):
    fake_http(_routes(ITEMS))
    source = get_source(
        "json_api", "test-filter",
        {
            "api_url": "https://mainapi.example.gov/api/v1/notices",
            "filter_path": "category.key",
            "filter_value": "bids",
        },
        settings=settings,
    )
    docs = source.fetch_documents(limit=5)
    assert len(docs) == 1
    assert docs[0].source_url.endswith("/41")


def test_legacy_mode_and_field_names_still_work(settings):
    """Configs written against the original adapter keep working:
    mode "dadeschools" and the "category_key" field name."""
    source = get_source(
        "json_api", "legacy",
        {
            "api_url": "https://mainapi.example.gov/api/v1/notices",
            "mode": "dadeschools",
            "category_key": "bids",
        },
        settings=settings,
    )
    assert source.config.mode == "metadata_items"
    assert source.config.filter_value == "bids"


def test_custom_field_mapping(settings, fake_http):
    fake_http(_routes({"results": [{"ref": "A-9", "when": "2026-01-05", "body": "Tender notice"}]}))
    source = get_source(
        "json_api", "custom",
        {
            "api_url": "https://example.gov/api/tenders",
            "items_key": "results",
            "id_field": "ref",
            "date_field": "when",
            "text_fields": {"REFERENCE": "ref", "BODY": "body"},
            "filter_path": None,
        },
        settings=settings,
    )
    docs = source.fetch_documents(limit=5)
    assert len(docs) == 1
    assert docs[0].doc_date == date(2026, 1, 5)
    text, _ = source.to_text(docs[0])
    assert "REFERENCE: A-9" in text
    assert "Tender notice" in text
