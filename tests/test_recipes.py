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


# ── Memory: what running a source teaches us about it ───────────────────


def test_a_clean_run_records_success_and_learns_the_url_shape():
    memory = recipes.SourceMemory()
    memory.record("ok", [
        "https://example.org/files/2026-06-15.pdf",
        "https://example.org/files/2026-05-11.pdf",
    ])
    assert memory.success_count == 1
    assert memory.consecutive_failures == 0
    assert memory.last_ok_at is not None
    assert memory.doc_url_pattern == r"^https://example\.org/files/[0-9-]+\.pdf$"


def test_partial_counts_as_failure():
    """Documents with no usable text are a failure of the one thing the
    source exists to do."""
    memory = recipes.SourceMemory()
    memory.record("partial", ["https://example.org/a.pdf"])
    assert memory.failure_count == 1
    assert memory.success_count == 0


def test_quarantine_after_repeated_failures_and_release_on_success():
    memory = recipes.SourceMemory(success_count=1, last_ok_at="2026-01-01T00:00:00")
    for _ in range(recipes.QUARANTINE_AFTER_FAILURES):
        memory.record("empty", [], error="nothing matched")
    assert memory.quarantined
    assert "consecutive failures" in memory.quarantine_reason

    memory.record("ok", ["https://example.org/a.pdf"])
    assert not memory.quarantined
    assert memory.quarantine_reason is None
    assert memory.consecutive_failures == 0


def test_a_source_that_never_worked_gets_a_different_diagnosis():
    """Never succeeding points at a bad config, not at a site that broke."""
    memory = recipes.SourceMemory()
    for _ in range(recipes.QUARANTINE_AFTER_FAILURES):
        memory.record("error", [], error="bad config")
    assert memory.quarantined
    assert "never succeeded" in memory.quarantine_reason
    assert "probe --verify" in memory.quarantine_reason


def test_url_shape_change_is_detected():
    memory = recipes.SourceMemory()
    memory.record("ok", [
        "https://example.org/files/2026-06-15.pdf",
        "https://example.org/files/2026-05-11.pdf",
    ])
    assert memory.matches_learned_shape(["https://example.org/files/2026-07-20.pdf"])
    assert not memory.matches_learned_shape(["https://example.org/oops/not-found.html"])


def test_url_pattern_needs_a_consistent_shape():
    assert recipes.derive_url_pattern(["https://example.org/a.pdf"]) is None
    assert recipes.derive_url_pattern([
        "https://example.org/a/1.pdf", "https://other.org/b/2.pdf",
    ]) is None


def test_memory_survives_a_round_trip_and_an_upsert(tmp_path):
    path = tmp_path / "s.json"
    book = recipes.load(path)
    recipe = recipes.Recipe("acme", "pdf_direct", {"page_url": "https://a"})
    recipe.memory.record("ok", ["https://a/1.pdf", "https://a/2.pdf"])
    book.upsert(recipe)
    book.save()

    reloaded = recipes.load(path)
    assert reloaded.get("acme").memory.success_count == 1
    assert reloaded.get("acme").memory.doc_url_pattern

    # Re-probing a source must not wipe what we know about it.
    reloaded.upsert(recipes.Recipe("acme", "html_page", {"page_url": "https://a"}))
    assert reloaded.get("acme").memory.success_count == 1


def test_quarantined_sources_are_excluded_from_a_routine_run(tmp_path):
    book = recipes.load(tmp_path / "s.json")
    healthy = recipes.Recipe("healthy", "pdf_direct", {})
    broken = recipes.Recipe("broken", "pdf_direct", {})
    broken.memory.quarantined = True
    broken.memory.quarantine_reason = "3 consecutive failures"
    book.upsert(healthy)
    book.upsert(broken)

    assert [r.id for r in book.active_sources()] == ["healthy"]
    assert [r.id for r in book.quarantined_sources()] == ["broken"]
    assert len(book.enabled_sources()) == 2
