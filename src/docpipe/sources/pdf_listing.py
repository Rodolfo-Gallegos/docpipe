"""Two-step listing: index page -> per-item detail page -> PDF.

    GET <index_url>
      -> hrefs matching <detail_link_pattern>, newest first
    GET <detail_url> for each of the newest N
      -> the first PDF link on that page
    GET <pdf_url>

Differs from `pdf_direct`, which expects the PDFs on the index itself.

Ordering: if the pattern's capture group parses as an integer, items sort
by it descending (numeric ids grow monotonically). Otherwise DOM order is
kept, since virtually every CMS renders its index newest-first.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pydantic import Field

from docpipe.base import BaseSource, FetchedDocument, SourceConfig
from docpipe.extract import pdf as pdf_extract
from docpipe.logger import get_logger
from docpipe.registry import register_source
from docpipe import http as _http

logger = get_logger(__name__)


class PdfListingConfig(SourceConfig):
    """`index_url`: the page listing every item.

    `detail_link_pattern`: regex with one capture group identifying the
        detail links. Examples:
          r"/meetings/details/(\\d+)"        numeric id, sorted descending
          r"/board-meetings/([a-z0-9-]+)/"   slug, DOM order preserved
    `pdf_link_keyword`: optional substring filter for picking the PDF on a
        detail page that offers several. Matched against href and link text.
    """

    index_url: str = Field(pattern=r"^https?://")
    detail_link_pattern: str = Field(min_length=1)
    pdf_link_keyword: Optional[str] = None


@register_source("pdf_listing")
class PdfListingSource(BaseSource):
    config_model = PdfListingConfig
    config: PdfListingConfig

    def fetch_documents(self, limit: int = 3) -> List[FetchedDocument]:
        index_url = self.config.index_url
        detail_re = re.compile(self.config.detail_link_pattern)
        logger.info(f"{self.tag} PdfListing index: {index_url}")

        response = _http.get(index_url, self.settings)
        if response is None:
            logger.error(f"{self.tag} PdfListing index failed")
            return []

        soup = BeautifulSoup(response.text, "lxml")
        dom_ordered: list[tuple[Optional[int], str]] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            match = detail_re.search(a["href"])
            if not match:
                continue
            full = urljoin(index_url, a["href"])
            if full in seen:
                continue
            seen.add(full)
            try:
                key: Optional[int] = int(match.group(1))
            except (ValueError, IndexError):
                key = None
            dom_ordered.append((key, full))

        if not dom_ordered:
            logger.warning(f"{self.tag} PdfListing: no detail links found")
            return []

        if all(key is not None for key, _ in dom_ordered):
            ordered = sorted(dom_ordered, key=lambda kv: kv[0] or 0, reverse=True)
        else:
            ordered = dom_ordered
        logger.info(
            f"{self.tag} PdfListing: {len(ordered)} detail pages, "
            f"taking {min(limit, len(ordered))} newest"
        )

        documents: list[FetchedDocument] = []
        for _, detail_url in ordered[:limit]:
            document = self._fetch_detail_pdf(detail_url)
            if document:
                documents.append(document)
        return documents

    def _fetch_detail_pdf(self, detail_url: str) -> Optional[FetchedDocument]:
        response = _http.get(detail_url, self.settings)
        if response is None:
            return None

        soup = BeautifulSoup(response.text, "lxml")
        keyword = (self.config.pdf_link_keyword or "").lower()
        pdf_url: Optional[str] = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.lower().endswith(".pdf"):
                continue
            if keyword and keyword not in href.lower() and keyword not in a.get_text(" ", strip=True).lower():
                continue
            pdf_url = urljoin(detail_url, href)
            break

        if not pdf_url:
            logger.info(f"{self.tag} PdfListing: no PDF on {detail_url}")
            return None

        result = _http.download(
            pdf_url,
            self.storage_dir(),
            self.settings,
            require_content_type="application/pdf",
            fallback_name="document.pdf",
        )
        if result is None:
            return None
        local_path, content = result
        return FetchedDocument(
            source_url=pdf_url,
            local_path=local_path,
            raw_content=content,
            content_type="pdf",
            fetched_at=datetime.now(timezone.utc),
        )

    def to_text(self, document: FetchedDocument) -> tuple[str, str]:
        if document.content_type == "pdf" and document.local_path:
            return pdf_extract.extract_text(document.local_path, self.settings)
        raise ValueError("PdfListingSource expects PDF documents")
