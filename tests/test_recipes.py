"""Tests for the recipe file an agent writes and re-reads."""
import json

import pytest

from docpipe import recipes


def test_missing_file_loads_as_an_empty_book(tmp_path):
    """An agent appends to a path that does not exist yet on the first run."""
    book = recipes.load(tmp_path / "new.json")
    assert len(book) == 0
    assert book.path == tmp_path / "new.json"


def test_upsert_is_idempotent_on_id(tmp_path):
    book = recipes.load(tmp_path / "s.json")
    assert book.upsert(recipes.Recipe("acme", "pdf_direct", {"page_url": "https://a"})) == "added"
    assert book.upsert(recipes.Recipe("acme", "html_page", {"page_url": "https://b"})) == "updated"
    assert len(book) == 1
    assert book.get("acme").source_type == "html_page"


def test_round_trip(tmp_path):
    path = tmp_path / "s.json"
    book = recipes.load(path)
    book.upsert(recipes.Recipe("acme", "pdf_direct", {"page_url": "https://a"}, notes="verified"))
    book.save()

    reloaded = recipes.load(path)
    assert len(reloaded) == 1
    assert reloaded.get("acme").notes == "verified"
    assert json.loads(path.read_text())["version"] == recipes.VERSION


def test_disabled_sources_are_skipped_by_runs(tmp_path):
    book = recipes.load(tmp_path / "s.json")
    book.upsert(recipes.Recipe("on", "pdf_direct", {}))
    book.upsert(recipes.Recipe("off", "pdf_direct", {}, enabled=False))
    assert [r.id for r in book.enabled_sources()] == ["on"]


def test_malformed_file_says_what_is_wrong(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"sources": [{"id": "acme"}]}))
    with pytest.raises(ValueError, match="source_type"):
        recipes.load(path)

    path.write_text(json.dumps({"nope": []}))
    with pytest.raises(ValueError, match="not a recipe file"):
        recipes.load(path)
