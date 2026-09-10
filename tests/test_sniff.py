"""Tests for docpipe.sniff.

The JSON-shape logic is pure and tested directly. The browser path runs
against a local HTTP server, so it exercises the real Playwright listener
without depending on any third-party site staying up. It skips when
chromium is not installed.
"""
import http.server
import json
import threading
from functools import partial

import pytest

from docpipe.sniff import _describe_json, _suggest, Call, SniffResult

# ── Pure logic ──────────────────────────────────────────────────────────


def test_describes_a_bare_array():
    shape, count, keys, listing = _describe_json([{"id": 1, "title": "a"}, {"id": 2}])
    assert (shape, count, listing) == ("array", 2, True)
    assert keys == ["id", "title"]


def test_describes_a_wrapped_array():
    shape, count, keys, listing = _describe_json({"items": [{"id": 1}, {"id": 2}], "total": 2})
    assert shape == "object with 'items' array"
    assert (count, listing) == (2, True)


def test_a_single_object_is_not_a_listing():
    _, count, _, listing = _describe_json({"id": 1, "name": "one thing"})
    assert (count, listing) == (0, False)


def test_suggestion_splits_url_and_query():
    result = SniffResult(url="https://example.org/app")
    result.calls = [Call(
        method="GET",
        url="https://example.org/api/notices?page=1&size=20",
        json_shape="object with 'items' array",
        item_count=20,
        item_keys=["id", "date", "subject"],
        looks_like_listing=True,
    )]
    _suggest(result)
    assert result.suggestion["config"] == {
        "api_url": "https://example.org/api/notices",
        "query": "?page=1&size=20",
        "items_key": "items",
    }


def test_a_post_listing_is_flagged_as_unsupported():
    """json_api only issues GET, and saying so beats a config that silently
    fetches the wrong thing."""
    result = SniffResult(url="https://example.org/app")
    result.calls = [Call(
        method="POST", url="https://example.org/bd-api/list",
        json_shape="array", item_count=5, looks_like_listing=True,
    )]
    _suggest(result)
    assert any("POST" in note for note in result.notes)


def test_a_stub_response_is_called_out_as_bot_blocking():
    result = SniffResult(url="https://example.org/app")
    result.calls = [Call(method="GET", url="https://example.org/app", status=200)]
    _suggest(result)
    assert any("automated browser" in note for note in result.notes)


# ── Real browser ────────────────────────────────────────────────────────

SPA_PAGE = b"""<!doctype html>
<html><head><title>Demo</title></head>
<body><div id="root">loading</div>
<script>
  fetch('/api/notices?page=1')
    .then(r => r.json())
    .then(d => { document.getElementById('root').textContent = d.items.length; });
</script>
</body></html>
"""

API_BODY = json.dumps({
    "items": [
        {"id": 1, "date": "2026-03-01", "subject": "Notice one"},
        {"id": 2, "date": "2026-02-01", "subject": "Notice two"},
    ],
    "total": 2,
}).encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/"):
            # Mislabeled on purpose: real APIs do this constantly, and the
            # sniffer must not filter on Content-Type alone.
            body, content_type = API_BODY, "text/html"
        else:
            body, content_type = SPA_PAGE, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_spa():
    server = http.server.HTTPServer(("127.0.0.1", 0), partial(_Handler))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


@pytest.mark.browser
def test_finds_the_api_behind_a_local_spa(local_spa):
    playwright = pytest.importorskip("playwright.sync_api")
    from docpipe.sniff import sniff

    try:
        result = sniff(local_spa, wait_seconds=3)
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("chromium not installed (playwright install chromium)")
        raise

    assert result.suggestion is not None, result.notes
    assert result.suggestion["config"]["api_url"].endswith("/api/notices")
    assert result.suggestion["config"]["items_key"] == "items"
    assert result.suggestion["item_keys"] == ["date", "id", "subject"]
    assert playwright  # silence the unused-name lint
