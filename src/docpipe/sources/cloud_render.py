"""Render in a hosted browser, for origins that refuse your own.

The last step of the escalation, and the only one that costs money per
page. Reach for it when, and only when, the cheaper steps have failed for
a reason this actually fixes:

    json_api          milliseconds, no browser            always try first
    pdf_direct/html   one request                         server-rendered
    playwright_render 5-15s, local Chromium               content needs JS
    cloud_render      10-30s, someone else's browser      you are blocked

Two distinct blocks, one adapter:

- **A WAF challenge** (Akamai, Cloudflare): the origin serves your local
  Chromium a 403 or an interstitial. A hosted browser with a maintained
  anti-bot fingerprint passes where a stock headless browser does not.
- **An IP block**: the origin refuses datacenter ranges outright, with a
  403 or a TCP timeout, and no fingerprint helps. That needs a different
  exit IP, which is what `proxy: true` buys.

If the page is merely JS-heavy and nobody is blocking you, this is a waste
of money: use `playwright_render`, or better, find the API with
`docpipe sniff`.

Downloads go through the page's own `fetch()` rather than a separate HTTP
request. That is the whole trick: the file request then carries the same
session, cookies, IP and fingerprint that just passed the challenge. A
plain `requests.get` from your machine would be blocked exactly like the
original page was.

Setup: `pip install 'docpipe[cloud]'`, then set `BROWSERBASE_API_KEY` and
`BROWSERBASE_PROJECT_ID`. Nothing here reads those at import time, so the
module is safe to import without them.
"""
from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from typing import Any, List, Literal, Optional

import requests
from pydantic import Field

from docpipe.base import BaseSource, FetchedDocument, SourceConfig
from docpipe.extract import html as html_extract
from docpipe.extract import pdf as pdf_extract
from docpipe.http import sanitize_filename
from docpipe.logger import get_logger
from docpipe.registry import register_source

logger = get_logger(__name__)

SESSIONS_ENDPOINT = "https://api.browserbase.com/v1/sessions"

# Collect every anchor that points at a file, with its visible text, so the
# keyword filters can match on either.
_JS_COLLECT_LINKS = """
    Array.from(document.querySelectorAll("a[href]"))
      .map(e => [e.href, (e.innerText || "").slice(0, 200)])
"""

# Fetch a file from inside the page and hand the bytes back base64-encoded.
# Chunked so a large PDF does not blow the argument limit of
# String.fromCharCode.
_JS_FETCH_FILE = """
    (async (url) => {
        const r = await fetch(url, {credentials: 'include'});
        if (!r.ok) return {status: r.status, body: ''};
        const bytes = new Uint8Array(await r.arrayBuffer());
        let binary = '';
        const CHUNK = 0x8000;
        for (let i = 0; i < bytes.length; i += CHUNK) {
            binary += String.fromCharCode.apply(
                null, bytes.subarray(i, i + CHUNK)
            );
        }
        return {status: r.status, body: btoa(binary), size: bytes.length,
                content_type: r.headers.get('content-type') || ''};
    })
"""


class CloudRenderConfig(SourceConfig):
    """`page_url`: the page to render.

    `proxy`: route the session through a residential exit IP. Needed when
        the origin blocks datacenter ranges outright (a 403 or a TCP
        timeout that a local browser also gets). Costs more per page, so
        leave it off until a run without it fails.
    `mode`:
      - `file_links` (default): collect links and download each.
      - `dom_html`: capture the rendered text as one document. Use when the
        content renders inline.
    `link_pattern`: substring the file URL must contain.
    `link_keyword`: substring matched against the URL *or* the link text.
        Use when the CMS serves files through extension-less URLs.
    `wait_ms`: how long to let the page hydrate after DOMContentLoaded.
    `pre_action_js`: JavaScript evaluated before scraping, for pages that
        need a click to reveal the list. Runs in the page, so keep it to a
        one-liner like `document.querySelector('#tab-2').click()`.
    """

    page_url: str = Field(pattern=r"^https?://")
    proxy: bool = False
    mode: Literal["file_links", "dom_html"] = "file_links"
    link_pattern: Optional[str] = None
    link_keyword: Optional[str] = None
    wait_ms: int = Field(default=5000, ge=0, le=60000)
    pre_action_js: Optional[str] = None


class CloudSessionError(RuntimeError):
    """The hosted browser could not be started. Message says why."""


