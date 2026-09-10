"""Tests for docpipe.runner: the journal is the product."""
from docpipe.runner import run_source
from tests.conftest import make_response

INDEX = """
<html><body>
  <a href="/docs/a.pdf">6-15-2026 Board Meeting</a>
  <a href="/docs/b.pdf">6-8-2026 Work Session</a>
</body></html>
"""


def _config(url="https://example.org/minutes"):
    return {"page_url": url}


def test_bad_config_is_a_diagnosis_not_a_traceback(settings):
    record = run_source("acme", "pdf_direct", {"wrong_key": 1}, settings=settings)
    assert record.status == "error"
    assert "schema" in record.diagnosis
    assert record.steps[0].name == "build" and record.steps[0].ok is False


def test_unknown_source_type_is_reported(settings):
    record = run_source("acme", "nope", {}, settings=settings)
    assert record.status == "error"
    assert "Unknown source_type" in record.error


def test_clean_run_but_no_documents_is_empty(settings, fake_http):
    fake_http(lambda url, **kw: make_response(text="<html><body>nothing</body></html>"))
    record = run_source("acme", "pdf_direct", _config(), settings=settings)
    assert record.status == "empty"
    assert "probe" in record.diagnosis


def test_documents_without_text_are_partial(settings, fake_http):
    def handler(url, **kw):
        if url.endswith(".pdf"):
            return make_response(content=b"%PDF-1.4", content_type="application/pdf")
        return make_response(text=INDEX)

    fake_http(handler)
    record = run_source("acme", "pdf_direct", _config(), settings=settings)
    assert record.status == "partial"
    assert "OCR" in record.diagnosis or "usable text" in record.diagnosis


def test_journal_records_every_step(settings, fake_http):
    def handler(url, **kw):
        if url.endswith(".pdf"):
            return make_response(content=b"%PDF-1.4", content_type="application/pdf")
        return make_response(text=INDEX)

    fake_http(handler)
    record = run_source("acme", "pdf_direct", _config(), settings=settings)
    assert [s.name for s in record.steps] == ["build", "fetch", "extract"]
    assert record.elapsed_ms >= 0
    assert record.to_dict()["source_id"] == "acme"


def test_no_extract_skips_text_and_still_succeeds(settings, fake_http):
    def handler(url, **kw):
        if url.endswith(".pdf"):
            return make_response(content=b"%PDF", content_type="application/pdf")
        return make_response(text=INDEX)

    fake_http(handler)
    record = run_source("acme", "pdf_direct", _config(), settings=settings, extract=False)
    assert record.status == "ok"
    assert [s.name for s in record.steps] == ["build", "fetch"]
    assert record.documents[0].text_chars == 0


def test_date_comes_from_the_title_before_the_body(settings, fake_http, monkeypatch):
    """Minutes open by approving the previous meeting, so the first date in
    the body is routinely the wrong one. The link text is not."""
    def handler(url, **kw):
        if url.endswith(".pdf"):
            return make_response(content=b"%PDF", content_type="application/pdf")
        return make_response(text=INDEX)

    fake_http(handler)
    import docpipe.sources.pdf_direct as adapter

    monkeypatch.setattr(
        adapter.pdf_extract, "extract_text",
        lambda path, settings=None: (
            "Minutes of the meeting. The Board approved the minutes of "
            "May 4, 2026. " * 20, "pdfplumber",
        ),
    )
    record = run_source("acme", "pdf_direct", _config(), settings=settings, limit=1)
    assert record.status == "ok"
    assert record.documents[0].doc_date == "2026-06-15"
    assert record.documents[0].date_source == "title"
