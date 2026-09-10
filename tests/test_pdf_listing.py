"""Tests for the pdf_listing adapter (index -> detail -> file)."""
from docpipe import get_source
from tests.conftest import make_response

NUMERIC_INDEX = """
<html><body>
  <a href="/meetings/details/104">October</a>
  <a href="/meetings/details/106">December</a>
  <a href="/meetings/details/105">November</a>
  <a href="/about">Not a meeting</a>
</body></html>
"""

SLUG_INDEX = """
<html><body>
  <a href="/board-meetings/december-2026/">December</a>
  <a href="/board-meetings/november-2026/">November</a>
</body></html>
"""

DETAIL = """
<html><body>
  <a href="/files/agenda-{id}.pdf">Agenda</a>
  <a href="/files/minutes-{id}.pdf">Minutes</a>
</body></html>
"""


def _routes(index_html, detail_html=DETAIL):
    def handler(url, **kwargs):
        if url.endswith(".pdf"):
            return make_response(content=b"%PDF-1.4 fake", content_type="application/pdf")
        if "/details/" in url or "/board-meetings/" in url:
            item_id = url.rstrip("/").rsplit("/", 1)[-1]
            return make_response(text=detail_html.replace("{id}", item_id))
        return make_response(text=index_html)
    return handler


def _source(settings, **config):
    config.setdefault("index_url", "https://example.org/meetings")
    config.setdefault("detail_link_pattern", r"/meetings/details/(\d+)")
    return get_source("pdf_listing", "acme", config, settings=settings)


def test_numeric_ids_sort_newest_first(settings, fake_http):
    fake_http(_routes(NUMERIC_INDEX))
    docs = _source(settings).fetch_documents(limit=2)
    assert [d.source_url for d in docs] == [
        "https://example.org/files/agenda-106.pdf",
        "https://example.org/files/agenda-105.pdf",
    ]


def test_slug_ids_preserve_dom_order(settings, fake_http):
    """Non-numeric ids cannot be sorted, and every CMS renders its index
    newest-first, so DOM order is the best available signal."""
    fake_http(_routes(SLUG_INDEX))
    docs = _source(
        settings,
        detail_link_pattern=r"/board-meetings/([a-z0-9-]+)/",
    ).fetch_documents(limit=2)
    assert [d.source_url for d in docs] == [
        "https://example.org/files/agenda-december-2026.pdf",
        "https://example.org/files/agenda-november-2026.pdf",
    ]


def test_keyword_picks_the_right_file_on_the_detail_page(settings, fake_http):
    fake_http(_routes(NUMERIC_INDEX))
    docs = _source(settings, pdf_link_keyword="minutes").fetch_documents(limit=1)
    assert docs[0].source_url.endswith("minutes-106.pdf")


def test_detail_page_without_a_file_is_skipped(settings, fake_http):
    fake_http(_routes(NUMERIC_INDEX, detail_html="<html><body>No attachments yet.</body></html>"))
    assert _source(settings).fetch_documents(limit=3) == []


def test_index_without_detail_links_returns_empty(settings, fake_http):
    fake_http(_routes("<html><body><a href='/about'>About</a></body></html>"))
    assert _source(settings).fetch_documents(limit=3) == []


def test_html_served_as_a_pdf_link_is_rejected(settings, fake_http):
    """A CMS answering a .pdf link with an error page must not leave a
    corrupt file on disk for the extractor to choke on."""
    def handler(url, **kwargs):
        if url.endswith(".pdf"):
            return make_response(text="<html>404</html>", content_type="text/html")
        if "/details/" in url:
            return make_response(text=DETAIL.replace("{id}", "106"))
        return make_response(text=NUMERIC_INDEX)

    fake_http(handler)
    assert _source(settings).fetch_documents(limit=1) == []
