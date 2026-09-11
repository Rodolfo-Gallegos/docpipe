"""Tests for the last step of the escalation: hosted browser and blocked."""
import pytest

from docpipe import get_source
from docpipe.sources.cloud_render import CloudSessionError, open_session


def test_blocked_source_fetches_nothing_but_keeps_the_record(settings):
    source = get_source(
        "blocked", "acme",
        {
            "origin_url": "https://portal.example.gov/docs",
            "reason": "login_required",
            "detail": "Behind an account request form approved by a human.",
        },
        settings=settings,
    )
    assert source.fetch_documents(limit=5) == []
    assert source.config.origin_url == "https://portal.example.gov/docs"


def test_blocked_reason_drives_the_suggested_next_step(settings):
    """A WAF block is worth retrying with a hosted browser; a captcha is not.
    The record has to say which, or it is just a shrug."""
    waf = get_source("blocked", "waf-site",
                     {"origin_url": "https://a.example", "reason": "waf"},
                     settings=settings)
    assert "cloud_render" in waf.next_step

    ip_blocked = get_source("blocked", "ip-site",
                            {"origin_url": "https://b.example", "reason": "ip_blocked"},
                            settings=settings)
    assert "proxy: true" in ip_blocked.next_step

    captcha = get_source("blocked", "captcha-site",
                         {"origin_url": "https://c.example", "reason": "captcha"},
                         settings=settings)
    assert "human" in captcha.next_step


def test_blocked_to_text_refuses_clearly(settings):
    source = get_source("blocked", "acme", {"origin_url": "https://a.example"},
                        settings=settings)
    with pytest.raises(ValueError, match="never produces documents"):
        source.to_text(None)


def test_cloud_session_without_credentials_says_what_is_missing(monkeypatch):
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    monkeypatch.delenv("BROWSERBASE_PROJECT_ID", raising=False)
    with pytest.raises(CloudSessionError, match="BROWSERBASE_API_KEY"):
        open_session()


def test_cloud_render_without_credentials_returns_empty_not_a_crash(settings, monkeypatch):
    """A missing key must not take down a batch run: the adapter logs and
    yields nothing, like every other expected failure."""
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    monkeypatch.delenv("BROWSERBASE_PROJECT_ID", raising=False)
    pytest.importorskip("playwright.sync_api")

    source = get_source("cloud_render", "acme", {"page_url": "https://a.example"},
                        settings=settings)
    assert source.fetch_documents(limit=1) == []


def test_cloud_session_reports_a_bad_response(monkeypatch):
    import docpipe.sources.cloud_render as cloud

    monkeypatch.setenv("BROWSERBASE_API_KEY", "k")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "p")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "abc"}   # no connectUrl

    monkeypatch.setattr(cloud.requests, "post", lambda *a, **kw: Response())
    with pytest.raises(CloudSessionError, match="connectUrl"):
        open_session()


def test_proxy_flag_reaches_the_session_request(monkeypatch):
    import docpipe.sources.cloud_render as cloud

    monkeypatch.setenv("BROWSERBASE_API_KEY", "k")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "p")
    sent = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "s1", "connectUrl": "wss://example"}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(json or {})
        return Response()

    monkeypatch.setattr(cloud.requests, "post", fake_post)

    open_session(proxy=False)
    assert "proxies" not in sent, "a plain session must not be billed as a proxy session"

    open_session(proxy=True)
    assert sent["proxies"] is True
    assert sent["projectId"] == "p"
