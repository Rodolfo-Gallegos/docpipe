"""Built-in source adapters.

Importing this package registers every built-in under its `source_type`
name, so `get_source("pdf_direct", ...)` resolves without the caller
importing the module. Registration is what the import is for; the names
are re-exported for direct use and for subclassing.
"""
from docpipe.sources.html_page import HtmlPageConfig, HtmlPageSource
from docpipe.sources.json_api import JsonApiConfig, JsonApiSource
from docpipe.sources.pdf_direct import PdfDirectConfig, PdfDirectSource
from docpipe.sources.pdf_listing import PdfListingConfig, PdfListingSource
from docpipe.sources.playwright_render import (
    PlaywrightRenderConfig,
    PlaywrightRenderSource,
)

__all__ = [
    "HtmlPageConfig", "HtmlPageSource",
    "JsonApiConfig", "JsonApiSource",
    "PdfDirectConfig", "PdfDirectSource",
    "PdfListingConfig", "PdfListingSource",
    "PlaywrightRenderConfig", "PlaywrightRenderSource",
]
