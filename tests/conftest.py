"""Shared fixtures.

`fake_http` replaces `requests.get` inside docpipe.http, which is the one
place every adapter goes through for network access.
"""
from unittest.mock import MagicMock

import pytest

from docpipe.settings import Settings


def make_response(text: str = "", json_data=None, content: bytes = b"", content_type: str = ""):
    response = MagicMock()
    response.status_code = 200
    response.text = text
    response.content = content
    response.json.return_value = json_data
    response.headers = {"Content-Type": content_type}
    response.url = ""
    response.raise_for_status.return_value = None
    return response


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings that download into an isolated temp directory."""
    return Settings(raw_dir=tmp_path)


@pytest.fixture
def fake_http(monkeypatch):
    """Route every docpipe HTTP GET to a routing function you supply.

        def routes(url, **kwargs):
            return make_response(text="<html>...</html>")
        fake_http(routes)
    """
    def install(handler):
        import docpipe.http

        def wrapped(url, **kwargs):
            response = handler(url, **kwargs)
            if response is None:
                raise AssertionError(f"test made an unexpected request to {url}")
            response.url = response.url or url
            return response

        monkeypatch.setattr(docpipe.http.requests, "get", wrapped)
    return install
