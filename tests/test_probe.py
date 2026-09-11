"""Tests for docpipe.probe, the URL classifier an agent leans on."""
import requests

from docpipe.probe import probe
from tests.conftest import make_response

PDF_INDEX = """
<html><body>
  <a href="/docs/a.pdf">March minutes</a>
  <a href="/docs/b.pdf">February minutes</a>
  <a href="/docs/c.pdf">January minutes</a>
</body></html>
"""

RESOURCE_MANAGER_INDEX = """
<html><body>
  <a href="/fs/resource-manager/view/aaa-111">6-15-2026 Board Meeting</a>
  <a href="/fs/resource-manager/view/bbb-222">6-8-2026 Work Session</a>
  <a href="/fs/resource-manager/view/ccc-333">5-11-2026 Board Meeting</a>
</body></html>
"""

DETAIL_INDEX = """
<html><body>
  <a href="/meetings/details/104">October</a>
  <a href="/meetings/details/105">November</a>
  <a href="/meetings/details/106">December</a>
</body></html>
"""

SPA_SHELL = """
<html><head>
  <script src="/a.js"></script><script src="/b.js"></script>
  <script src="/c.js"></script><script src="/d.js"></script>
  <script src="/e.js"></script><script src="/f.js"></script>
</head><body><div id="root"></div></body></html>
"""

WORDPRESS = """
<html><head>
  <link rel="https://api.w.org/" href="https://example.org/wp-json/">
</head><body><a href="/board/agenda-2026">Agenda</a></body></html>
"""


def _serve(html, link_content_type="text/html"):
    def handler(url, **kwargs):
        if "/fs/" in url or "/meetings/details/" in url or url.endswith(".pdf"):
            return make_response(text="", content_type=link_content_type)
        return make_response(text=html)
    return handler


def test_pdf_links_give_pdf_direct(fake_http, settings):
    fake_http(_serve(PDF_INDEX))
    best = probe("https://example.org/minutes", settings=settings).best
    assert best.source_type == "pdf_direct"
    assert best.confidence == "high"
    assert best.config == {"page_url": "https://example.org/minutes"}


def test_extensionless_links_are_checked_by_content_type(fake_http, settings):
    """The Finalsite case: no .pdf extension anywhere, so ask the server."""
    fake_http(_serve(RESOURCE_MANAGER_INDEX, link_content_type="application/pdf"))
    result = probe("https://example.org/minutes", settings=settings)
    best = result.best
    assert best.source_type == "pdf_direct"
    assert best.confidence == "high"
    assert best.config["pdf_link_pattern"] == "/fs/resource-manager/view/"
    assert result.stats["probed_content_type"] == "application/pdf"


def test_same_links_serving_html_are_treated_as_detail_pages(fake_http, settings):
    """Identical structure, different Content-Type, opposite conclusion."""
    fake_http(_serve(DETAIL_INDEX, link_content_type="text/html"))
    types = [c.source_type for c in probe("https://example.org/m", settings=settings).candidates]
    assert "pdf_listing" in types
    assert "pdf_direct" not in types


def test_detail_pattern_is_a_usable_regex(fake_http, settings):
    import re

    fake_http(_serve(DETAIL_INDEX))
    listing = next(
        c for c in probe("https://example.org/m", settings=settings).candidates
        if c.source_type == "pdf_listing"
    )
    pattern = listing.config["detail_link_pattern"]
    assert re.search(pattern, "/meetings/details/106").group(1) == "106"


def test_script_shell_reads_as_a_spa(fake_http, settings):
    fake_http(_serve(SPA_SHELL))
    spa = next(
        c for c in probe("https://example.org/app", settings=settings).candidates
        if c.platform == "spa"
    )
    assert spa.source_type == "playwright_render"
    assert "json_api" in spa.next_step, "must steer toward the API before the browser"


def test_wordpress_rest_api_is_detected(fake_http, settings):
    fake_http(_serve(WORDPRESS))
    best = probe("https://example.org/board", settings=settings).best
    assert best.source_type == "json_api"
    assert best.config == {
        "api_url": "https://example.org/wp-json/wp/v2/media",
        "mode": "wp_rest_media",
    }


