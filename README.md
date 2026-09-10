# docpipe

Fetch documents from web sources and turn them into clean text.

Extracted from a production pipeline that scrapes public board minutes across
hundreds of US school districts, generalized so the same machinery works on any
document source: tender portals, regulator filings, press-release archives,
grant announcements.

Two halves, useful together and separately:

- **Sources** retrieve documents from an origin (an index page, a JSON
  endpoint, a JS-rendered SPA) and hand back files or HTML.
- **Extractors** turn those into text, find the document's date, and cut the
  text down to something an LLM can afford to read.

## Install

```bash
pip install "docpipe[pdf] @ git+https://github.com/<you>/docpipe@v0.1.0"
```

Extras, because a project that only reads PDFs should not install a browser:

| Extra | Pulls in | Needed for |
|---|---|---|
| `pdf` | pdfplumber, pypdf | PDF text extraction |
| `ocr` | pytesseract, pdf2image | scanned PDFs with no text layer |
| `browser` | playwright | the `playwright_render` source |
| `all` | all of the above | |

`ocr` also needs the system binaries `tesseract` and `poppler-utils`.
`browser` needs `playwright install chromium` after pip.

## Quickstart

```python
from pathlib import Path
from docpipe import Settings, get_source

settings = Settings(raw_dir=Path("/var/tmp/scrapes"), http_timeout=60)

source = get_source(
    "pdf_direct",
    "acme-county",                        # names the download dir and log prefix
    {"page_url": "https://example.org/board/minutes"},
    settings=settings,
)

for doc in source.fetch_documents(limit=5):
    text, method = source.to_text(doc)
    print(doc.source_url, doc.doc_date, method, len(text))
```

The extractors work standalone, no source required:

```python
from docpipe.extract import pdf, dates, truncate

text, method = pdf.extract_text("report.pdf")     # ("...", "pdfplumber" | "pypdf" | "ocr")
when = dates.parse_date_from_text(text)           # date | None
fitted, meta = truncate.truncate_smart(text, max_chars=800_000)
```

## Sources

Pick the cheapest adapter that works. A JSON endpoint costs milliseconds, HTML
scraping costs a request, a browser costs 5 to 15 seconds and several MB. Before
reaching for `playwright_render`, open the network tab and look for the API the
page is already calling.

| `source_type` | Shape | Cost |
|---|---|---|
| `json_api` | JSON listing endpoint. Two built-in shapes: a wrapped item list, and WordPress `/wp-json/wp/v2/media` | one request |
| `pdf_direct` | Index page linking straight to files | one request + one per file |
| `html_page` | Index page linking to HTML documents | one request per document |
| `pdf_listing` | Index to per-item detail page to file | two requests per document |
| `playwright_render` | JS-rendered page, real Chromium | 5 to 15 s per page |

Every adapter validates its config through pydantic at construction, so a
misspelled key fails immediately instead of returning zero documents an hour
into a batch run.

`playwright_render` will not defeat a serious WAF. If the origin answers your
datacenter IP with a 403 no matter what, you need a residential proxy or a
hosted browser, not this.

## Extractors

**`extract.pdf`** tries pdfplumber, then pypdf, then OCR, returning
`(text, method)` so you can audit later which path produced a given result. The
OCR path carries hard caps on page count and per-page and rasterization timeouts,
because one pathological scan will otherwise hang an unattended run forever.

**`extract.html`** strips scripts, styles and chrome, then prefers `<main>`,
`<article>`, `<body>` in that order.

**`extract.dates`** pulls a date from document text or from a file name, with no
LLM involved. The hard part is not matching a date, it is rejecting the wrong
one: a document head is full of fiscal-year ends, effective dates, deadlines and
references to the *next* meeting. The default profile filters those by
inspecting the text immediately before each match. `PLAIN_PROFILE` turns the
filtering off for documents where a date is just a date.

**`extract.truncate`** fits an oversized document into a context budget without
a blind head cut. It drops boilerplate tails, then keeps context windows around
the passages that matter, then falls back to head plus middle plus tail. What
counts as boilerplate and what counts as worth keeping is a `TruncationProfile`:

```python
from docpipe.extract.truncate import keyword_profile, truncate_smart

profile = keyword_profile(
    keep_keywords=["adverse event", "contraindicat", "dosage"],
    tail_keywords=["Bibliography", "References"],
)
fitted, meta = truncate_smart(text, max_chars=200_000, profile=profile)
print(meta["method"])   # no_op | trimmed_safe_tail | trimmed_tail+windows | ...
```

The built-in `PROCUREMENT_PROFILE` (the default) targets public procurement
documents. `GENERIC_PROFILE` has no vocabulary at all.

## Writing your own source

Subclass `BaseSource`, declare a config model, register a name:

```python
from docpipe import BaseSource, FetchedDocument, SourceConfig, register_source
from docpipe import http
from docpipe.extract import pdf as pdf_extract


class MyPortalConfig(SourceConfig):
    portal_id: int
    since_year: int = 2024


@register_source("my_portal")
class MyPortalSource(BaseSource):
    config_model = MyPortalConfig
    config: MyPortalConfig

    def fetch_documents(self, limit: int = 3) -> list[FetchedDocument]:
        response = http.get(f"https://portal.example/api/{self.config.portal_id}", self.settings)
        ...
        result = http.download(url, self.storage_dir(), self.settings,
                               require_content_type="application/pdf")
        ...

    def to_text(self, document):
        return pdf_extract.extract_text(document.local_path, self.settings)
```

`get_source("my_portal", ...)` resolves it like any built-in. `self.storage_dir()`
returns `settings.raw_dir / source_id`, created on demand.

## Design notes

- **No global config.** Everything runtime-tunable lives on `Settings`, passed
  in or defaulted. Nothing is read from the environment, and nothing touches
  the filesystem at import time.
- **`source_id` is just a label.** It names the download subdirectory and
  prefixes log lines. Any slug works.
- **Failures return empty, they do not raise.** An unreachable index or a
  missing attachment logs a warning and yields no documents, so one bad source
  cannot take down a batch. Programming errors (a bad config, a missing
  dependency) do raise.
- **`FetchedDocument.doc_date` is best effort.** Adapters fill it when the
  listing or URL exposes it. Otherwise it stays `None` and you can fill it from
  the text with `extract.dates`.

## Development

```bash
make setup   # venv + editable install with all extras
make test
make lint
```
