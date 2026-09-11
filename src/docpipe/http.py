"""HTTP helpers shared by the adapters.

Every adapter used to repeat the same download-and-save block, each with
its own subtly different filename sanitizing and content-type check. This
is that block, once.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping, Optional

import requests

from docpipe.logger import get_logger
from docpipe.settings import Settings

logger = get_logger(__name__)

_UNSAFE_FILENAME_CHARS = re.compile(r"[^a-zA-Z0-9._-]")


def sanitize_filename(name: str, max_chars: int = 200, fallback: str = "document") -> str:
    """Make a URL segment safe to write to disk."""
    name = name.split("?")[0].split("#")[0]
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)[:max_chars]
    return name or fallback


def attempt(
    url: str,
    settings: Settings,
    headers: Optional[Mapping[str, str]] = None,
    **kwargs,
) -> tuple[Optional[requests.Response], Optional[int], Optional[str]]:
    """GET that reports how it failed. Returns (response, status, error).

    `get` swallows failures, which is right for an adapter mid-run: one bad
    link should not stop a batch. Diagnosis needs the opposite, because a
    403 and a DNS failure mean completely different things: one says a
    hosted browser might get through, the other says the URL is wrong.
    """
    try:
        response = requests.get(
            url,
            headers={**settings.http_headers, **(headers or {})},
            timeout=settings.http_timeout,
            **kwargs,
        )
    except requests.Timeout as e:
        return None, None, f"timeout after {settings.http_timeout}s: {e}"
    except requests.ConnectionError as e:
        return None, None, f"connection failed: {e}"
    except requests.RequestException as e:
        return None, None, str(e)

    if response.status_code >= 400:
        return None, response.status_code, f"HTTP {response.status_code}"
    return response, response.status_code, None


def get(
    url: str,
    settings: Settings,
    headers: Optional[Mapping[str, str]] = None,
    **kwargs,
) -> Optional[requests.Response]:
    """GET with the configured headers and timeout. None on any failure."""
    try:
        response = requests.get(
            url,
            headers={**settings.http_headers, **(headers or {})},
            timeout=settings.http_timeout,
            **kwargs,
        )
        response.raise_for_status()
        return response
    except requests.RequestException as e:
        logger.warning(f"GET failed {url}: {e}")
        return None


def download(
    url: str,
    dest_dir: Path,
    settings: Settings,
    headers: Optional[Mapping[str, str]] = None,
    require_content_type: Optional[str] = None,
    fallback_name: str = "document",
) -> Optional[tuple[Path, bytes]]:
    """Download `url` into `dest_dir`. Returns (path, bytes) or None.

    `require_content_type` is a substring check against the response's
    Content-Type: a CMS that answers a `.pdf` link with an HTML error page
    should not produce a file on disk that later fails PDF parsing with a
    confusing error.

    The file name comes from the URL *after* redirects, so a CDN's
    descriptive name wins over an opaque resource-manager UUID.
    """
    response = get(url, settings, headers=headers)
    if response is None:
        return None

    if require_content_type:
        actual = (response.headers.get("Content-Type") or "").lower()
        if require_content_type.lower() not in actual:
            logger.info(f"Skipping {url}: Content-Type {actual!r}")
            return None

    final_url = getattr(response, "url", None) or url
    filename = sanitize_filename(
        final_url.rsplit("/", 1)[-1],
        max_chars=settings.max_filename_chars,
        fallback=fallback_name,
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / filename
    path.write_bytes(response.content)
    logger.info(f"Downloaded {filename} ({len(response.content)} bytes)")
    return path, response.content
