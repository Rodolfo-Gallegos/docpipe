"""Tests for the html_page adapter."""
from docpipe import get_source
from tests.conftest import make_response

INDEX_HTML = """
<html><body>
  <a href="/board/minutes/march-2026">March Minutes</a>
  <a href="/board/minutes/february-2026">February Minutes</a>
  <a href="/board/minutes/january-2026.pdf">January Minutes (PDF)</a>
  <a href="/contact">Contact us</a>
</body></html>
"""

PAGE_HTML = "<html><body><main>Motion carried unanimously.</main></body></html>"


def _routes(index_html=INDEX_HTML):
    def handler(url, **kwargs):
        return make_response(text=PAGE_HTML if "/minutes/" in url else index_html)
    return handler


def _source(settings, **config):
    config.setdefault("page_url", "https://example.org/board")
    return get_source("html_page", "acme", config, settings=settings)


def test_follows_matching_links_and_skips_attachments(settings, fake_http):
    fake_http(_routes())
    docs = _source(settings).fetch_documents(limit=10)
    assert len(docs) == 2, "the .pdf link belongs to the PDF adapters"
    assert all(d.content_type == "html" for d in docs)
    assert all(d.raw_html == PAGE_HTML for d in docs)


def test_matches_on_link_text_too(settings, fake_http):
    fake_http(_routes())
    docs = _source(settings, link_pattern="february").fetch_documents(limit=10)
    assert len(docs) == 1
    assert docs[0].source_url.endswith("february-2026")


def test_falls_back_to_the_index_page_itself(settings, fake_http):
    """Small sites keep everything on one page. Returning nothing there
    would look like a broken source."""
    fake_http(_routes(index_html=PAGE_HTML))
    docs = _source(settings, link_pattern="nothing-matches-this").fetch_documents(limit=3)
    assert len(docs) == 1
    assert docs[0].source_url == "https://example.org/board"


def test_to_text_strips_chrome(settings, fake_http):
    fake_http(_routes())
    source = _source(settings)
    text, method = source.to_text(source.fetch_documents(limit=1)[0])
    assert method == "html"
    assert text == "Motion carried unanimously."
