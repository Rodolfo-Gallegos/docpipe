---
name: docpipe
description: Find, classify and harvest document sources on the public web (PDFs, HTML documents, JSON listing APIs) using the docpipe CLI. Use whenever the task is to collect documents from an organization's site, add a new source to a scraping fleet, diagnose a source that stopped returning documents, or extract text from PDFs at scale.
---

# docpipe

A CLI for turning "this organization publishes documents somewhere" into
"those documents are on disk as text". You do the searching; docpipe does
the classifying, fetching and extracting, and tells you when it is wrong.

Every command prints JSON to stdout and logs to stderr. Exit code 0 means
clean, 1 means bad usage, 2 means the command ran but the result needs
attention. Branch on the exit code, read the JSON for the why.

## The loop

**1. Find the page.** This part is yours. Search for where the
organization publishes the documents (minutes, notices, tenders, filings,
reports). Prefer the page that lists many of them over a single document.
Avoid aggregators and news coverage.

**2. Classify it.**

```bash
docpipe probe "<url>" --verify
```

`--verify` actually runs the top candidate for one document. Read the
result before believing it:

- `candidates[0].verified: true` means it works. Move on.
- `verified: false` with a note means it tried and failed, and the note
  says how. The probe tries up to three candidates and promotes whichever
  one works, so a `false` on the first with a `true` on the second is a
  success, not a failure.
- `source_type: null` means the platform is recognized but no adapter
  reads it. `next_step` says what to do (usually: it has a public API,
  write an adapter).
- `platform: "spa"` means the content arrives after JS. Go to step 3.

**3. Only if it is a SPA, find the API.**

```bash
docpipe sniff "<url>" --wait 10
```

Reports the JSON calls the page makes and suggests a `json_api` config.
Prefer this over `playwright_render` every time: a browser costs 5 to 15
seconds and several MB per page, the API costs milliseconds. If sniff
suggests nothing, re-run with `--include-all` before giving up.

**4. Record it.**

```bash
docpipe add sources.json "<url>" --id <slug> --verify
```

Writes the working config into a recipe file. `--verify` refuses to record
a source that does not actually produce a document, which is the point:
the recipe file should only ever contain sources that work.

**5. Harvest.**

```bash
docpipe run sources.json --limit 5 --raw-dir ./data/raw
```

Runs every enabled source and returns a journal per source: which steps
ran, what came back, and a diagnosis when something is off.

## When a source breaks

```bash
docpipe run sources.json --only <id> --limit 1
```

Read `status` and `diagnosis`:

| status | meaning | usual fix |
|---|---|---|
| `error` | the adapter could not run | config no longer validates; check `docpipe schema <type>` |
| `empty` | ran clean, matched nothing | the page moved or changed markup; re-run `probe` |
| `partial` | fetched, but little or no text | scanned PDFs (install the ocr extra), or the links point at landing pages |
| `ok` | working | nothing |

Re-probing the URL is almost always the right first move: sites get
redesigned, and the adapter that fit last year may not fit now.

## Writing a config by hand

```bash
docpipe schema                    # every adapter
docpipe schema pdf_direct         # one, with field docs
docpipe validate pdf_direct --config '{"page_url": "https://..."}'
docpipe fetch pdf_direct --id test --config '{...}' --limit 1
```

`validate` before `fetch` before `add`. Each step fails faster than the
next.

## Extracting text from files you already have

```bash
docpipe extract report.pdf                    # text + method + date
docpipe extract *.pdf --out ./text            # write .txt files
docpipe extract big.pdf --max-chars 200000    # fit an LLM context budget
```

## Rules

- **Cheapest adapter that works.** `json_api` over HTML scraping, HTML
  scraping over `playwright_render`. Check for an API before rendering.
- **Never record an unverified source.** Use `--verify` on `add`.
- **Do not invent config fields.** Run `docpipe schema <type>`; the
  adapters reject unknown keys on purpose.
- **A high-confidence guess is still a guess.** Only `verified: true`
  means it works.
- **Respect the site.** Reasonable limits, no parallel hammering. These
  are usually small public servers.
