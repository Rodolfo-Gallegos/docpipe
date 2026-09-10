"""JS-rendered pages: run a real browser, then scrape what it produced.

Use when the raw HTML is an empty shell and the content arrives via XHR
after JS runs. Do NOT reach for this first. It costs 5-15 seconds and
1-5 MB per page, where a JSON endpoint costs milliseconds. Check the
network tab for an API and try `json_api` before paying for a browser.

It also will not defeat a serious WAF. If the origin answers your
datacenter IP with a 403 no matter what, you need a residential proxy or a
hosted browser, not this.

Flow:
  1. Launch Chromium with automation flags masked.
  2. Navigate, wait per `wait_strategy`.
  3. Optionally follow one link by its visible text (for landing pages that
     index documents behind a per-year sub-page).
  4. Either collect file links from the rendered DOM and download each, or
     capture the rendered HTML as a single document.

Needs `pip install 'docpipe[browser]'` plus `playwright install chromium`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import Field

from docpipe.base import BaseSource, FetchedDocument, SourceConfig
from docpipe.extract import html as html_extract
from docpipe.extract import pdf as pdf_extract
from docpipe.logger import get_logger
from docpipe.registry import register_source
from docpipe import http as _http

logger = get_logger(__name__)

WaitStrategy = Literal["domcontentloaded", "networkidle", "selector"]

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]

# JS evaluated in the page. Kept as constants so the Python stays readable.
_JS_FIND_LINK_BY_TEXT = (
    '(els, kw) => { const a = els.find(e => '
    '(e.textContent || "").toLowerCase().includes(kw.toLowerCase())); '
    'return a ? a.href : null; }'
)
_JS_LINKS_BY_TEXT = (
    '(els, kw) => els.filter(e => (e.textContent || "")'
    '.toLowerCase().includes(kw.toLowerCase())).map(e => e.href)'
)
_JS_PDF_LINKS = 'els => els.map(e => e.href).filter(h => /\\.pdf(\\?|$)/i.test(h))'


class PlaywrightRenderConfig(SourceConfig):
    """`page_url`: the page to render.

    `wait_strategy`:
      - `domcontentloaded` (default, fastest): wait for the DOM, then sleep
        `post_render_sleep_sec`.
      - `networkidle`: wait for the network to go quiet. Slowest, most
        complete, and prone to timing out on pages with polling widgets.
      - `selector`: wait for `wait_selector` to appear. Most precise when
        you know what the content looks like.
    `post_render_sleep_sec`: extra seconds after the strategy fires, for
        lazy-loaded below-the-fold content.
    `mode`:
      - `pdf_links` (default): collect file links and download each.
      - `dom_html`: capture the rendered HTML as one document. Cheaper; use
        when the content renders inline.
    `pdf_link_keyword`: substring filter on the file URL.
    `link_text_keyword`: match links by their visible text instead of a
        `.pdf` extension. Needed for CMSs that serve files through
        extension-less resource-manager redirects.
    `follow_link_text`: navigate to the first link whose text contains this
        before scraping. `{year}` is replaced with the current year, so
        "{year} Meetings" rolls over each January on its own.
    """

    page_url: str = Field(pattern=r"^https?://")
    wait_strategy: WaitStrategy = "domcontentloaded"
    wait_selector: Optional[str] = None
    post_render_sleep_sec: int = Field(default=4, ge=0, le=30)
    mode: Literal["pdf_links", "dom_html"] = "pdf_links"
    pdf_link_keyword: Optional[str] = None
    link_text_keyword: Optional[str] = None
    follow_link_text: Optional[str] = None


@register_source("playwright_render")
class PlaywrightRenderSource(BaseSource):
    config_model = PlaywrightRenderConfig
    config: PlaywrightRenderConfig

    def fetch_documents(self, limit: int = 3) -> List[FetchedDocument]:
        # Playwright's sync API refuses to run inside a running event loop,
        # so we own the loop here. Chromium is launched fresh per call: it
        # keeps memory bounded at the cost of ~1s of startup, which is
        # noise next to the render itself.
        return asyncio.run(self._fetch_async(limit))

    async def _fetch_async(self, limit: int) -> List[FetchedDocument]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "playwright is required. Install with: pip install 'docpipe[browser]' "
                "&& playwright install chromium"
            ) from e

        rendered_html: Optional[str] = None
        file_hrefs: list[str] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            try:
                ctx = await browser.new_context(
                    user_agent=self.settings.browser_user_agent,
                    viewport={"width": 1366, "height": 768},
                    locale="en-US",
                )
                page = await ctx.new_page()
                try:
                    await self._navigate(page, self.config.page_url)
                    await self._maybe_follow_link(page)
                    if self.config.mode == "pdf_links":
                        file_hrefs = await self._collect_links(page, limit)
                    else:
                        rendered_html = await page.content()
                except Exception as e:
                    logger.warning(f"{self.tag} playwright_render navigation failed: {e}")
            finally:
                await browser.close()

        if self.config.mode == "dom_html" and rendered_html:
            return [FetchedDocument(
                source_url=self.config.page_url,
                raw_html=rendered_html,
                content_type="html",
                fetched_at=datetime.now(timezone.utc),
            )]

        # Download over plain HTTP: the render already proved the host
        # serves us, and a browser per file buys nothing.
        documents: list[FetchedDocument] = []
        for href in file_hrefs:
            document = self._download(href)
            if document:
                documents.append(document)
        return documents

    async def _navigate(self, page, url: str) -> None:
        wait_until = (
            "networkidle" if self.config.wait_strategy == "networkidle"
            else "domcontentloaded"
        )
        await page.goto(url, timeout=30000, wait_until=wait_until)
        if self.config.wait_strategy == "selector" and self.config.wait_selector:
            await page.wait_for_selector(self.config.wait_selector, timeout=15000)
        if self.config.post_render_sleep_sec:
            await asyncio.sleep(self.config.post_render_sleep_sec)

    async def _maybe_follow_link(self, page) -> None:
        if not self.config.follow_link_text:
            return
        target = self.config.follow_link_text.replace(
            "{year}", str(datetime.now(timezone.utc).year)
        )
        href = await page.eval_on_selector_all("a[href]", _JS_FIND_LINK_BY_TEXT, target)
        if not href:
            logger.warning(
                f"{self.tag} follow_link_text {target!r} not found on {self.config.page_url}"
            )
            return
        await self._navigate(page, href)

    async def _collect_links(self, page, limit: int) -> list[str]:
        if self.config.link_text_keyword:
            hrefs = await page.eval_on_selector_all(
                "a[href]", _JS_LINKS_BY_TEXT, self.config.link_text_keyword
            )
        else:
            hrefs = await page.eval_on_selector_all("a[href]", _JS_PDF_LINKS)
            keyword = (self.config.pdf_link_keyword or "").lower()
            if keyword:
                hrefs = [h for h in hrefs if keyword in h.lower()]
        return list(dict.fromkeys(hrefs))[:limit]

    def _download(self, url: str) -> Optional[FetchedDocument]:
        result = _http.download(
            url,
            self.storage_dir(),
            self.settings,
            headers={"User-Agent": self.settings.browser_user_agent},
            require_content_type="application/pdf",
            fallback_name="document.pdf",
        )
        if result is None:
            return None
        local_path, content = result
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
        raise ValueError("PlaywrightRenderSource: document has neither PDF nor HTML")