def test_known_platform_without_an_adapter_says_so(fake_http, settings):
    fake_http(_serve("<html><body><a href='/x'>Agenda</a></body></html>"))
    result = probe("https://go.boarddocs.com/pa/x/Board.nsf/Public", settings=settings)
    assert result.platform == "boarddocs"
    best = result.best
    assert best.source_type is None
    assert "JSON API" in best.next_step


def test_unreachable_url_reports_cleanly(settings, monkeypatch):
    import docpipe.http

    monkeypatch.setattr(
        docpipe.http.requests, "get",
        lambda url, **kwargs: (_ for _ in ()).throw(requests.RequestException("refused")),
    )
    result = probe("https://example.org/gone", settings=settings)
    assert result.ok is False
    assert result.platform == "unreachable"
    assert result.candidates == []


def test_verify_promotes_the_candidate_that_actually_works(fake_http, settings):
    """The first guess returns nothing, the second returns a document. The
    working one must end up first, with the failure kept as evidence."""
    html = """
    <html><body>
      <a href="/meetings/details/104">October Minutes</a>
      <a href="/meetings/details/105">November Minutes</a>
      <a href="/meetings/details/106">December Minutes</a>
    </body></html>
    """
    body = "<html><body><main>" + ("Motion carried. " * 100) + "</main></body></html>"

    def handler(url, **kwargs):
        if "/meetings/details/" in url:
            # No PDF on the detail page, so pdf_listing comes back empty,
            # but the page itself is real content, so html_page works.
            return make_response(text=body, content_type="text/html")
        return make_response(text=html)

    fake_http(handler)
    result = probe("https://example.org/m", settings=settings, verify=True)
    assert result.best.source_type == "html_page"
    assert result.best.verified is True
    failed = next(c for c in result.candidates if c.source_type == "pdf_listing")
    assert failed.verified is False
    assert any("Promoted" in note for note in result.notes)


def test_verify_rejects_a_fetch_that_yields_no_real_text(fake_http, settings):
    html = "<html><body><a href='/a.pdf'>Minutes</a><a href='/b.pdf'>More</a>" \
           "<a href='/c.pdf'>Even more</a></body></html>"

    def handler(url, **kwargs):
        if url.endswith(".pdf"):
            return make_response(content=b"%PDF-1.4", content_type="application/pdf")
        return make_response(text=html)

    fake_http(handler)
    result = probe("https://example.org/m", settings=settings, verify=True)
    # An unparseable PDF yields no text, which is not a working source.
    assert result.best.verified is False
    assert "effectively empty" in result.best.verified_note or "extraction failed" in result.best.verified_note


# ── Escalation: what to do when the door is shut ────────────────────────


def _refuse(status=None, exc=None):
    def handler(url, **kwargs):
        if exc:
            raise exc
        response = make_response(text="blocked")
        response.status_code = status
        return response
    return handler


def test_a_403_suggests_the_hosted_browser(settings, monkeypatch):
    import docpipe.http

    def handler(url, **kwargs):
        response = make_response(text="Forbidden")
        response.status_code = 403
        return response

    monkeypatch.setattr(docpipe.http.requests, "get", handler)
    result = probe("https://example.org/docs", settings=settings)

    assert result.ok is False
    assert result.platform == "blocked"
    assert result.best.source_type == "cloud_render"
    assert result.best.config == {"page_url": "https://example.org/docs"}
    assert "proxy: true" in result.best.next_step


def test_a_timeout_suggests_a_residential_exit(settings, monkeypatch):
    import docpipe.http

    monkeypatch.setattr(
        docpipe.http.requests, "get",
        lambda url, **kw: (_ for _ in ()).throw(requests.Timeout("timed out")),
    )
    result = probe("https://example.org/docs", settings=settings)

    assert result.platform == "blocked"
    assert result.best.source_type == "cloud_render"
    assert result.best.config["proxy"] is True


def test_a_404_does_not_suggest_heavier_machinery(settings, monkeypatch):
    """A missing page is a wrong URL. No browser fixes that, and saying so
    keeps an agent from burning money on it."""
    import docpipe.http

    def handler(url, **kwargs):
        response = make_response(text="Not Found")
        response.status_code = 404
        return response

    monkeypatch.setattr(docpipe.http.requests, "get", handler)
    result = probe("https://example.org/gone", settings=settings)

    assert result.candidates == []
    assert "moved" in result.notes[0]
