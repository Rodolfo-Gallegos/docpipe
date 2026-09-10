"""Tests for docpipe.registry."""
import pytest
from pydantic import Field, ValidationError

from docpipe import BaseSource, SourceConfig, available_sources, get_source, register_source
from docpipe.registry import _REGISTRY


@pytest.fixture
def unregister():
    """Remove any names a test registers, so the global registry stays clean."""
    added: list[str] = []
    yield added.append
    for name in added:
        _REGISTRY.pop(name, None)


class DemoConfig(SourceConfig):
    endpoint: str = Field(pattern=r"^https?://")
    depth: int = 1


class DemoSource(BaseSource):
    config_model = DemoConfig

    def fetch_documents(self, limit: int = 3):
        return []

    def to_text(self, document):
        return "", "none"


def test_builtins_are_registered():
    names = available_sources()
    for expected in ("pdf_direct", "html_page", "pdf_listing", "json_api", "playwright_render"):
        assert expected in names


def test_third_party_adapter_resolves_through_get_source(unregister):
    unregister("demo")
    register_source("demo")(DemoSource)
    source = get_source("demo", "acme", {"endpoint": "https://example.org"})
    assert isinstance(source, DemoSource)
    assert source.source_id == "acme"
    assert source.config.depth == 1


def test_unknown_source_type_names_the_alternatives():
    with pytest.raises(ValueError, match="Unknown source_type"):
        get_source("nope", "acme", {})


def test_registering_a_name_twice_raises(unregister):
    unregister("demo-dup")
    register_source("demo-dup")(DemoSource)

    class Other(DemoSource):
        pass

    with pytest.raises(ValueError, match="already registered"):
        register_source("demo-dup")(Other)


def test_config_is_validated_at_construction(unregister):
    unregister("demo-validate")
    register_source("demo-validate")(DemoSource)

    with pytest.raises(ValidationError):
        get_source("demo-validate", "acme", {"endpoint": "ftp://example.org"})

    # A misspelled key must fail loudly rather than be silently ignored.
    with pytest.raises(ValidationError):
        get_source("demo-validate", "acme", {"endpoint": "https://example.org", "dept": 2})


def test_an_already_validated_config_is_passed_through(unregister):
    unregister("demo-passthrough")
    register_source("demo-passthrough")(DemoSource)
    config = DemoConfig(endpoint="https://example.org", depth=7)
    assert get_source("demo-passthrough", "acme", config).config is config
