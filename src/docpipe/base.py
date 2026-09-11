"""Base class and shared document model for all source types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from docpipe.settings import DEFAULT_SETTINGS, Settings


class SourceConfig(BaseModel):
    """Base for every adapter config.

    `extra="forbid"` is deliberate: a misspelled key fails at construction
    time with a ValidationError instead of being silently ignored and
    surfacing later as a scrape that returns nothing.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FetchedDocument(BaseModel):
    """A document retrieved from a source, ready for text extraction.

    Exactly one of `local_path` / `raw_html` is normally populated:
    binary documents are written to disk and referenced by path, HTML
    documents are carried in memory.

    `fetched_at` records when the HTTP fetch completed, which is distinct
    from whenever the caller later persists or processes the document.

    `doc_date` is the date the document itself is about (publication,
    filing, session), not the fetch date. It is best-effort: adapters set
    it when the listing or the URL exposes it, otherwise it stays None and
    the caller can fill it from the text with `docpipe.extract.dates`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    source_url: str
    # The link text the document was found under ("2026-06-15 Quarterly
    # Report"). Often the only place its real date appears: the file name is
    # a UUID and the body opens by referring to an earlier document.
    title: Optional[str] = None
    local_path: Optional[Path] = None
    raw_content: Optional[bytes] = None
    raw_html: Optional[str] = None
    doc_date: Optional[date] = None
    content_type: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BaseSource(ABC):
    """Abstract base for fetching documents from one origin.

    Subclasses declare `config_model` and receive an already-validated
    config instance. `source_id` is a caller-chosen slug: it names the
    download subdirectory and prefixes log lines, nothing more.
    """

    config_model: type[SourceConfig]

    def __init__(
        self,
        source_id: str,
        config: SourceConfig,
        settings: Optional[Settings] = None,
    ):
        self.source_id = source_id
        self.config = config
        self.settings = settings or DEFAULT_SETTINGS

    @abstractmethod
    def fetch_documents(self, limit: int = 3) -> List[FetchedDocument]:
        """Fetch up to `limit` most recent documents."""
        raise NotImplementedError

    @abstractmethod
    def to_text(self, document: FetchedDocument) -> tuple[str, str]:
        """Convert a fetched document to clean text. Returns (text, method)."""
        raise NotImplementedError

    # ── helpers for subclasses ─────────────────────────────────────────

    @property
    def tag(self) -> str:
        """Log prefix, e.g. `[acme-corp]`."""
        return f"[{self.source_id}]"

    def storage_dir(self) -> Path:
        return self.settings.storage_dir(self.source_id)
