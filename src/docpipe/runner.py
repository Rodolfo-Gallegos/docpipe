"""Run one source end to end and journal what happened.

The journal is the point. When a source stops working (and they all do:
URLs move, a CMS is replaced, a WAF appears) the useful question is not
"did it fail" but "which step failed, with what evidence". So every run
records its steps, and a failure is a status plus a diagnosis rather than
a traceback.

    from docpipe.runner import run_source

    record = run_source("acme", "pdf_direct", {"page_url": "..."}, limit=3)
    print(record.status)          # ok | partial | empty | error
    print(record.to_dict())       # JSON-serializable, for an agent to read
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from docpipe.logger import get_logger
from docpipe.registry import get_source
from docpipe.settings import DEFAULT_SETTINGS, Settings

logger = get_logger(__name__)

# Text this short is a fetch that technically worked and practically did
# not: a cover page, a "no records" stub, an image-only scan with OCR off.
MIN_USEFUL_CHARS = 500


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    elapsed_ms: int = 0


@dataclass
class DocumentRecord:
    source_url: str
    title: Optional[str] = None
    content_type: str = ""
    local_path: Optional[str] = None
    doc_date: Optional[str] = None
    date_source: Optional[str] = None
    text_chars: int = 0
    extract_method: Optional[str] = None
    truncation: Optional[dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class RunRecord:
    """One source's run.

    status:
      ok       every document produced usable text
      partial  some documents produced usable text, some did not
      empty    the adapter ran clean but found nothing to fetch
      error    the adapter could not run at all
    """

    source_id: str
    source_type: str
    status: str = "error"
    documents: list[DocumentRecord] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    error: Optional[str] = None
    diagnosis: Optional[str] = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_source(
    source_id: str,
    source_type: str,
    config: dict,
    settings: Optional[Settings] = None,
    limit: int = 3,
    extract: bool = True,
    max_chars: Optional[int] = None,
    truncation_profile=None,
) -> RunRecord:
    """Fetch and (optionally) extract, returning a journal of the attempt.

    Never raises for an expected failure: an unreachable host, a bad
    config, a PDF that will not parse all come back as a record with a
    status and a diagnosis. That is what makes it usable unattended.
    """
    settings = settings or DEFAULT_SETTINGS
    record = RunRecord(source_id=source_id, source_type=source_type)
    started = time.monotonic()

    source = _build(record, source_type, source_id, config, settings)
    if source is None:
        record.elapsed_ms = int((time.monotonic() - started) * 1000)
        return record

    documents = _fetch(record, source, limit)
    if documents is None:
        record.elapsed_ms = int((time.monotonic() - started) * 1000)
        return record

    if not documents:
        record.status = "empty"
        record.diagnosis = (
            "The adapter ran without error but matched no documents. Either "
            "the config is too narrow, the page moved, or this is the wrong "
            "adapter. Re-run `docpipe probe` on the URL."
        )
        record.elapsed_ms = int((time.monotonic() - started) * 1000)
        return record

    if extract:
        _extract_all(record, source, documents, max_chars, truncation_profile)
    else:
        record.documents = [_describe(d) for d in documents]
        record.status = "ok"

    record.elapsed_ms = int((time.monotonic() - started) * 1000)
    return record


def _build(record, source_type, source_id, config, settings):
    started = time.monotonic()
    try:
        source = get_source(source_type, source_id, config, settings=settings)
    except Exception as e:
        elapsed = int((time.monotonic() - started) * 1000)
        record.steps.append(Step("build", False, f"{type(e).__name__}: {e}", elapsed))
        record.error = f"{type(e).__name__}: {e}"
        record.diagnosis = (
            "The config did not validate against the adapter's schema. Run "
            f"`docpipe schema {source_type}` to see the accepted fields."
        )
        return None
    record.steps.append(
        Step("build", True, f"{type(source).__name__}", int((time.monotonic() - started) * 1000))
    )
    return source


def _fetch(record, source, limit):
    started = time.monotonic()
    try:
        documents = source.fetch_documents(limit=limit)
    except Exception as e:
        elapsed = int((time.monotonic() - started) * 1000)
        record.steps.append(Step("fetch", False, f"{type(e).__name__}: {e}", elapsed))
        record.error = f"{type(e).__name__}: {e}"
        record.diagnosis = (
            "The adapter raised while fetching. If this is playwright_render, "
            "check that chromium is installed (`playwright install chromium`)."
        )
        return None
    record.steps.append(
        Step("fetch", True, f"{len(documents)} documents", int((time.monotonic() - started) * 1000))
    )
    return documents


def _extract_all(record, source, documents, max_chars, truncation_profile):
    started = time.monotonic()
    usable = 0
    for document in documents:
        described = _describe(document)
        try:
            text, method = source.to_text(document)
        except Exception as e:
            described.error = f"{type(e).__name__}: {e}"
            record.documents.append(described)
            continue

        described.extract_method = method
        described.text_chars = len(text)

        # Most adapters cannot know the document's date: the listing shows a
        # title and the URL is a UUID. Fill the gap, preferring the title
        # over the body. Minutes routinely open by approving the *previous*
        # meeting's minutes, so the first date in the text is often the wrong
        # one, while the link said "6-15-2026 Board Meeting" all along.
        if described.doc_date is None:
            from docpipe.extract.dates import parse_date_from_text, parse_date_from_url

            for value, origin in (
                (document.title, "title"),
                (document.source_url, "url"),
            ):
                found = parse_date_from_url(value) if value else None
                if found:
                    described.doc_date = found.isoformat()
                    described.date_source = origin
                    break
            else:
                found = parse_date_from_text(text)
                if found:
                    described.doc_date = found.isoformat()
                    described.date_source = "text"
        if max_chars and len(text) > max_chars:
            from docpipe.extract.truncate import DEFAULT_PROFILE, truncate_smart

            _, meta = truncate_smart(
                text, max_chars, profile=truncation_profile or DEFAULT_PROFILE
            )
            described.truncation = dict(meta)
        if len(text) >= MIN_USEFUL_CHARS:
            usable += 1
        record.documents.append(described)

    record.steps.append(Step(
        "extract",
        usable > 0,
        f"{usable}/{len(documents)} documents produced usable text",
        int((time.monotonic() - started) * 1000),
    ))

    if usable == len(documents):
        record.status = "ok"
    elif usable:
        record.status = "partial"
        record.diagnosis = (
            "Some documents came back nearly empty. Usually scanned PDFs with "
            "no text layer: install the ocr extra, or check the failures below."
        )
    else:
        record.status = "partial"
        record.diagnosis = (
            "Documents were fetched but none produced usable text. If they are "
            "scans, enable OCR (`pip install 'docpipe[ocr]'` plus tesseract and "
            "poppler-utils). If they are HTML, the content may be behind JS."
        )


def _describe(document) -> DocumentRecord:
    return DocumentRecord(
        source_url=document.source_url,
        title=document.title,
        content_type=document.content_type,
        local_path=str(document.local_path) if document.local_path else None,
        doc_date=document.doc_date.isoformat() if document.doc_date else None,
        date_source="adapter" if document.doc_date else None,
    )
