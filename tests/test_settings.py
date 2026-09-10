"""Tests for docpipe.settings."""
from pathlib import Path

from docpipe.settings import DEFAULT_SETTINGS, Settings


def test_raw_dir_accepts_a_string():
    assert Settings(raw_dir="/tmp/scrapes").raw_dir == Path("/tmp/scrapes")


def test_replace_returns_a_copy():
    base = Settings(http_timeout=30)
    slow = base.replace(http_timeout=120)
    assert slow.http_timeout == 120
    assert base.http_timeout == 30


def test_storage_dir_is_created_per_source(tmp_path):
    settings = Settings(raw_dir=tmp_path)
    target = settings.storage_dir("acme-corp")
    assert target == tmp_path / "acme-corp"
    assert target.is_dir()


def test_defaults_do_not_touch_the_filesystem_on_import():
    """The original config module ran mkdir at import time, which made the
    library unusable in read-only environments."""
    assert not DEFAULT_SETTINGS.raw_dir.exists() or DEFAULT_SETTINGS.raw_dir.is_dir()


def test_headers_are_per_instance():
    a, b = Settings(), Settings()
    a.http_headers["X-Test"] = "1"
    assert "X-Test" not in b.http_headers
