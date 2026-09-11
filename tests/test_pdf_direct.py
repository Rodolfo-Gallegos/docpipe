"""Tests for the pdf_direct adapter."""
from datetime import date

from docpipe import get_source
from tests.conftest import make_response

INDEX_HTML = """
<html><body>
  <a href="/docs/2026-03-16-minutes.pdf">March minutes</a>
  <a href="/docs/2026-03-16-minutes.pdf">March minutes (duplicate link)</a>
  <a href="/docs/2026-02-10-minutes.pdf">February minutes</a>
  <a href="/archive/2019-01-01-minutes.pdf">Archived minutes</a>
  <a href="/about">Not a document</a>
</body></html>
"""


def _routes(index_html=INDEX_HTML):
    def handler(url, **kwargs):
        if url.endswith(".pdf"):
            return make_response(content=b"%PDF-1.4 fake", content_type="application/pdf")
        return make_response(text=index_html)
    return handler


def _source(settings, **config):
    config.setdefault("page_url", "https://example.org/board/minutes")
    return get_source("pdf_direct", "acme", config, settings=settings)


def test_collects_and_dedupes_pdf_links(settings, fake_http):
    fake_http(_routes())
    docs = _source(settings).fetch_documents(limit=10)

    urls = [d.source_url for d in docs]
    assert len(urls) == len(set(urls)), "the duplicate link must be collapsed"
    assert len(docs) == 3
    assert all(d.content_type == "pdf" for d in docs)


def test_reads_the_date_out_of_the_file_name(settings, fake_http):
    fake_http(_routes())
    docs = _source(settings).fetch_documents(limit=1)
    assert docs[0].doc_date == date(2026, 3, 16)


def test_exclude_pattern_drops_matching_links(settings, fake_http):
    fake_http(_routes())
    docs = _source(settings, pdf_exclude_pattern="/archive/").fetch_documents(limit=10)
    assert len(docs) == 2
    assert not any("/archive/" in d.source_url for d in docs)


def test_link_pattern_replaces_the_extension_check(settings, fake_http):
    fake_http(_routes())
    docs = _source(settings, pdf_link_pattern="/archive/").fetch_documents(limit=10)
    assert len(docs) == 1
    assert "/archive/" in docs[0].source_url


def test_limit_caps_downloads(settings, fake_http):
    fake_http(_routes())
    assert len(_source(settings).fetch_documents(limit=2)) == 2


def test_files_land_under_the_configured_raw_dir(settings, fake_http):
    fake_http(_routes())
    docs = _source(settings).fetch_documents(limit=1)
    assert docs[0].local_path.parent == settings.raw_dir / "acme"
    assert docs[0].local_path.read_bytes() == b"%PDF-1.4 fake"


def test_unreachable_index_returns_empty(settings, monkeypatch):
    import requests

    import docpipe.http

    def boom(url, **kwargs):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(docpipe.http.requests, "get", boom)
    assert _source(settings).fetch_documents(limit=3) == []
