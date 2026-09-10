"""Sites that publish the content itself as HTML, not as attachments.

    GET <page_url>  ->  links matching <link_pattern>  ->  GET each

When no link matches, the index page itself is returned as the document.
Some small sites keep everything on one page, and that fallback is the
difference between a working source and an empty result.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pydantic import Field

from docpipe.base import BaseSource, FetchedDocument, SourceConfig
from docpipe.extract import html as html_extract
from docpipe.logger import get_logger
from docpipe.registry import register_source
from docpipe import http as _http

logger = get_logger(__name__)


class HtmlPageConfig(SourceConfig):
    """`page_url`: the index page.

    `link_pattern`: substring matched case-insensitively against each
        link's href *or* its visible text.
    """

    page_url: str = Field(pattern=r"^https?://", alias="minutes_page_url")
    link_pattern: str = "minutes"


@register_source("html_page")
class HtmlPageSource(BaseSource):
    config_model = HtmlPageConfig
    config: HtmlPageConfig

    def fetch_documents(self, limit: int = 3) -> List[FetchedDocument]:
        page_url = self.config.page_url
        needle = self.config.link_pattern.lower()
        logger.info(f"{self.tag} Fetching index page: {page_url}")

        response = _http.get(page_url, self.settings)
        if response is None:
            logger.error(f"{self.tag} Failed to fetch index")
            return []

        soup = BeautifulSoup(response.text, "lxml")
        links: dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            label = a.get_text(" ", strip=True)
            if needle in href.lower() or needle in label.lower():
                full_url = urljoin(page_url, href)
                # Binary attachments belong to the PDF adapters.
                if not full_url.lower().endswith((".pdf", ".doc", ".docx")):
                    links.setdefault(full_url, label)
        logger.info(f"{self.tag} Found {len(links)} candidate pages")

        documents: list[FetchedDocument] = []
        for url in list(links)[:limit]:
            page_response = _http.get(url, self.settings)
            if page_response is None:
                continue
            documents.append(FetchedDocument(
                source_url=url,
                title=links[url] or None,
                raw_html=page_response.text,
                content_type="html",
                fetched_at=datetime.now(timezone.utc),
            ))

        if not documents and not links:
            logger.info(f"{self.tag} No links matched, using the index page as content")
            documents.append(FetchedDocument(
                source_url=page_url,
                raw_html=response.text,
                content_type="html",
                fetched_at=datetime.now(timezone.utc),
            ))

        return documents

    def to_text(self, document: FetchedDocument) -> tuple[str, str]:
        if document.raw_html is None:
            raise ValueError("HtmlPageSource requires raw_html")
        return html_extract.extract_text(document.raw_html), "html"
