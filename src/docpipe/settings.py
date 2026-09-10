"""Runtime knobs shared by every source and extractor.

The original pipeline read these from a module-level `config.py`, which made
the adapters unusable outside that one project. Here they live in a plain
value object that callers pass in (or leave at the defaults):

    from docpipe import Settings, get_source

    settings = Settings(raw_dir=Path("/var/tmp/scrapes"), http_timeout=60)
    source = get_source("pdf_direct", "acme", cfg, settings=settings)

`Settings` is immutable. Use `replace()` to derive a variant:

    slow = settings.replace(http_timeout=120)
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DEFAULT_HTTP_HEADERS: Mapping[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

# Chrome UA used by the rendering sources, so the follow-up file download
# presents the same identity the browser did.
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Settings:
    """Configuration shared across sources and extractors.

    raw_dir
        Where downloaded files land. Each source writes to
        `raw_dir / source_id /`. Created on demand, never at import time.
    http_timeout
        Per-request timeout in seconds.
    http_headers
        Sent on every plain HTTP request.
    browser_user_agent
        UA for rendered sessions and their follow-up downloads.
    max_filename_chars
        Downloaded names are sanitized to `[A-Za-z0-9._-]` and cut here.
    ocr_enabled
        When False, `extract.pdf` returns whatever the text layer yields and
        never rasterizes. Turn off in environments without tesseract/poppler.
    ocr_min_text_chars
        Below this many characters, a text-layer result counts as a failure
        and the next strategy runs.
    ocr_max_pages, ocr_per_page_timeout, ocr_rasterize_timeout
        Hard caps so one pathological scan cannot hang a batch run.
    """

    raw_dir: Path = Path("data/raw")
    http_timeout: int = 30
    http_headers: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_HTTP_HEADERS)
    )
    browser_user_agent: str = DEFAULT_BROWSER_USER_AGENT
    max_filename_chars: int = 200

    ocr_enabled: bool = True
    ocr_min_text_chars: int = 100
    ocr_max_pages: int = 30
    ocr_per_page_timeout: int = 30
    ocr_rasterize_timeout: int = 120

    def __post_init__(self) -> None:
        # Accept a string path without forcing every caller to wrap it.
        if not isinstance(self.raw_dir, Path):
            object.__setattr__(self, "raw_dir", Path(self.raw_dir))

    def replace(self, **changes) -> "Settings":
        """Return a copy with `changes` applied."""
        return dataclasses.replace(self, **changes)

    def storage_dir(self, source_id: str) -> Path:
        """Per-source download directory, created if missing."""
        target = self.raw_dir / source_id
        target.mkdir(parents=True, exist_ok=True)
        return target


DEFAULT_SETTINGS = Settings()
