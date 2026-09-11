"""docpipe: fetch documents from web sources, turn them into clean text.

Two halves that are useful together and separately.

**Sources** retrieve documents from an origin. Pick the cheapest adapter
that works: a JSON endpoint beats HTML scraping, and HTML scraping beats
driving a browser.

    from docpipe import Settings, get_source

    source = get_source(
        "pdf_direct",
        "acme-corp",
        {"page_url": "https://example.org/reports", "pdf_link_pattern": "/annual/"},
        settings=Settings(raw_dir=Path("/var/tmp/scrapes")),
    )
    for doc in source.fetch_documents(limit=5):
        text, method = source.to_text(doc)

**Extractors** turn bytes into text, and text into something an LLM can
afford to read. They work on any file, with or without a source:

    from docpipe.extract import pdf, dates, truncate

    text, method = pdf.extract_text("report.pdf")
    when = dates.parse_date_from_text(text)
    fitted, meta = truncate.truncate_smart(text, max_chars=800_000)

Register your own adapter with `@register_source("name")` and it resolves
through `get_source` alongside the built-ins.
"""
from docpipe.base import BaseSource, FetchedDocument, SourceConfig
from docpipe.registry import (
    available_sources,
    get_source,
    register_source,
    source_class,
)
from docpipe.settings import DEFAULT_SETTINGS, Settings

# Registers the built-in adapters as a side effect of import.
from docpipe import sources as sources  # noqa: E402,F401

__version__ = "0.3.0"

__all__ = [
    "BaseSource",
    "FetchedDocument",
    "SourceConfig",
    "Settings",
    "DEFAULT_SETTINGS",
    "get_source",
    "register_source",
    "available_sources",
    "source_class",
    "__version__",
]
