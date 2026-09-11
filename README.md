# docpipe

Find document sources on the public web, classify them, and turn what they
publish into clean text. Built to be driven by an agent.

For the long tail of sites that publish documents and have no API
worth the name: tender portals, regulator filings, grant announcements,
council and committee records, press archives. The kind of source where
each site is a little different, none of them are worth a bespoke scraper,
and together they are worth a lot.

Three layers, each usable on its own:

- **A CLI that speaks JSON.** `probe` a URL and it tells you which adapter
  reads it, with what config, with what evidence, and whether that actually
  worked. `sniff` a SPA and it reports the JSON API behind it. Every
  command prints JSON to stdout and logs to stderr, with exit codes worth
  branching on.
- **Sources** that retrieve documents: an index page, a JSON endpoint, a
  JS-rendered app.
- **Extractors** that turn those into text, find the document's date, and
  cut it down to fit a context budget.

The design rule: docpipe never calls an LLM. Your agent already knows how
to search the web and read a page. What it cannot do reliably is guess an
adapter config from prose, or know whether a guess works. That part is
mechanical, so docpipe does it mechanically and hands back evidence.

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

## Driving it from an agent

Install the skill and subagent definitions into a project:

```bash
docpipe agent-kit --into .
```

That writes `.claude/skills/docpipe/SKILL.md` (how and when to use each
command) and four subagents:

| Subagent | Does |
|---|---|
| `source-finder` | Searches for where an organization publishes documents, classifies the page, verifies it, records it |
| `api-sniffer` | Given a SPA, finds the JSON API behind it and proves it can be called without a browser |
| `adapter-writer` | Turns an endpoint brief into a registered, tested adapter |
| `source-fixer` | Diagnoses a source that stopped working and applies the smallest fix |

Existing files are never overwritten without `--force`, so your edits
survive an upgrade.

### The loop

```bash
# 1. The agent searches the web and finds a candidate page.

# 2. Classify it, and actually run it.
docpipe probe "https://example.org/board/minutes" --verify
```

```json
{
  "candidates": [{
    "source_type": "pdf_direct",
    "config": {"page_url": "https://example.org/board/minutes",
               "pdf_link_pattern": "/fs/resource-manager/view/"},
    "confidence": "high",
    "evidence": [
      "25 links share the route shape /fs/<slug>/view/<slug>",
      "the first one answers with Content-Type application/pdf, so these are the documents themselves, not detail pages"
    ],
    "verified": true,
    "verified_note": "Fetched https://example.org/fs/... and extracted 9461 chars via pdfplumber."
  }]
}
```

`--verify` runs the top candidate for one document, and tries the next one
when it comes back empty, promoting whichever actually works. A
high-confidence guess is still a guess; `verified: true` is the answer.

```bash
# 3. If it is a SPA, find the API before paying for a browser.
docpipe sniff "https://example.org/portal" --wait 10

# 4. Record the working config.
docpipe add sources.json "https://example.org/board/minutes" --id acme --verify

# 5. Harvest, and read the journal.
docpipe run sources.json --limit 5 --raw-dir ./data/raw
```

Every run returns a per-source journal: which steps ran, what came back,
and a diagnosis when something is off.

```json
{"status": "partial",
 "steps": [{"name": "fetch", "ok": true, "detail": "3 documents", "elapsed_ms": 5170}],
 "diagnosis": "Documents were fetched but none produced usable text. If they are scans, enable OCR..."}
```

`status` is one of `ok`, `partial`, `empty`, `error`. Exit code 0 means
every source came back clean; 2 means something needs attention. `partial`
counts as needing attention on purpose: a run that downloads three PDFs and
extracts nothing from them looks like success in a log and is not one.

### Commands

| Command | For |
|---|---|
| `probe <url> [--verify]` | Which adapter reads this, with what config |
| `sniff <url>` | The JSON API behind a JS-rendered page |
| `schema [type]` | The fields an adapter accepts, with docs |
| `validate <type> --config` | Check a config before running it |
| `fetch <type> --config` | Run one adapter now, no recipe file |
| `add <recipe> <url> --id` | Probe and record into a recipe file |
| `list <recipe>` / `run <recipe>` | Show / execute recorded sources |
| `run --remember` | Execute and write back what it learned |
| `extract <paths>` | Text, method and date from local files |
| `analyze <paths>` | Text plus your prompt and schema, to JSON (needs a key) |
| `agent-kit --into <dir>` | Install the skill and subagents |

