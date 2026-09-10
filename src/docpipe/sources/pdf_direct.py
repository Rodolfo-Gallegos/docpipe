"""Sites that link their PDFs straight from one index page.

    GET <page_url>  ->  every <a href> that looks like a PDF  ->  download

The simplest and most common shape. When the PDFs live one click deeper,
behind per-item detail pages, use `pdf_listing` instead.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pydantic import Field

from docpipe.base import BaseSource, FetchedDocument, SourceConfig
from docpipe.extract import pdf as pdf_extract
from docpipe.extract.dates import parse_date_from_url
from docpipe.logger import get_logger
from docpipe.registry import register_source
from docpipe import http as _http

logger = get_logger(__name__)


class PdfDirectConfig(SourceConfig):
    """`page_url`: the page listing the documents.

    `pdf_link_pattern`: optional substring the absolute URL must contain.
        When unset, links are matched by a `.pdf` extension.
    `pdf_exclude_pattern`: optional substring that disqualifies a link
        (archives, translations, agenda-vs-minutes splits).
    """

    page_url: str = Field(pattern=r"^https?://", alias="minutes_page_url")
    pdf_link_pattern: Optional[str] = None
    pdf_exclude_pattern: Optional[str] = None


@register_source("pdf_direct")
class PdfDirectSource(BaseSource):
    config_model = PdfDirectConfig
    config: PdfDirectConfig

    def fetch_documents(self, limit: int = 3) -> List[FetchedDocument]:
        page_url = self.config.page_url
        logger.info(f"{self.tag} Fetching index page: {page_url}")

        response = _http.get(page_url, self.settings)
        if response is None:
            logger.error(f"{self.tag} Failed to fetch index page")
            return []

        soup = BeautifulSoup(response.text, "lxml")
        pdf_links: dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            full_url = urljoin(page_url, a["href"])

            if self.config.pdf_link_pattern:
                if self.config.pdf_link_pattern not in full_url:
                    continue
            elif not full_url.lower().endswith(".pdf"):
                continue

            if self.config.pdf_exclude_pattern and self.config.pdf_exclude_pattern in full_url:
                continue

            pdf_links.setdefault(full_url, a.get_text(" ", strip=True))

        logger.info(f"{self.tag} Found {len(pdf_links)} candidate PDFs")

        documents: list[FetchedDocument] = []
        for pdf_url in list(pdf_links)[:limit]:
            result = _http.download(
                pdf_url, self.storage_dir(), self.settings, fallback_name="document.pdf"
            )
            if result is None:
                continue
            local_path, content = result
            documents.append(FetchedDocument(
                source_url=pdf_url,
                title=pdf_links[pdf_url] or None,
                local_path=local_path,
                raw_content=content,
                doc_date=parse_date_from_url(pdf_url),
                content_type="pdf",
                fetched_at=datetime.now(timezone.utc),
            ))

        return documents

    def to_text(self, document: FetchedDocument) -> tuple[str, str]:
        if document.local_path is None:
            raise ValueError("PdfDirectSource requires local_path")
        return pdf_extract.extract_text(document.local_path, self.settings)
