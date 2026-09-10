"""Extract clean text from PDF files.

Three strategies, tried in order, first one that yields enough text wins:

1. **pdfplumber** - best layout preservation.
2. **pypdf** - faster, more tolerant of malformed files.
3. **OCR** (pdf2image + pytesseract) - for scans with no text layer.

Returns `(text, method)` so the caller can record which path produced the
result, which matters when auditing extraction quality later.

The OCR path is wrapped in hard caps (page count, per-page timeout,
rasterization timeout) because a single pathological scan can otherwise
hang an unattended batch run indefinitely. Tune them on `Settings`.

Install extras: `docpipe[pdf]` for steps 1-2, `docpipe[ocr]` for step 3
(which also needs the `tesseract` and `poppler-utils` system binaries).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Optional, Tuple

from docpipe.logger import get_logger
from docpipe.settings import DEFAULT_SETTINGS, Settings

logger = get_logger(__name__)


class MissingDependency(RuntimeError):
    """A PDF backend is not installed. Message names the extra to install."""


def extract_with_pdfplumber(pdf_path: Path) -> str:
    try:
        import pdfplumber
    except ImportError as e:
        raise MissingDependency(
            "pdfplumber is required. Install with: pip install 'docpipe[pdf]'"
        ) from e

    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    return "\n\n".join(text_parts).strip()


def extract_with_pypdf(pdf_path: Path) -> str:
    try:
        import pypdf
    except ImportError as e:
        raise MissingDependency(
            "pypdf is required. Install with: pip install 'docpipe[pdf]'"
        ) from e

    reader = pypdf.PdfReader(pdf_path)
    text_parts = []
    for page in reader.pages:
        text_parts.append(page.extract_text() or "")
    return "\n\n".join(text_parts).strip()


def extract_with_ocr(pdf_path: Path, settings: Optional[Settings] = None) -> str:
    settings = settings or DEFAULT_SETTINGS
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        logger.warning(
            "OCR libraries not available (pip install 'docpipe[ocr]'), skipping OCR fallback"
        )
        return ""

    # Rasterize with a wall-clock timeout. convert_from_path spawns poppler
    # under the hood and has no native timeout, so we run it in a worker.
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(convert_from_path, str(pdf_path))
        try:
            images = future.result(timeout=settings.ocr_rasterize_timeout)
        except FuturesTimeout:
            logger.warning(
                f"OCR rasterization timed out after {settings.ocr_rasterize_timeout}s "
                f"on {pdf_path.name}"
            )
            return ""

    if len(images) > settings.ocr_max_pages:
        logger.warning(
            f"OCR capping {pdf_path.name} at {settings.ocr_max_pages} pages "
            f"(source had {len(images)})"
        )
        images = images[: settings.ocr_max_pages]

    text_parts = []
    for i, image in enumerate(images):
        try:
            page_text = pytesseract.image_to_string(
                image, timeout=settings.ocr_per_page_timeout
            )
        except RuntimeError as e:
            # pytesseract raises RuntimeError on its own timeout.
            logger.warning(
                f"OCR page {i + 1} of {pdf_path.name} timed out, skipping: {e}"
            )
            continue
        text_parts.append(page_text)
    return "\n\n".join(text_parts).strip()


def extract_text(
    pdf_path: Path | str,
    settings: Optional[Settings] = None,
) -> Tuple[str, str]:
    """Extract text from a PDF with the fallback chain. Returns (text, method).

    `method` is one of "pdfplumber", "pypdf", "ocr", or "none" when every
    strategy came back empty.
    """
    settings = settings or DEFAULT_SETTINGS
    pdf_path = Path(pdf_path)
    threshold = settings.ocr_min_text_chars

    for name, fn in (("pdfplumber", extract_with_pdfplumber), ("pypdf", extract_with_pypdf)):
        try:
            text = fn(pdf_path)
        except MissingDependency:
            raise
        except Exception as e:
            logger.warning(f"{name} failed on {pdf_path.name}: {e}")
            continue
        if len(text) >= threshold:
            logger.info(f"{name} extracted {len(text)} chars from {pdf_path.name}")
            return text, name

    if not settings.ocr_enabled:
        logger.info(f"OCR disabled, returning empty text for {pdf_path.name}")
        return "", "none"

    logger.info(f"Falling back to OCR for {pdf_path.name}")
    text = extract_with_ocr(pdf_path, settings)
    return text, "ocr" if text else "none"