### Memory: maintaining a fleet by exception

`docpipe run --remember` writes back what the run taught it:

```json
{
  "id": "acme",
  "source_type": "pdf_direct",
  "config": {"page_url": "https://example.org/board/minutes"},
  "memory": {
    "success_count": 12,
    "consecutive_failures": 0,
    "last_ok_at": "2026-09-10T12:00:00+00:00",
    "doc_url_pattern": "^https://example\\.org/files/[0-9-]+\\.pdf$"
  }
}
```

Three things come out of that:

- **Quarantine.** Three consecutive failures and the source stops being
  run, with a reason recorded. A source that never worked gets a different
  diagnosis than one that worked for a year and stopped, because the first
  is a bad config and the second is a site that changed. One success
  releases it.
- **Shape change.** The learned URL pattern is a tripwire. A source can
  return documents and still be broken: a redesign that swaps the archive
  for a "not found" list still yields links. When the URLs stop matching
  what this source used to produce, the run says so.
- **Triage by exception.** With a few hundred sources, the useful question
  is not "did the run pass" but "which five need me today".

Without `--remember` the file is never written to, so a run stays a
read-only operation.

### The recipe file

The durable artifact of the loop, and the input to every later run. Plain
JSON, so an agent can append to it and a human can review the diff.

```json
{
  "version": 1,
  "sources": [{
    "id": "acme",
    "source_type": "pdf_direct",
    "config": {"page_url": "https://example.org/board/minutes"},
    "enabled": true,
    "notes": "Verified 2026-09-10: 3 documents, pdfplumber."
  }]
}
```

## Optional: analysis with an LLM

Everything above is deterministic and needs no API key. This one part is
opt-in, and it is the boundary of what docpipe does: it hands the text to
a model with *your* prompt and *your* schema, and gives you back JSON.

```bash
pip install 'docpipe[anthropic]'   # or 'docpipe[gemini]'
export ANTHROPIC_API_KEY=...       # or GOOGLE_API_KEY

docpipe analyze minutes.pdf \
  --prompt "Extract every contract award: vendor, amount, and what for." \
  --schema awards.schema.json
```

docpipe supplies no prompt and no schema. It cannot: what counts as a
useful field depends entirely on what you are collecting. What it does
supply is the plumbing that is identical every time, and annoying every
time:

- **Fitting the text to the budget.** Oversized documents go through
  `truncate_smart`, and the result reports what was dropped. Silent
  truncation would make the output a lie.
- **One schema, either provider.** Anthropic requires
  `additionalProperties: false`; Gemini rejects the same key outright.
  Write standard JSON Schema and docpipe translates.
- **Failing over.** A busy model falls back to a stand-in. A malformed
  request does not: retrying a 400 just spends money twice.
- **Not paying for nothing.** An empty document never reaches the model.

The Gemini defaults are the rolling `-latest` aliases on purpose. Pinned
Gemini versions get retired for new users while still appearing in the
model list, so a pin that worked when you wrote it returns 404 a year
later. Pass `--model` to pin deliberately.

## Using it as a library

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
| `playwright_render` | JS-rendered page, local Chromium | 5 to 15 s per page |
| `cloud_render` | Hosted browser, for origins that block yours | 10 to 30 s per page, billed |
| `blocked` | A source you cannot read, recorded on purpose | nothing |

The order is an escalation, and each step up costs more than the last:

```
json_api            milliseconds, no browser         always try first
pdf_direct/html     one request                      server-rendered
playwright_render   5-15s, local Chromium            content needs JS
cloud_render        10-30s, someone else's browser   you are blocked
blocked             nothing                          nothing gets through
```

`probe` tells you which step you are on, including the last two: a 403 or a
timeout comes back with a `cloud_render` config already filled in, and
`proxy: true` when the symptom points at a datacenter IP ban rather than a
bot check. When even that fails, record the source as `blocked` with a
reason. An absent source and an impossible one look identical in a recipe
file, and the difference is worth keeping.

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
