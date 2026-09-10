"""Tests for the CLI contract an agent depends on: JSON on stdout, and
exit codes it can branch on without reading prose."""
import json

import pytest

from docpipe.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main
from tests.conftest import make_response


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_sources_lists_adapters_and_marks_aliases(capsys):
    code, payload = run(capsys, "sources")
    assert code == EXIT_OK and payload["ok"]
    by_name = {s["source_type"]: s for s in payload["sources"]}
    assert "pdf_direct" in by_name
    assert by_name["district_api"]["alias_of"] == "json_api"
    assert by_name["pdf_direct"]["summary"]


def test_schema_returns_a_usable_json_schema(capsys):
    code, payload = run(capsys, "schema", "pdf_direct")
    assert code == EXIT_OK
    schema = payload["schemas"]["pdf_direct"]
    assert "page_url" in schema["properties"]
    assert "pdf_link_pattern" in schema["properties"]


def test_schema_for_an_unknown_adapter_lists_the_real_ones(capsys):
    code, payload = run(capsys, "schema", "nope")
    assert code == EXIT_USAGE
    assert payload["ok"] is False
    assert "pdf_direct" in payload["available"]


def test_validate_accepts_a_good_config(capsys):
    code, payload = run(
        capsys, "validate", "pdf_direct", "--config", '{"page_url": "https://example.org"}'
    )
    assert code == EXIT_OK and payload["ok"]
    assert payload["config"]["page_url"] == "https://example.org"


def test_validate_explains_a_bad_config(capsys):
    code, payload = run(capsys, "validate", "pdf_direct", "--config", '{"nope": 1}')
    assert code == EXIT_FAILED
    assert payload["ok"] is False
    assert payload["errors"]
    assert "docpipe schema pdf_direct" in payload["hint"]


def test_validate_rejects_unparseable_json(capsys):
    code, payload = run(capsys, "validate", "pdf_direct", "--config", "{not json")
    assert code == EXIT_USAGE
    assert "readable JSON" in payload["error"]


def test_stdout_carries_json_only(capsys, fake_http, tmp_path):
    """Logs go to stderr so an agent can pipe stdout straight into a parser."""
    fake_http(lambda url, **kw: make_response(text="<html><body>hi</body></html>"))
    main(["probe", "https://example.org", "--raw-dir", str(tmp_path)])
    captured = capsys.readouterr()
    json.loads(captured.out)  # raises if anything else leaked in


def test_probe_of_an_unreachable_url_exits_failed(capsys, monkeypatch, tmp_path):
    import requests

    import docpipe.http

    monkeypatch.setattr(
        docpipe.http.requests, "get",
        lambda url, **kw: (_ for _ in ()).throw(requests.RequestException("refused")),
    )
    code, payload = run(capsys, "probe", "https://example.org", "--raw-dir", str(tmp_path))
    assert code == EXIT_FAILED
    assert payload["platform"] == "unreachable"


def test_add_then_list_then_run(capsys, fake_http, tmp_path):
    """The loop an agent drives: discover, record, execute."""
    index = (
        "<html><body>"
        "<a href='/docs/a.pdf'>6-15-2026 Board Meeting</a>"
        "<a href='/docs/b.pdf'>6-8-2026 Work Session</a>"
        "<a href='/docs/c.pdf'>5-11-2026 Board Meeting</a>"
        "</body></html>"
    )

    def handler(url, **kw):
        if url.endswith(".pdf"):
            return make_response(content=b"%PDF-1.4", content_type="application/pdf")
        return make_response(text=index)

    fake_http(handler)
    recipe = tmp_path / "sources.json"

    code, payload = run(
        capsys, "add", str(recipe), "https://example.org/minutes",
        "--id", "acme", "--raw-dir", str(tmp_path / "raw"),
    )
    assert code == EXIT_OK
    assert payload["action"] == "added"
    assert payload["source"]["source_type"] == "pdf_direct"

    code, payload = run(capsys, "list", str(recipe))
    assert code == EXIT_OK and payload["count"] == 1

    code, payload = run(
        capsys, "run", str(recipe), "--limit", "1", "--raw-dir", str(tmp_path / "raw")
    )
    # The fake PDF yields no text, so the run is honest about being partial.
    assert payload["runs"][0]["status"] == "partial"
    assert code == EXIT_FAILED


def test_add_refuses_to_record_a_platform_it_cannot_read(capsys, fake_http, tmp_path):
    fake_http(lambda url, **kw: make_response(text="<html><body>shell</body></html>"))
    code, payload = run(
        capsys, "add", str(tmp_path / "s.json"),
        "https://go.boarddocs.com/pa/x/Board.nsf/Public", "--id", "x",
        "--raw-dir", str(tmp_path),
    )
    assert code == EXIT_FAILED
    assert "no built-in adapter" in payload["reason"]
    assert not (tmp_path / "s.json").exists(), "a failed probe must not write a recipe"


def test_run_on_an_unknown_id_lists_what_it_knows(capsys, tmp_path):
    recipe = tmp_path / "s.json"
    recipe.write_text(json.dumps({
        "version": 1,
        "sources": [{"id": "acme", "source_type": "pdf_direct", "config": {}}],
    }))
    code, payload = run(capsys, "run", str(recipe), "--only", "ghost")
    assert code == EXIT_USAGE
    assert payload["known"] == ["acme"]


def test_extract_reports_a_missing_file(capsys, tmp_path):
    code, payload = run(capsys, "extract", str(tmp_path / "ghost.pdf"))
    assert code == EXIT_FAILED
    assert payload["results"][0]["error"] == "file not found"


def test_extract_reads_html_and_finds_its_date(capsys, tmp_path):
    page = tmp_path / "minutes.html"
    page.write_text("<html><body><main>Regular Board Meeting Minutes March 18, 2026. "
                    "Motion carried.</main></body></html>")
    code, payload = run(capsys, "extract", str(page))
    assert code == EXIT_OK
    result = payload["results"][0]
    assert result["method"] == "html"
    assert result["doc_date"] == "2026-03-18"
    assert "Motion carried" in result["text"]


def test_quiet_works_before_and_after_the_subcommand(capsys):
    assert main(["-q", "sources"]) == EXIT_OK
    capsys.readouterr()
    assert main(["sources", "-q"]) == EXIT_OK


def test_unknown_command_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code != 0


def test_agent_kit_installs_and_does_not_clobber(capsys, tmp_path):
    code, payload = run(capsys, "agent-kit", "--into", str(tmp_path))
    assert code == EXIT_OK
    assert (tmp_path / ".claude/skills/docpipe/SKILL.md").exists()
    assert (tmp_path / ".claude/agents/source-finder.md").exists()
    assert payload["skipped"] == []

    # A second run must not overwrite a file the user has since edited.
    edited = tmp_path / ".claude/agents/source-finder.md"
    edited.write_text("my own version")
    code, payload = run(capsys, "agent-kit", "--into", str(tmp_path))
    assert str(edited) in payload["skipped"]
    assert edited.read_text() == "my own version"
    assert "--force" in payload["hint"]
