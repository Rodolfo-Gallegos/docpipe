"""Command line interface, shaped for an agent to drive.

Every command writes JSON to stdout and nothing else. Logs go to stderr.
An agent can therefore pipe any command straight into a parser without
stripping banners, and a human can read the same output with `| jq`.

The loop this is built for:

    docpipe probe <url> --verify     # what is this, and does it work?
    docpipe sniff <url>              # when it is a SPA: find the JSON API
    docpipe add sources.json <url> --id acme --verify
    docpipe run sources.json --remember   # fetch, extract, and learn
    docpipe schema pdf_direct        # what fields does this adapter take?

Exit codes: 0 clean success, 1 bad usage or invalid input, 2 the command
ran but the result needs attention (nothing fetched, text came back empty,
config rejected, source unreachable). "Partial" counts as 2 on purpose: a
run that downloads three PDFs and extracts nothing from them looks like
success in a log and is not one.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from docpipe import recipes
from docpipe.probe import probe as probe_url
from docpipe.registry import available_sources, canonical_name, source_class
from docpipe.runner import run_source
from docpipe.settings import Settings

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_FAILED = 2


def _emit(payload: Any, exit_code: int = EXIT_OK) -> int:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return exit_code


def _fail(message: str, **extra) -> int:
    return _emit({"ok": False, "error": message, **extra}, EXIT_USAGE)


def _parse_config(raw: Optional[str], config_file: Optional[str]) -> dict:
    if config_file:
        return json.loads(Path(config_file).read_text())
    if not raw:
        return {}
    if raw.strip().startswith("@"):
        return json.loads(Path(raw.strip()[1:]).read_text())
    return json.loads(raw)


def _settings(args) -> Settings:
    settings = Settings()
    if getattr(args, "raw_dir", None):
        settings = settings.replace(raw_dir=Path(args.raw_dir))
    if getattr(args, "timeout", None):
        settings = settings.replace(http_timeout=args.timeout)
    if getattr(args, "no_ocr", False):
        settings = settings.replace(ocr_enabled=False)
    return settings


# ── Commands ────────────────────────────────────────────────────────────


def cmd_sources(args) -> int:
    payload = []
    for name in available_sources():
        cls = source_class(name)
        # The description that matters lives in the adapter's module
        # docstring, not on the class.
        module_doc = (sys.modules[cls.__module__].__doc__ or "").strip()
        entry = {
            "source_type": name,
            "class": cls.__name__,
            "summary": module_doc.split("\n")[0],
        }
        canonical = canonical_name(cls)
        if canonical != name:
            entry["alias_of"] = canonical
        payload.append(entry)
    return _emit({"ok": True, "sources": payload})


def cmd_schema(args) -> int:
    names = [args.source_type] if args.source_type else available_sources()
    schemas = {}
    for name in names:
        try:
            cls = source_class(name)
        except ValueError as e:
            return _fail(str(e), available=available_sources())

        # by_alias=False so the schema shows the canonical field names, the
        # same ones `probe` emits. The aliases still validate, so list them
        # separately instead of letting them shadow the real names.
        schema = cls.config_model.model_json_schema(by_alias=False)
        aliases = {
            field_name: field.alias
            for field_name, field in cls.config_model.model_fields.items()
            if field.alias and field.alias != field_name
        }
        if aliases:
            schema["x-accepted-aliases"] = aliases
        schemas[name] = schema
    return _emit({"ok": True, "schemas": schemas})


def cmd_probe(args) -> int:
    result = probe_url(args.url, settings=_settings(args), verify=args.verify)
    payload = {"ok": result.ok, **result.to_dict()}
    if not result.ok:
        return _emit(payload, EXIT_FAILED)
    if args.verify and result.best and result.best.verified is False:
        return _emit(payload, EXIT_FAILED)
    return _emit(payload)


def cmd_validate(args) -> int:
    try:
        cls = source_class(args.source_type)
    except ValueError as e:
        return _fail(str(e), available=available_sources())
    try:
        config = _parse_config(args.config, args.config_file)
    except (json.JSONDecodeError, OSError) as e:
        return _fail(f"config is not readable JSON: {e}")

    try:
        validated = cls.config_model.model_validate(config)
    except ValidationError as exc:
        errors = exc.errors()
    except Exception as exc:
        errors = [{"msg": str(exc)}]
    else:
        errors = None

    if errors is not None:
        return _emit({
            "ok": False,
            "source_type": args.source_type,
            "errors": errors,
            "hint": f"Run `docpipe schema {args.source_type}` for the accepted fields.",
        }, EXIT_FAILED)

    return _emit({
        "ok": True,
        "source_type": args.source_type,
        "config": validated.model_dump(mode="json"),
    })


def cmd_fetch(args) -> int:
    try:
        config = _parse_config(args.config, args.config_file)
    except (json.JSONDecodeError, OSError) as e:
        return _fail(f"config is not readable JSON: {e}")

    record = run_source(
        args.id,
        args.source_type,
        config,
        settings=_settings(args),
        limit=args.limit,
        extract=not args.no_extract,
        max_chars=args.max_chars,
    )
    payload = {"ok": record.status == "ok", **record.to_dict()}
    return _emit(payload, EXIT_OK if payload["ok"] else EXIT_FAILED)


def cmd_extract(args) -> int:
    from docpipe.extract import dates
    from docpipe.extract import html as html_extract
    from docpipe.extract import pdf as pdf_extract
    from docpipe.extract.truncate import DEFAULT_PROFILE, GENERIC_PROFILE, truncate_smart

    settings = _settings(args)
    profile = GENERIC_PROFILE if args.profile == "generic" else DEFAULT_PROFILE
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    failures = 0
    for raw_path in args.paths:
        path = Path(raw_path)
        entry: dict[str, Any] = {"path": str(path)}
        if not path.exists():
            entry.update(ok=False, error="file not found")
            failures += 1
            results.append(entry)
            continue

        try:
            if path.suffix.lower() in (".html", ".htm"):
                text, method = html_extract.extract_text(path.read_text(errors="replace")), "html"
            else:
                text, method = pdf_extract.extract_text(path, settings)
        except Exception as e:
            entry.update(ok=False, error=f"{type(e).__name__}: {e}")
            failures += 1
            results.append(entry)
            continue

        entry.update(ok=True, method=method, chars=len(text))
        found = dates.parse_date_from_text(text) or dates.parse_date_from_url(path.name)
        entry["doc_date"] = found.isoformat() if found else None

        if args.max_chars and len(text) > args.max_chars:
            text, meta = truncate_smart(text, args.max_chars, profile=profile)
            entry["truncation"] = dict(meta)

        if out_dir:
            target = out_dir / (path.stem + ".txt")
            target.write_text(text)
            entry["text_path"] = str(target)
        else:
            entry["text"] = text if args.full else text[:2000]
            entry["text_truncated_in_output"] = not args.full and len(text) > 2000

        results.append(entry)

    return _emit(
        {"ok": failures == 0, "results": results},
        EXIT_OK if failures == 0 else EXIT_FAILED,
    )


def cmd_analyze(args) -> int:
    from docpipe.analyze import AnalysisError, analyze, available_providers
    from docpipe.extract import html as html_extract
    from docpipe.extract import pdf as pdf_extract

    prompt = args.prompt
    if args.prompt_file:
        try:
            prompt = Path(args.prompt_file).read_text()
        except OSError as e:
            return _fail(f"could not read the prompt file: {e}")
    if not prompt:
        return _fail("a prompt is required: pass --prompt or --prompt-file")

    schema = None
    if args.schema:
        try:
            schema = json.loads(Path(args.schema).read_text())
        except (OSError, json.JSONDecodeError) as e:
            return _fail(f"could not read the schema: {e}")

    settings = _settings(args)
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    failures = 0
    for raw_path in args.paths:
        entry: dict[str, Any] = {"path": raw_path}

        if raw_path == "-":
            text = sys.stdin.read()
        else:
            path = Path(raw_path)
            if not path.exists():
                entry.update(ok=False, error="file not found")
                failures += 1
                results.append(entry)
                continue
            try:
                if path.suffix.lower() in (".html", ".htm"):
                    text = html_extract.extract_text(path.read_text(errors="replace"))
                elif path.suffix.lower() in (".txt", ".md", ""):
                    text = path.read_text(errors="replace")
                else:
                    text, _ = pdf_extract.extract_text(path, settings)
            except Exception as e:
                entry.update(ok=False, error=f"could not extract text: {e}")
                failures += 1
                results.append(entry)
                continue

        try:
            result = analyze(
                text,
                prompt=prompt,
                schema=schema,
                provider=args.provider,
                model=args.model,
                fallback_model=args.fallback_model,
                max_input_chars=args.max_chars,
            )
        except AnalysisError as e:
            return _emit({
                "ok": False,
                "error": str(e),
                "providers_with_credentials": available_providers(),
            }, EXIT_USAGE)

        entry.update(result.to_dict())
        if not result.ok:
            failures += 1
        if out_dir and result.ok:
            target = out_dir / (Path(raw_path).stem + ".json")
            target.write_text(json.dumps(result.data, indent=2, default=str) + "\n")
            entry["output_path"] = str(target)
            entry.pop("text", None)
        results.append(entry)

    return _emit(
        {"ok": failures == 0, "results": results},
        EXIT_OK if failures == 0 else EXIT_FAILED,
    )


def cmd_agent_kit(args) -> int:
    """Copy the skill and subagent definitions into a project.

    They ship inside the package so `pip install docpipe` is enough to get
    them; this just puts them where Claude Code looks.
    """
    import shutil

    source_root = Path(__file__).parent / "agent_kit"
    target_root = Path(args.into) / ".claude"

    copied, skipped = [], []
    for source in sorted(source_root.rglob("*.md")):
        target = target_root / source.relative_to(source_root)
        if target.exists() and not args.force:
            skipped.append(str(target))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(target))

    return _emit({
        "ok": True,
        "copied": copied,
        "skipped": skipped,
        "hint": (
            "Pass --force to overwrite the skipped files."
            if skipped else
            "Invoke the skill with /docpipe, or let the subagents pick it up."
        ),
    })


def cmd_sniff(args) -> int:
    from docpipe.sniff import sniff

    try:
        result = sniff(
            args.url,
            settings=_settings(args),
            wait_seconds=args.wait,
            include_all=args.include_all,
        )
    except RuntimeError as e:
        return _fail(str(e))
    except Exception as e:
        # Playwright raises its own error types for a missing browser
        # binary. Surface that as JSON with the remedy, not a traceback an
        # agent has to parse out of stderr.
        message = str(e)
        hint = None
        if "Executable doesn" in message or "playwright install" in message:
            hint = "Run: playwright install chromium"
        return _fail(f"{type(e).__name__}: {message.splitlines()[0]}", hint=hint)

    payload = {"ok": result.suggestion is not None, **result.to_dict()}
    return _emit(payload, EXIT_OK if payload["ok"] else EXIT_FAILED)


def cmd_add(args) -> int:
    result = probe_url(args.url, settings=_settings(args), verify=args.verify)
    if not result.ok or not result.best:
        return _emit({
            "ok": False,
            "url": args.url,
            "reason": "nothing to add: the probe found no usable candidate",
            "probe": result.to_dict(),
        }, EXIT_FAILED)

    best = result.best
    if not best.source_type:
        return _emit({
            "ok": False,
            "url": args.url,
            "reason": f"platform {best.platform} has no built-in adapter",
            "next_step": best.next_step,
            "probe": result.to_dict(),
        }, EXIT_FAILED)

    if args.verify and best.verified is False:
        return _emit({
            "ok": False,
            "url": args.url,
            "reason": "the candidate did not verify",
            "detail": best.verified_note,
            "probe": result.to_dict(),
        }, EXIT_FAILED)

    book = recipes.load(args.recipe)
    note = args.note or best.verified_note or f"Added from probe ({best.confidence} confidence)."
    action = book.upsert(recipes.Recipe(
        id=args.id,
        source_type=best.source_type,
        config=best.config,
        notes=note,
    ))
    path = book.save()

    return _emit({
        "ok": True,
        "action": action,
        "recipe_file": str(path),
        "source": {"id": args.id, "source_type": best.source_type, "config": best.config},
        "confidence": best.confidence,
        "evidence": best.evidence,
        "verified": best.verified,
    })


def cmd_list(args) -> int:
    try:
        book = recipes.load(args.recipe)
    except (ValueError, json.JSONDecodeError) as e:
        return _fail(str(e))
    return _emit({
        "ok": True,
        "recipe_file": str(args.recipe),
        "count": len(book),
        "sources": [r.to_dict() for r in book],
    })


def cmd_run(args) -> int:
    try:
        book = recipes.load(args.recipe)
    except (ValueError, json.JSONDecodeError) as e:
        return _fail(str(e))

    pool = book.enabled_sources() if args.include_quarantined else book.active_sources()
    selected = [r for r in pool if not args.only or r.id in args.only]

    skipped = [
        {"id": r.id, "reason": r.memory.quarantine_reason}
        for r in book.quarantined_sources()
        if not args.include_quarantined and (not args.only or r.id in args.only)
    ]

    if not selected:
        return _fail(
            "no sources to run",
            recipe_file=str(args.recipe),
            known=[r.id for r in book],
            quarantined=skipped,
            hint=("All matching sources are quarantined. Pass "
                  "--include-quarantined to run them anyway.") if skipped else None,
        )

    settings = _settings(args)
    runs = []
    for recipe in selected:
        record = run_source(
            recipe.id,
            recipe.source_type,
            recipe.config,
            settings=settings,
            limit=args.limit,
            extract=not args.no_extract,
            max_chars=args.max_chars,
        )
        run_payload = record.to_dict()

        if args.remember:
            urls = [d.source_url for d in record.documents]
            # Check the learned shape before recording, so the comparison is
            # against what we knew going in, not what we just learned.
            if record.status == "ok" and not recipe.memory.matches_learned_shape(urls):
                run_payload["shape_changed"] = {
                    "learned_pattern": recipe.memory.doc_url_pattern,
                    "note": (
                        "The source returned documents whose URLs no longer "
                        "match the shape it used to produce. It may still be "
                        "working, or the site was redesigned and this is now "
                        "fetching the wrong thing. Worth a look."
                    ),
                }
            recipe.memory.record(record.status, urls, error=record.error or record.diagnosis)
            run_payload["memory"] = recipe.memory.to_dict()

        runs.append(run_payload)

    if args.remember:
        book.save()

    summary: dict[str, int] = {}
    for run in runs:
        summary[run["status"]] = summary.get(run["status"], 0) + 1

    # Only a clean run exits 0. "partial" means documents came back without
    # usable text, which looks like success in a log and is not one, so an
    # unattended caller has to be able to see it in the exit code.
    clean = summary.get("ok", 0) == len(runs)

    payload = {
        "ok": clean,
        "recipe_file": str(args.recipe),
        "summary": summary,
        "runs": runs,
    }
    if skipped:
        payload["skipped_quarantined"] = skipped
    if args.remember:
        payload["remembered"] = True
        newly = [r["source_id"] for r in runs if r.get("memory", {}).get("quarantined")]
        if newly:
            payload["newly_quarantined"] = newly

    return _emit(payload, EXIT_OK if clean else EXIT_FAILED)


# ── Parser ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docpipe",
        description="Fetch documents from web sources and turn them into text. "
                    "Every command prints JSON to stdout; logs go to stderr.",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="silence logs on stderr")

    # Repeated on every subcommand so `docpipe probe <url> -q` works as well
    # as `docpipe -q probe <url>`. SUPPRESS keeps the subcommand's default
    # from overwriting the flag when it was given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="silence logs on stderr")

    sub = parser.add_subparsers(dest="command", required=True)

    def add_http_flags(p):
        p.add_argument("--raw-dir", help="where downloads land (default: data/raw)")
        p.add_argument("--timeout", type=int, help="per-request timeout in seconds")

    p = sub.add_parser("sources", parents=[common], help="list the registered adapters")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("schema", parents=[common], help="JSON Schema for an adapter's config")
    p.add_argument("source_type", nargs="?", help="omit for every adapter")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("probe", parents=[common], help="classify a URL: which adapter reads it, with what config")
    p.add_argument("url")
    p.add_argument("--verify", action="store_true",
                   help="actually run the top candidate for one document")
    add_http_flags(p)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("validate", parents=[common], help="check a config against an adapter's schema")
    p.add_argument("source_type")
    p.add_argument("--config", help="JSON string, or @path/to/file.json")
    p.add_argument("--config-file")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("fetch", parents=[common], help="run one adapter now, without a recipe file")
    p.add_argument("source_type")
    p.add_argument("--id", default="adhoc", help="names the download dir and log prefix")
    p.add_argument("--config", help="JSON string, or @path/to/file.json")
    p.add_argument("--config-file")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--no-extract", action="store_true", help="download only, skip text extraction")
    p.add_argument("--max-chars", type=int, help="report how the text would be truncated to fit")
    p.add_argument("--no-ocr", action="store_true")
    add_http_flags(p)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("extract", parents=[common], help="extract text from local PDF or HTML files")
    p.add_argument("paths", nargs="+")
    p.add_argument("--out", help="write .txt files here instead of inlining the text")
    p.add_argument("--full", action="store_true", help="inline the whole text, not the first 2000 chars")
    p.add_argument("--max-chars", type=int, help="smart-truncate to this budget")
    p.add_argument("--profile", choices=["procurement", "generic"], default="procurement")
    p.add_argument("--no-ocr", action="store_true")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("analyze", parents=[common],
                       help="send extracted text to a model and get structured data back")
    p.add_argument("paths", nargs="+", help="files to analyze, or - for stdin")
    p.add_argument("--prompt", help="what to extract, in your words")
    p.add_argument("--prompt-file", help="read the prompt from a file")
    p.add_argument("--schema", help="path to a JSON Schema constraining the output")
    p.add_argument("--provider", choices=["anthropic", "gemini"],
                   help="default: whichever key is in the environment")
    p.add_argument("--model")
    p.add_argument("--fallback-model", help="used when the primary is busy")
    p.add_argument("--max-chars", type=int, default=400_000,
                   help="smart-truncate the input to this budget first")
    p.add_argument("--out", help="write one JSON file per input instead of inlining")
    p.add_argument("--no-ocr", action="store_true")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("agent-kit", parents=[common],
                       help="install the skill and subagent definitions into a project")
    p.add_argument("--into", default=".", help="project root (default: here)")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    p.set_defaults(func=cmd_agent_kit)

    p = sub.add_parser("sniff", parents=[common],
                       help="load a page in a browser and report the JSON API behind it")
    p.add_argument("url")
    p.add_argument("--wait", type=int, default=8, help="seconds to watch after load")
    p.add_argument("--include-all", action="store_true",
                   help="report every response, not just JSON")
    add_http_flags(p)
    p.set_defaults(func=cmd_sniff)

    p = sub.add_parser("add", parents=[common], help="probe a URL and write the result into a recipe file")
    p.add_argument("recipe")
    p.add_argument("url")
    p.add_argument("--id", required=True)
    p.add_argument("--note")
    p.add_argument("--verify", action="store_true", help="refuse to add a candidate that does not work")
    add_http_flags(p)
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", parents=[common], help="show the sources in a recipe file")
    p.add_argument("recipe")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("run", parents=[common], help="run every source in a recipe file")
    p.add_argument("recipe")
    p.add_argument("--only", action="append", help="run just this id (repeatable)")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--no-extract", action="store_true")
    p.add_argument("--max-chars", type=int)
    p.add_argument("--no-ocr", action="store_true")
    p.add_argument("--remember", action="store_true",
                   help="write what this run taught us back into the recipe file")
    p.add_argument("--include-quarantined", action="store_true",
                   help="also run sources quarantined after repeated failures")
    add_http_flags(p)
    p.set_defaults(func=cmd_run)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "quiet", False):
        logging.getLogger("docpipe").setLevel(logging.ERROR)
        for name in list(logging.root.manager.loggerDict):
            if name.startswith("docpipe"):
                logging.getLogger(name).setLevel(logging.ERROR)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return _fail("interrupted")


if __name__ == "__main__":
    sys.exit(main())
