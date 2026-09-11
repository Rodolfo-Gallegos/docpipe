"""Classify a URL: which adapter should read it, with what config.

This is the step an agent runs right after it finds a candidate URL. It is
deliberately LLM-free. The agent already knows how to search the web; what
it cannot do reliably is guess an adapter and its config from prose. That
part is mechanical, so we do it mechanically and hand back evidence the
agent can check.

The result is a ranked list of candidates. Each one carries:

- `source_type`: the adapter to use, or None when the platform is
  recognized but no built-in adapter handles it (write one, or register
  your own).
- `config`: ready to pass to `get_source`, not a sketch.
- `confidence`: high / medium / low.
- `evidence`: why, in plain strings, so a human or an agent can audit the
  call instead of trusting it.
- `next_step`: what to do when the answer is "no adapter fits", e.g. sniff
  the page's XHR traffic for a JSON API.

Nothing here is authoritative. `probe(url, verify=True)` actually runs the
top candidate for one document and reports whether it produced anything,
which is the only real answer.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from docpipe import http as _http
from docpipe.logger import get_logger
from docpipe.settings import DEFAULT_SETTINGS, Settings

logger = get_logger(__name__)

# Platforms we can name from the host alone. Naming one is useful even
# without an adapter: it tells the agent what it is dealing with and
# whether a documented API exists.
_KNOWN_HOSTS: tuple[tuple[str, str], ...] = (
    ("boarddocs.com", "boarddocs"),
    ("diligent.com", "boarddocs"),
    ("granicus.com", "granicus"),
    ("legistar.com", "legistar"),
    ("civicclerk.com", "civicclerk"),
    ("civicplus.com", "civicplus"),
    ("swagit.com", "swagit"),
    ("boardbook.org", "boardbook"),
    ("novusagenda.com", "novusagenda"),
    ("iqm2.com", "iqm2"),
)

# Hints for platforms with no built-in adapter. These save an agent the
# discovery round-trip: several of them have undocumented public APIs.
_PLATFORM_HINTS: dict[str, str] = {
    "boarddocs": (
        "BoardDocs exposes an undocumented public JSON API on every site "
        "(POST to /bd-api/... with the committee id). No browser needed. "
        "Write an adapter against it rather than rendering the page."
    ),
    "granicus": (
        "Granicus ViewPublisher pages are server-rendered HTML tables with "
        "direct file links. A pdf_direct or pdf_listing config often works; "
        "check whether the links carry a .pdf extension."
    ),
    "legistar": (
        "Legistar has a documented public REST API at webapi.legistar.com "
        "(no key for public bodies). Prefer it over scraping Calendar.aspx."
    ),
    "civicclerk": (
        "CivicClerk is a SPA over a JSON API. Sniff the XHR traffic for the "
        "endpoint, then use json_api instead of rendering."
    ),
}

_DOC_LINK_WORDS = (
    "minutes", "agenda", "notice", "report", "packet", "resolution",
    "bid", "tender", "rfp", "filing", "transcript", "meeting", "session",
    "hearing", "budget", "contract", "proceedings",
)

# Below this, a "successful" extraction is not worth reporting as one.
MIN_USEFUL_TEXT_CHARS = 500

# A page this thin with this many scripts is a shell that fills itself in.
_SPA_MAX_TEXT_CHARS = 600
_SPA_MIN_SCRIPTS = 5


@dataclass
class Candidate:
    source_type: Optional[str]
    platform: str
    config: dict[str, Any] = field(default_factory=dict)
    confidence: str = "low"
    evidence: list[str] = field(default_factory=list)
    next_step: Optional[str] = None
    verified: Optional[bool] = None
    verified_note: Optional[str] = None


@dataclass
class ProbeResult:
    url: str
    final_url: str
    ok: bool
    platform: str
    candidates: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def best(self) -> Optional[Candidate]:
        return self.candidates[0] if self.candidates else None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("best", None)
        return data


_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _slug_shape(path: str) -> str:
    """Collapse a path to its shape: /meetings/details/106 -> /meetings/details/<n>.

    Used to spot a set of links that are the same route with a varying id,
    which is what an index of detail pages looks like.
    """
    parts = []
    for segment in path.strip("/").split("/"):
        if not segment:
            continue
        if segment.isdigit():
            parts.append("<n>")
        elif re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", segment, re.IGNORECASE):
            parts.append("<slug>")
        else:
            parts.append(segment)
    return "/" + "/".join(parts)


def _shape_to_pattern(shape: str) -> str:
    """Turn a shape back into a regex for `detail_link_pattern`.

    Built segment by segment rather than by escaping the whole string and
    substituting: re.escape leaves `<` and `>` untouched, so the naive
    version silently produced a pattern that matched nothing.

    Only the last placeholder captures. `pdf_listing` reads group(1) as the
    item id, and in a shape like /fs/<slug>/view/<slug> the id is the last
    segment, not the first.
    """
    segments = [seg for seg in shape.strip("/").split("/") if seg]
    last_placeholder = max(
        (i for i, seg in enumerate(segments) if seg in ("<n>", "<slug>")),
        default=-1,
    )
    parts: list[str] = []
    for i, segment in enumerate(segments):
        if segment == "<n>":
            body = r"\d+"
        elif segment == "<slug>":
            body = r"[a-zA-Z0-9-]+"
        else:
            parts.append(re.escape(segment))
            continue
        parts.append(f"({body})" if i == last_placeholder else f"(?:{body})")
    return "/" + "/".join(parts)


def probe(
    url: str,
    settings: Optional[Settings] = None,
    verify: bool = False,
) -> ProbeResult:
    """Fetch `url` and rank the adapters that could read it.

    `verify=True` runs the top candidate for a single document and records
    whether it actually produced one. Slower, and the only answer that
    counts.
    """
    settings = settings or DEFAULT_SETTINGS
    response, status, error = _http.attempt(url, settings)
    if response is None:
        return _unreachable(url, status, error)

    final_url = getattr(response, "url", None) or url
    html = response.text or ""
    soup = BeautifulSoup(html, "lxml")
    platform = _platform_from_host(final_url)

    links = _collect_links(soup, final_url)
    text = soup.get_text(" ", strip=True)
    stats = {
        "http_status": getattr(response, "status_code", None),
        "html_bytes": len(html),
        "visible_text_chars": len(text),
        "script_tags": len(soup.find_all("script")),
        "total_links": len(links),
        "pdf_links": sum(1 for href, _ in links if href.lower().split("?")[0].endswith(".pdf")),
    }

    result = ProbeResult(
        url=url, final_url=final_url, ok=True, platform=platform, stats=stats
    )

    _add_wordpress_candidate(result, soup, final_url, settings)
    _add_pdf_direct_candidate(result, links, final_url)
    _add_extensionless_pdf_candidate(result, links, final_url, settings)
    _add_pdf_listing_candidate(result, links, final_url, stats)
    _add_html_page_candidate(result, links, final_url)
    _add_spa_candidate(result, stats, final_url)
    _add_platform_candidate(result, platform, final_url)

    result.candidates.sort(key=lambda c: _CONFIDENCE_RANK.get(c.confidence, 3))

    if not result.candidates:
        result.notes.append(
            "Nothing matched. Open the page and check the network tab: if the "
            "content arrives over XHR, find that endpoint and use json_api."
        )

    if verify and result.candidates:
        _verify(result, settings)

    return result


def _unreachable(url: str, status: Optional[int], error: Optional[str]) -> ProbeResult:
    """Turn a failed fetch into a diagnosis and, where one exists, a way out.

    The distinction that matters: a block is a door you may still open with
    a different browser or a different IP, while a 404 or a DNS failure
    means the URL itself is wrong and no amount of tooling helps.
    """
    result = ProbeResult(
        url=url, final_url=url, ok=False, platform="unreachable",
        stats={"http_status": status, "error": error},
    )

    if status in (401, 403, 406, 429) or status == 503:
        result.platform = "blocked"
        result.notes.append(
            f"The origin answered {status} rather than refusing the connection, "
            "so the server is up and is turning us away. Open the page in a "
            "normal browser: if it loads there, this is a bot or IP block, not "
            "a bad URL."
        )
        result.candidates.append(Candidate(
            source_type="cloud_render",
            platform="blocked",
            config={"page_url": url},
            confidence="medium",
            evidence=[f"plain HTTP request returned {status}"],
            next_step=(
                "A hosted browser passes most WAF challenges. Set "
                "BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID, then try "
                "this config. If it still fails, add proxy: true to get a "
                "residential exit IP, which is what an outright datacenter "
                "ban needs. If that fails too, record it with source_type "
                "\"blocked\" so nobody re-investigates it in six months."
            ),
        ))
        return result

    if status == 404:
        result.notes.append(
            "The server is up and says this page does not exist. The document "
            "archive most likely moved: search the site for it rather than "
            "reaching for a heavier adapter."
        )
        return result

    if error and ("timeout" in error or "connection failed" in error):
        result.platform = "blocked"
        result.notes.append(
            f"No response at all ({error}). Either the host is down, or it "
            "drops traffic from datacenter ranges without answering, which "
            "looks identical from here. Check whether the page loads from a "
            "home connection."
        )
        result.candidates.append(Candidate(
            source_type="cloud_render",
            platform="blocked",
            config={"page_url": url, "proxy": True},
            confidence="low",
            evidence=[error],
            next_step=(
                "proxy: true routes through a residential IP, which is the "
                "only thing that helps if the origin is dropping datacenter "
                "traffic. If the host is simply down, this will fail too, so "
                "confirm the page loads somewhere before paying for it."
            ),
        ))
        return result

    result.notes.append(f"The URL did not respond: {error or 'unknown error'}.")
    return result


def _platform_from_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for needle, name in _KNOWN_HOSTS:
        if needle in host:
            return name
    return "unknown"


def _collect_links(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    """(absolute href, visible text) for every link, deduped, same-page order."""
    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if href in seen:
            continue
        seen.add(href)
        links.append((href, a.get_text(" ", strip=True)))
    return links


def _add_wordpress_candidate(
    result: ProbeResult, soup: BeautifulSoup, url: str, settings: Settings
) -> None:
    """WordPress announces its REST API in a <link rel>. That is a free
    JSON endpoint listing every uploaded file, no scraping required."""
    tag = soup.find("link", rel=lambda v: v and "api.w.org" in " ".join(v if isinstance(v, list) else [v]))
    api_root = tag.get("href") if tag else None
    if not api_root and "/wp-content/" in (soup.decode() if len(soup.decode()) < 2_000_000 else ""):
        api_root = urljoin(url, "/wp-json/")
    if not api_root:
        return

    media_url = urljoin(api_root.rstrip("/") + "/", "wp/v2/media")
    result.candidates.append(Candidate(
        source_type="json_api",
        platform="wordpress",
        config={"api_url": media_url, "mode": "wp_rest_media"},
        confidence="high",
        evidence=[
            f"The page advertises a WordPress REST API at {api_root}",
            "wp/v2/media lists every uploaded file as JSON, newest first",
        ],
        next_step=(
            "Confirm the media endpoint returns PDFs: "
            f"curl -s '{media_url}?mime_type=application/pdf&per_page=3'"
        ),
    ))


def _add_pdf_direct_candidate(
    result: ProbeResult, links: list[tuple[str, str]], url: str
) -> None:
    pdf_links = [href for href, _ in links if href.lower().split("?")[0].endswith(".pdf")]
    if not pdf_links:
        return

    confidence = "high" if len(pdf_links) >= 3 else "medium"
    evidence = [f"{len(pdf_links)} links on the page end in .pdf"]

    # A shared parent path is a good exclude/include hint for the agent.
    parents = Counter(href.rsplit("/", 1)[0] for href in pdf_links)
    common_parent, count = parents.most_common(1)[0]
    if count >= 3 and len(parents) > 1:
        evidence.append(f"{count} of them sit under {common_parent}")

    result.candidates.append(Candidate(
        source_type="pdf_direct",
        platform=result.platform if result.platform != "unknown" else "generic_html",
        config={"page_url": url},
        confidence=confidence,
        evidence=evidence,
        next_step=(
            "If the page mixes documents you do not want, narrow it with "
            "pdf_link_pattern or pdf_exclude_pattern."
        ) if len(parents) > 1 else None,
    ))


def _add_pdf_listing_candidate(
    result: ProbeResult, links: list[tuple[str, str]], url: str, stats: dict
) -> None:
    """Detect an index of per-item detail pages (no files on this page)."""
    if stats["pdf_links"] >= 3:
        return  # pdf_direct already covers it
    if any(c.source_type == "pdf_direct" and c.confidence == "high" for c in result.candidates):
        return  # we asked, and those links serve files, not detail pages

    base_host = urlparse(url).hostname
    shapes: Counter[str] = Counter()
    for href, _ in links:
        parsed = urlparse(href)
        if parsed.hostname != base_host or not parsed.path.strip("/"):
            continue
        shape = _slug_shape(parsed.path)
        if "<n>" in shape or "<slug>" in shape:
            shapes[shape] += 1

    if not shapes:
        return
    shape, count = shapes.most_common(1)[0]
    if count < 3:
        return

    result.candidates.append(Candidate(
        source_type="pdf_listing",
        platform=result.platform if result.platform != "unknown" else "generic_html",
        config={
            "index_url": url,
            "detail_link_pattern": _shape_to_pattern(shape),
        },
        confidence="medium",
        evidence=[
            f"{count} links share the route shape {shape}, which reads as an "
            "index of per-item detail pages",
            f"only {stats['pdf_links']} direct .pdf links on this page",
        ],
        next_step=(
            "Open one detail page and confirm it carries the file. If several "
            "files hang off it, set pdf_link_keyword to pick the right one."
        ),
    ))


def _link_groups(links: list[tuple[str, str]], base_url: str) -> list[tuple[str, list[str]]]:
    """Group same-site links by route shape, largest group first.

    A page that lists documents produces one big group of links that are
    the same route with a varying id. That structural signal is far more
    reliable than any keyword list, because it does not care what language
    the site is in or what the links are called.
    """
    base_host = urlparse(base_url).hostname
    groups: dict[str, list[str]] = {}
    for href, _ in links:
        parsed = urlparse(href)
        if parsed.hostname != base_host or not parsed.path.strip("/"):
            continue
        shape = _slug_shape(parsed.path)
        if "<n>" not in shape and "<slug>" not in shape:
            continue
        groups.setdefault(shape, []).append(href)
    return sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)


def _content_type_of(url: str, settings: Settings) -> Optional[str]:
    """Ask what a link actually serves. One request, body not read."""
    response = _http.get(url, settings, stream=True)
    if response is None:
        return None
    content_type = (response.headers.get("Content-Type") or "").lower()
    response.close()
    return content_type


def _add_extensionless_pdf_candidate(
    result: ProbeResult, links: list[tuple[str, str]], url: str, settings: Settings
) -> None:
    """Catch documents served without a .pdf extension.

    Finalsite, Blackboard and friends serve files through a resource
    manager: /fs/resource-manager/view/<uuid>, no extension, the type
    visible only in the response header. An extension check misses every
    one of them, and the page then looks like an index of detail pages
    when it is really a list of documents.

    So we ask. One request against the largest link group settles it, and
    the answer also tells `pdf_listing` whether it is looking at detail
    pages or at the files themselves.
    """
    if any(c.source_type == "pdf_direct" and c.confidence == "high" for c in result.candidates):
        return  # the extension check already found plenty

    groups = _link_groups(links, url)
    probes: list[tuple[str, list[str]]] = [(shape, hrefs) for shape, hrefs in groups if len(hrefs) >= 3][:1]

    # Fall back to keyword-looking links when there is no dominant group.
    if not probes:
        worded = [
            href for href, text in links
            if not urlparse(href).path.lower().endswith((".pdf", ".doc", ".docx"))
            and any(word in f"{href} {text}".lower() for word in _DOC_LINK_WORDS)
        ]
        if not worded:
            return
        probes = [("", worded)]

    for shape, hrefs in probes:
        content_type = _content_type_of(hrefs[0], settings)
        if not content_type:
            continue
        result.stats["probed_link"] = hrefs[0]
        result.stats["probed_content_type"] = content_type
        if "application/pdf" not in content_type:
            continue

        prefix = _common_path_prefix([urlparse(h).path for h in hrefs])
        config: dict[str, Any] = {"page_url": url}
        if prefix and prefix != "/":
            config["pdf_link_pattern"] = prefix

        evidence = [
            f"{len(hrefs)} links share the route shape {shape or '(keyword match)'}",
            f"the first one answers with Content-Type {content_type}, so these "
            "are the documents themselves, not detail pages",
            f"example: {hrefs[0]}",
        ]
        if prefix and prefix != "/":
            evidence.append(f"they share the path prefix {prefix}")

        result.candidates.append(Candidate(
            source_type="pdf_direct",
            platform=result.platform if result.platform != "unknown" else "generic_html",
            config=config,
            confidence="high",
            evidence=evidence,
            next_step=(
                "This CMS hides the extension behind a resource manager. "
                "pdf_link_pattern matches on the URL, so widen or narrow it if "
                "the page mixes document types."
            ),
        ))
        return


def _common_path_prefix(paths: list[str]) -> str:
    """Longest shared leading run of path segments, e.g. /fs/resource-manager/view/."""
    if not paths:
        return ""
    split = [[seg for seg in path.split("/") if seg] for path in paths]
    shared: list[str] = []
    for parts in zip(*split):
        if len(set(parts)) == 1:
            shared.append(parts[0])
        else:
            break
    return "/" + "/".join(shared) + "/" if shared else ""


def _add_html_page_candidate(
    result: ProbeResult, links: list[tuple[str, str]], url: str
) -> None:
    """Documents published as HTML pages rather than attachments."""
    matches: Counter[str] = Counter()
    for href, text in links:
        haystack = f"{href} {text}".lower()
        if href.lower().split("?")[0].endswith((".pdf", ".doc", ".docx")):
            continue
        for word in _DOC_LINK_WORDS:
            if word in haystack:
                matches[word] += 1
                break

    if not matches:
        return
    word, count = matches.most_common(1)[0]
    if count < 2:
        return

    result.candidates.append(Candidate(
        source_type="html_page",
        platform=result.platform if result.platform != "unknown" else "generic_html",
        config={"page_url": url, "link_pattern": word},
        confidence="medium" if count >= 4 else "low",
        evidence=[f"{count} non-attachment links mention {word!r}"],
        next_step="Check that those links open the document itself, not a landing page.",
    ))


def _add_spa_candidate(result: ProbeResult, stats: dict, url: str) -> None:
    is_shell = (
        stats["visible_text_chars"] < _SPA_MAX_TEXT_CHARS
        and stats["script_tags"] >= _SPA_MIN_SCRIPTS
    )
    if not is_shell:
        return

    result.candidates.append(Candidate(
        source_type="playwright_render",
        platform="spa",
        config={"page_url": url, "wait_strategy": "networkidle", "mode": "pdf_links"},
        confidence="medium",
        evidence=[
            f"only {stats['visible_text_chars']} chars of visible text behind "
            f"{stats['script_tags']} script tags: the content arrives after JS runs",
        ],
        next_step=(
            "Before paying for a browser, sniff the page's XHR traffic for the "
            "JSON endpoint it calls and use json_api instead. Rendering costs "
            "5 to 15 seconds per page; the API costs milliseconds."
        ),
    ))


def _add_platform_candidate(result: ProbeResult, platform: str, url: str) -> None:
    """Name a known platform, even when no built-in adapter handles it.

    Knowing you are looking at BoardDocs or Legistar is worth more than any
    generic guess: both have public APIs, and a generic HTML adapter over
    them produces a page of navigation chrome. So when we recognize the
    host and have no solid adapter, the platform itself is the answer, and
    the next step is to build against its API.
    """
    if platform == "unknown":
        return

    hint = _PLATFORM_HINTS.get(
        platform,
        f"No built-in adapter for {platform}. Inspect the network traffic for "
        "a JSON API, then write an adapter and register it with "
        "@register_source.",
    )

    # A verified-looking generic adapter beats a platform note, so demote
    # the hint to a note rather than competing with it.
    if any(c.source_type and c.confidence == "high" for c in result.candidates):
        result.notes.append(f"The host identifies this as {platform}. {hint}")
        return

    result.candidates.append(Candidate(
        source_type=None,
        platform=platform,
        config={},
        confidence="high",
        evidence=[
            f"the host identifies this as {platform}",
            "no built-in adapter covers it, and a generic HTML adapter over "
            "this platform mostly returns navigation chrome",
        ],
        next_step=hint,
    ))


def _verify(result: ProbeResult, settings: Settings, max_attempts: int = 3) -> None:
    """Run candidates in order until one produces a document.

    An agent would try the next guess when the first one comes back empty,
    so the tool does it too. The first candidate that works is promoted to
    the front; the ones that failed keep their note, which is the evidence
    for why they were wrong.
    """
    import tempfile

    for index, candidate in enumerate(result.candidates[:max_attempts]):
        if not candidate.source_type:
            candidate.verified = False
            candidate.verified_note = "No built-in adapter to run."
            continue

        with tempfile.TemporaryDirectory() as tmp:
            _verify_one(candidate, settings.replace(raw_dir=tmp))

        if candidate.verified:
            if index:
                result.candidates.insert(0, result.candidates.pop(index))
                result.notes.append(
                    f"Promoted {candidate.source_type} over the higher-ranked "
                    "guesses: it is the one that actually returned a document."
                )
            return

    result.notes.append(
        "No candidate returned a document. Open the page in a browser and "
        "watch the network tab: if the list arrives over XHR, find that "
        "endpoint and use json_api. If the page renders server-side but the "
        "links are odd, narrow the config by hand and re-run `docpipe fetch`."
    )


def _verify_one(candidate: Candidate, settings: Settings) -> None:
    from docpipe.registry import get_source

    try:
        source = get_source(
            candidate.source_type, "probe", candidate.config, settings=settings
        )
        documents = source.fetch_documents(limit=1)
    except Exception as e:
        candidate.verified = False
        candidate.verified_note = f"{type(e).__name__}: {e}"
        return

    if not documents:
        candidate.verified = False
        candidate.verified_note = (
            "The adapter ran but found no documents. The config needs "
            "narrowing, or this is the wrong adapter."
        )
        return

    document = documents[0]
    try:
        text, method = source.to_text(document)
    except Exception as e:
        candidate.verified = False
        candidate.verified_note = (
            f"Fetched {document.source_url} but extraction failed: {e}"
        )
        return

    # A fetch that yields a handful of characters is a fetch that worked
    # and a source that did not: a cover page, a "no records" stub, or a
    # scan with no text layer and OCR off. Calling that verified would send
    # an agent off with a source that produces nothing.
    if len(text) < MIN_USEFUL_TEXT_CHARS:
        candidate.verified = False
        candidate.verified_note = (
            f"Fetched {document.source_url} but only extracted {len(text)} chars "
            f"via {method}, which is effectively empty. If the document is a "
            "scan, install the ocr extra; otherwise this is the wrong link."
        )
        return

    candidate.verified = True
    candidate.verified_note = (
        f"Fetched {document.source_url} and extracted {len(text)} chars via {method}."
    )