def open_session(proxy: bool = False, timeout: int = 60) -> tuple[str, str]:
    """Create a Browserbase session. Returns (session_id, connect_url).

    Uses the REST API rather than the vendor SDK: one less dependency for
    a single POST, and the failure modes stay visible.
    """
    api_key = os.getenv("BROWSERBASE_API_KEY")
    project_id = os.getenv("BROWSERBASE_PROJECT_ID")
    if not api_key or not project_id:
        raise CloudSessionError(
            "BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID must be set. "
            "Get both at browserbase.com, then export them."
        )

    payload: dict[str, Any] = {"projectId": project_id}
    if proxy:
        payload["proxies"] = True

    try:
        response = requests.post(
            SESSIONS_ENDPOINT,
            headers={"X-BB-API-Key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        raise CloudSessionError(f"Could not start a cloud browser session: {e}") from e
    except ValueError as e:
        raise CloudSessionError(f"Session endpoint did not return JSON: {e}") from e

    connect_url = data.get("connectUrl")
    if not connect_url:
        raise CloudSessionError(
            f"Session response carried no connectUrl: {list(data)}"
        )
    return data.get("id", ""), connect_url


@register_source("cloud_render")
class CloudRenderSource(BaseSource):
    config_model = CloudRenderConfig
    config: CloudRenderConfig

    def fetch_documents(self, limit: int = 3) -> List[FetchedDocument]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "playwright is required. Install with: pip install 'docpipe[cloud]'"
            ) from e

        try:
            session_id, connect_url = open_session(proxy=self.config.proxy)
        except CloudSessionError as e:
            logger.error(f"{self.tag} {e}")
            return []

        logger.info(
            f"{self.tag} cloud session {session_id or '(unnamed)'} "
            f"({'residential proxy' if self.config.proxy else 'default exit'})"
        )

        documents: list[FetchedDocument] = []
        with sync_playwright() as p:
            browser = None
            try:
                browser = p.chromium.connect_over_cdp(connect_url)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()

                page.goto(self.config.page_url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(self.config.wait_ms)

                if self.config.pre_action_js:
                    try:
                        page.evaluate(self.config.pre_action_js)
                        page.wait_for_timeout(self.config.wait_ms)
                    except Exception as e:
                        logger.warning(f"{self.tag} pre_action_js failed: {e}")

                if self.config.mode == "dom_html":
                    document = self._capture_dom(page)
                    if document:
                        documents.append(document)
                else:
                    for href in self._collect_links(page)[:limit]:
                        document = self._download_in_page(page, href)
                        if document:
                            documents.append(document)
            except Exception as e:
                logger.error(f"{self.tag} cloud render failed: {e}")
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass

        logger.info(f"{self.tag} cloud render returned {len(documents)} document(s)")
        return documents

    def _collect_links(self, page) -> list[str]:
        try:
            found = page.evaluate(_JS_COLLECT_LINKS) or []
        except Exception as e:
            logger.warning(f"{self.tag} could not read links: {e}")
            return []

        pattern = (self.config.link_pattern or "").lower()
        keyword = (self.config.link_keyword or "").lower()
        matched: list[str] = []
        seen: set[str] = set()
        for href, text in found:
            haystack_url = (href or "").lower()
            if not haystack_url:
                continue
            if pattern and pattern not in haystack_url:
                continue
            if keyword:
                if keyword not in haystack_url and keyword not in (text or "").lower():
                    continue
            elif not pattern and ".pdf" not in haystack_url:
                # No filter given: fall back to the extension, the same
                # default the plain HTTP adapters use.
                continue
            if href in seen:
                continue
            seen.add(href)
            matched.append(href)

        logger.info(f"{self.tag} {len(matched)} matching link(s)")
        return matched

    def _capture_dom(self, page) -> Optional[FetchedDocument]:
        """Rendered text, preferred over raw HTML.

        `innerText` is what the browser actually shows, which sidesteps the
        HTML extractor's chrome filters. Some grid widgets live in elements
        whose class names collide with that blocklist, and stripping them
        would drop the very content we came for.
        """
        rendered = ""
        try:
            rendered = page.inner_text("body")
        except Exception:
            pass
        if not rendered or len(rendered) < 200:
            try:
                rendered = page.content() or ""
            except Exception:
                return None
        if not rendered:
            return None

        return FetchedDocument(
            source_url=self.config.page_url,
            raw_html=f"<html><body><pre>{rendered}</pre></body></html>",
            content_type="html",
            fetched_at=datetime.now(timezone.utc),
        )

    def _download_in_page(self, page, url: str) -> Optional[FetchedDocument]:
        try:
            result = page.evaluate(_JS_FETCH_FILE, url)
        except Exception as e:
            logger.warning(f"{self.tag} in-page fetch failed for {url}: {e}")
            return None

        if not result or result.get("status") != 200:
            status = (result or {}).get("status")
            logger.warning(f"{self.tag} {url} returned status {status}")
            return None

        content_type = (result.get("content_type") or "").lower()
        if "pdf" not in content_type:
            logger.info(f"{self.tag} skipping {url}: Content-Type {content_type!r}")
            return None

        try:
            content = base64.b64decode(result["body"])
        except Exception as e:
            logger.warning(f"{self.tag} could not decode {url}: {e}")
            return None

        destination = self.storage_dir()
        filename = sanitize_filename(
            url.rsplit("/", 1)[-1],
            max_chars=self.settings.max_filename_chars,
            fallback="document.pdf",
        )
        local_path = destination / filename
        local_path.write_bytes(content)
        logger.info(f"{self.tag} downloaded {filename} ({len(content)} bytes)")

        return FetchedDocument(
            source_url=url,
            local_path=local_path,
            raw_content=content,
            content_type="pdf",
            fetched_at=datetime.now(timezone.utc),
        )

    def to_text(self, document: FetchedDocument) -> tuple[str, str]:
        if document.content_type == "pdf" and document.local_path:
            return pdf_extract.extract_text(document.local_path, self.settings)
        if document.raw_html:
            return html_extract.extract_text(document.raw_html), "html"
        raise ValueError("CloudRenderSource: document has neither file nor HTML")
