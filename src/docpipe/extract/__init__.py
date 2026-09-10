"""Text extraction: PDF, HTML, dates, and context-budget truncation.

    from docpipe.extract import pdf, html, dates, truncate

    text, method = pdf.extract_text(path)
    body = html.extract_text(raw_html)
    when = dates.parse_date_from_text(text)
    fitted, meta = truncate.truncate_smart(text, max_chars=800_000)
"""
from docpipe.extract import dates, html, pdf, truncate

__all__ = ["dates", "html", "pdf", "truncate"]
