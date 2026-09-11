"""Sites that expose a JSON listing endpoint.

The cheapest source there is: no HTML parsing, no browser, one request.
Whenever a site has one of these behind its UI, prefer it over scraping.

Two response shapes are built in:

- **metadata_items** (default): an object wrapping a list, e.g.
  `{"items": [...]}`. Items carry text metadata but usually no attachment,
  so the adapter synthesizes one pseudo-document per item out of the
  configured fields. Which fields, and what they are labeled, is config.

- **wp_rest_media**: the flat array WordPress returns from
  `/wp-json/wp/v2/media`. Each item points at a real file through
  `source_url`, so the adapter downloads and extracts the PDFs directly.
  Present on any WordPress site, which is a large share of the public web.

`items_key` and `text_fields` cover most other JSON listings without new
code: point `items_key` at the array and label the fields you want.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Literal, Optional

from pydantic import Field

from docpipe.base import BaseSource, FetchedDocument, SourceConfig
from docpipe.extract import html as html_extract
from docpipe.extract import pdf as pdf_extract
from docpipe.extract.dates import parse_iso_prefix
from docpipe.logger import get_logger
from docpipe.registry import register_source
from docpipe import http as _http

logger = get_logger(__name__)

# Labels and dotted paths for the default metadata_items shape.
DEFAULT_TEXT_FIELDS: dict[str, str] = {
    "ITEM ID": "id",
    "DATE": "date",
    "DEPARTMENT": "department",
    "CATEGORY": "category.title",
    "LOCATION": "location",
    "CONTACT": "contact",
    "SUBJECT": "subject",
}


class JsonApiConfig(SourceConfig):
    """`api_url`: the listing endpoint, without query params.

    `query`: literal query string appended to `api_url`, `?` included.
        Defaults differ per mode (newest-first paging in both cases).
    `mode`: response shape, see the module docstring.
    `items_key`: key holding the array in metadata_items mode. Set to None
        when the endpoint returns a bare array.
    `text_fields`: label -> dotted path, in the order they should appear in
        the synthesized document.
    `date_field`, `id_field`: dotted paths used for the document date and
        its source URL suffix.
    `filter_path` / `filter_value`: optional client-side equality filter,
        e.g. "category.key" == "board-meetings". Server-side category
        filters on these endpoints are frequently broken, so we filter here.
    `pdf_url_field`: in wp_rest_media mode, the item key holding the file
        URL.
    """

    api_url: str = Field(pattern=r"^https?://")
    query: Optional[str] = None
    mode: Literal["metadata_items", "wp_rest_media"] = "metadata_items"

    items_key: Optional[str] = "items"
    text_fields: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_TEXT_FIELDS))
    date_field: str = "date"
    id_field: str = "id"
    filter_path: Optional[str] = "category.key"
    filter_value: Optional[str] = None

    pdf_url_field: str = "source_url"


def _dig(item: dict, path: str) -> Any:
    """Resolve a dotted path against nested dicts. Missing -> None."""
    current: Any = item
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


@register_source("json_api")
class JsonApiSource(BaseSource):
    config_model = JsonApiConfig
    config: JsonApiConfig

    def fetch_documents(self, limit: int = 3) -> List[FetchedDocument]:
        if self.config.mode == "wp_rest_media":
            return self._fetch_wp_rest_media(limit)
        return self._fetch_metadata_items(limit)

    def _get_json(self, default_query: str) -> Any:
        url = self.config.api_url + (self.config.query or default_query)
        logger.info(f"{self.tag} JsonApi ({self.config.mode}): {url}")
        response = _http.get(url, self.settings, headers={"Accept": "application/json"})
        if response is None:
            return None
        try:
            return response.json()
        except ValueError as e:
            logger.error(f"{self.tag} JsonApi: response was not JSON: {e}")
            return None

    # ── Mode: metadata_items ───────────────────────────────────────────

    def _fetch_metadata_items(self, limit: int) -> List[FetchedDocument]:
        data = self._get_json("?limit=10&skip=0&sortBy=date&sortDesc=true")
        if data is None:
            return []

        if self.config.items_key is None:
            items = data
        else:
            items = data.get(self.config.items_key) if isinstance(data, dict) else None
        if not isinstance(items, list):
            logger.warning(f"{self.tag} JsonApi: unexpected shape, no item list")
            return []

        if self.config.filter_value and self.config.filter_path:
            items = [
                it for it in items
                if _dig(it, self.config.filter_path) == self.config.filter_value
            ]

        if not items:
            logger.info(f"{self.tag} JsonApi: 0 items after filter")
            return []

        documents: list[FetchedDocument] = []
        for item in items[:limit]:
            item_id = _dig(item, self.config.id_field) or "unknown"
            documents.append(FetchedDocument(
                source_url=f"{self.config.api_url}/{item_id}",
                raw_html=self._synthesize_html(item),
                doc_date=parse_iso_prefix(_dig(item, self.config.date_field)),
                content_type="html",
                fetched_at=datetime.now(timezone.utc),
            ))
        return documents

    def _synthesize_html(self, item: dict) -> str:
        """Render one item's fields as a text document.

        Wrapped in <pre> so it flows through the same HTML extractor as
        every other document instead of needing a third content type.
        """
        lines: list[str] = []
        for label, path in self.config.text_fields.items():
            value = _dig(item, path)
            value = "" if value is None else str(value).strip()
            if "\n" in value or len(value) > 120:
                lines.extend(["", f"{label}:", value])
            else:
                lines.append(f"{label}: {value}")
        return "<html><body><pre>" + "\n".join(lines) + "</pre></body></html>"

    # ── Mode: wp_rest_media ────────────────────────────────────────────

    def _fetch_wp_rest_media(self, limit: int) -> List[FetchedDocument]:
        data = self._get_json(
            "?mime_type=application/pdf&per_page=10&orderby=date&order=desc"
        )
        if data is None:
            return []
        if not isinstance(data, list):
            logger.warning(f"{self.tag} wp_rest_media: expected a list, got {type(data)}")
            return []

        logger.info(f"{self.tag} wp_rest_media: {len(data)} items from API")
        dest = self.storage_dir()

        documents: list[FetchedDocument] = []
        for item in data:
            if len(documents) >= limit:
                break
            pdf_url = str(item.get(self.config.pdf_url_field) or "")
            if not pdf_url.lower().endswith(".pdf"):
                continue
            result = _http.download(
                pdf_url, dest, self.settings, fallback_name="document.pdf"
            )
            if result is None:
                continue
            local_path, content = result
            documents.append(FetchedDocument(
                source_url=pdf_url,
                local_path=local_path,
                raw_content=content,
                doc_date=parse_iso_prefix(item.get(self.config.date_field)),
                content_type="pdf",
                fetched_at=datetime.now(timezone.utc),
            ))
        return documents

    def to_text(self, document: FetchedDocument) -> tuple[str, str]:
        if document.content_type == "pdf" and document.local_path:
            return pdf_extract.extract_text(document.local_path, self.settings)
        if document.raw_html:
            return html_extract.extract_text(document.raw_html), "html"
        raise ValueError("JsonApiSource: no extractable content on document")
