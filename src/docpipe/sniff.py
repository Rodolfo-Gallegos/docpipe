"""Watch a page's network traffic and report the JSON API behind it.

The single most valuable move when a page turns out to be a SPA: almost
every one of them is a thin shell over an endpoint that returns exactly
the list you want, in JSON, with no rendering and no browser. Finding that
endpoint turns a 10-second render into a 200ms request.

Doing it by hand means opening devtools and reading the network tab. Doing
it in an agent used to mean writing a throwaway Playwright script every
time. This is that script, once, with the output shaped for a decision:
which endpoint returns the most list-like JSON, and what a `json_api`
config against it would look like.

Needs `pip install 'docpipe[browser]'` and `playwright install chromium`.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from docpipe.logger import get_logger
from docpipe.settings import DEFAULT_SETTINGS, Settings

logger = get_logger(__name__)

# Response bodies larger than this are summarized, not stored.
_MAX_BODY_PREVIEW = 4000
_IGNORED_SUFFIXES = (
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff",
    ".woff2", ".ttf", ".ico", ".webp", ".mp4",
)


@dataclass
class Call:
    method: str
    url: str
    status: Optional[int] = None
    content_type: str = ""
    bytes: int = 0
    json_shape: Optional[str] = None
    item_count: int = 0
    item_keys: list[str] = field(default_factory=list)
    looks_like_listing: bool = False
    preview: Optional[str] = None


@dataclass
class SniffResult:
    url: str
    calls: list[Call] = field(default_factory=list)
    suggestion: Optional[dict[str, Any]] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _describe_json(payload: Any) -> tuple[str, int, list[str], bool]:
    """(shape, item count, keys of the first item, is this a listing)."""
    if isinstance(payload, list):
        first = payload[0] if payload else None
        keys = sorted(first)[:12] if isinstance(first, dict) else []
        return "array", len(payload), keys, len(payload) >= 2

    if isinstance(payload, dict):
        # The common wrapper: one key holding the array that matters.
        for key, value in payload.items():
            if isinstance(value, list) and len(value) >= 2:
                first = value[0]
                keys = sorted(first)[:12] if isinstance(first, dict) else []
                return f"object with '{key}' array", len(value), keys, True
        return "object", 0, sorted(payload)[:12], False

    return type(payload).__name__, 0, [], False


def sniff(
    url: str,
    settings: Optional[Settings] = None,
    wait_seconds: int = 8,
    include_all: bool = False,
) -> SniffResult:
    """Load `url` in Chromium and report the data calls it makes."""
    settings = settings or DEFAULT_SETTINGS
    return asyncio.run(_sniff_async(url, settings, wait_seconds, include_all))


async def _sniff_async(
    url: str, settings: Settings, wait_seconds: int, include_all: bool
) -> SniffResult:
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(
            "playwright is required. Install with: pip install 'docpipe[browser]' "
            "&& playwright install chromium"
        ) from e

    result = SniffResult(url=url)
    pending: list[Call] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(user_agent=settings.browser_user_agent)
            page = await ctx.new_page()

            async def on_response(response):
                request_url = response.url
                if not include_all and request_url.lower().split("?")[0].endswith(_IGNORED_SUFFIXES):
                    return

                content_type = (response.headers.get("content-type") or "").lower()
                try:
                    body = await response.text()
                except Exception:
                    return

                # Do not trust Content-Type. Plenty of real APIs answer JSON
                # labeled text/html or text/xml (anything on Domino, most
                # legacy .NET handlers), and filtering on the header alone
                # hides exactly the endpoints worth finding. Look at the body.
                stripped = body.lstrip()
                is_json = "json" in content_type or stripped[:1] in ("{", "[")
                if not is_json and not include_all:
                    return

                call = Call(
                    method=response.request.method,
                    url=request_url,
                    status=response.status,
                    content_type=content_type,
                    bytes=len(body),
                )
                if is_json:
                    try:
                        payload = json.loads(body)
                    except ValueError:
                        call.preview = body[:200]
                        pending.append(call)
                        return
                    shape, count, keys, listing = _describe_json(payload)
                    call.json_shape = shape
                    call.item_count = count
                    call.item_keys = keys
                    call.looks_like_listing = listing
                    call.preview = json.dumps(payload)[:_MAX_BODY_PREVIEW]
                else:
                    call.preview = body[:200]
                pending.append(call)

            # Each response is read in its own task; hold on to them so the
            # browser is not closed out from under a body that is still
            # being read, which silently drops calls.
            tasks: list[asyncio.Task] = []
            page.on("response", lambda r: tasks.append(asyncio.create_task(on_response(r))))

            try:
                await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                await asyncio.sleep(wait_seconds)
            except Exception as e:
                result.notes.append(f"Navigation problem: {e}")

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await browser.close()

    # Biggest listing first: that is almost always the one that matters.
    result.calls = sorted(
        pending, key=lambda c: (c.looks_like_listing, c.item_count), reverse=True
    )
    _suggest(result)
    return result


def _suggest(result: SniffResult) -> None:
    listing = next((c for c in result.calls if c.looks_like_listing), None)
    if listing is None:
        if len(result.calls) <= 2:
            # A real page pulls in stylesheets, scripts and images. One or
            # two responses means we were served a stub: the site is
            # refusing automated browsers, and no amount of waiting fixes it.
            result.notes.append(
                "The page made almost no requests, which usually means it "
                "detected the automated browser and served a stub. Try "
                "fetching it with plain HTTP (`docpipe probe`), or drive a "
                "real browser session by hand and read the network tab."
            )
        elif result.calls:
            result.notes.append(
                "JSON calls were seen but none returned a list. The data may "
                "arrive over a websocket, be paginated behind a POST body, or "
                "need a session cookie. Re-run with --include-all to see "
                "every response."
            )
        else:
            result.notes.append(
                "No JSON traffic at all. The page is probably server-rendered: "
                "run `docpipe probe` on it instead."
            )
        return

    base_url = listing.url.split("?")[0]
    query = listing.url[len(base_url):] or None
    config: dict[str, Any] = {"api_url": base_url}
    if query:
        config["query"] = query
    if listing.json_shape and listing.json_shape.startswith("object with"):
        config["items_key"] = listing.json_shape.split("'")[1]
    else:
        config["items_key"] = None

    result.suggestion = {
        "source_type": "json_api",
        "config": config,
        "why": (
            f"{listing.method} {base_url} returned {listing.item_count} items "
            f"as {listing.json_shape}"
        ),
        "item_keys": listing.item_keys,
        "next_step": (
            "Map the item keys onto text_fields / date_field / id_field, then "
            "`docpipe validate json_api --config ...`. If the items link to "
            "files, point pdf_url_field at that key and set mode accordingly. "
            "If the call needs headers or a POST body, this adapter cannot "
            "send them: write one with @register_source."
        ),
    }

    if listing.method != "GET":
        result.notes.append(
            f"The listing call is a {listing.method}, and json_api only issues "
            "GET requests. You will need a custom adapter."
        )
