"""Tests for the optional LLM analysis layer.

No network: the provider functions are the seam, so they get replaced.
What is worth testing here is everything around the call, which is the
part that decides whether a batch of a few hundred documents survives.
"""
import json

import pytest

from docpipe import analyze as module
from docpipe.analyze import AnalysisError, analyze, available_providers

SCHEMA = {
    "type": "object",
    "properties": {"vendor": {"type": "string"}},
    "required": ["vendor"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def no_ambient_keys(monkeypatch):
    """Never let the developer's own keys decide a test's outcome."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def fake_provider(returns=None, raises=None, record=None):
    def provider(text, prompt, schema, model, max_output_tokens):
        if record is not None:
            record.append({"model": model, "text": text, "prompt": prompt, "schema": schema})
        if raises:
            raise raises
        return returns
    return provider


def test_no_credentials_is_a_setup_error(monkeypatch):
    """Raised, not returned: a missing key is a misconfiguration the caller
    must fix, not a per-document failure to log and move past."""
    with pytest.raises(AnalysisError, match="No provider credentials"):
        analyze("text", prompt="summarize")


def test_provider_is_picked_from_the_environment(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    assert available_providers() == ["gemini"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert available_providers() == ["anthropic", "gemini"]

    monkeypatch.setattr(module, "_ANTHROPIC", fake_provider(returns='{"vendor": "Acme"}'))
    result = analyze("text", prompt="extract", schema=SCHEMA)
    assert result.provider == "anthropic"


def test_unknown_provider_is_rejected(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with pytest.raises(AnalysisError, match="Unknown provider"):
        analyze("text", prompt="x", provider="llama")


def test_structured_output_is_parsed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(module, "_ANTHROPIC", fake_provider(returns='{"vendor": "Acme"}'))
    result = analyze("text", prompt="extract", schema=SCHEMA)
    assert result.ok
    assert result.data == {"vendor": "Acme"}
    assert result.model == "claude-opus-5"


def test_fenced_json_is_tolerated(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr(
        module, "_GEMINI", fake_provider(returns='```json\n{"vendor": "Acme"}\n```'),
    )
    result = analyze("text", prompt="extract", schema=SCHEMA)
    assert result.data == {"vendor": "Acme"}


def test_unparseable_json_under_a_schema_is_a_failure(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(module, "_ANTHROPIC", fake_provider(returns="I could not find any."))
    result = analyze("text", prompt="extract", schema=SCHEMA)
    assert not result.ok
    assert "usable JSON" in result.error


def test_prose_is_fine_when_no_schema_was_asked_for(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(module, "_ANTHROPIC", fake_provider(returns="A summary in prose."))
    result = analyze("text", prompt="summarize")
    assert result.ok
    assert result.text == "A summary in prose."
    assert result.data is None


def test_a_busy_model_falls_back_to_the_stand_in(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    calls = []

    def provider(text, prompt, schema, model, max_output_tokens):
        calls.append(model)
        if len(calls) == 1:
            raise RuntimeError("529 overloaded_error")
        return '{"vendor": "Acme"}'

    monkeypatch.setattr(module, "_ANTHROPIC", provider)
    result = analyze("text", prompt="extract", schema=SCHEMA)
    assert result.ok
    assert result.used_fallback
    assert calls == ["claude-opus-5", "claude-sonnet-5"]


def test_a_real_error_does_not_burn_a_second_call(monkeypatch):
    """A malformed request fails the same way twice. Retrying it just costs
    money and hides the actual problem."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    calls = []

    def provider(text, prompt, schema, model, max_output_tokens):
        calls.append(model)
        raise ValueError("400 invalid schema: additionalProperties required")

    monkeypatch.setattr(module, "_ANTHROPIC", provider)
    result = analyze("text", prompt="extract", schema=SCHEMA)
    assert not result.ok
    assert len(calls) == 1
    assert "invalid schema" in result.error


def test_oversized_input_is_truncated_and_says_so(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    record = []
    monkeypatch.setattr(
        module, "_ANTHROPIC", fake_provider(returns='{"vendor": "Acme"}', record=record),
    )
    huge = "Filler sentence about nothing. " * 50_000  # ~1.5M chars
    result = analyze(huge, prompt="extract", schema=SCHEMA, max_input_chars=50_000)

    assert result.ok
    assert result.input_chars <= 50_000
    assert len(record[0]["text"]) <= 50_000
    assert result.truncation["original_chars"] == len(huge)
    assert any("reduced" in w for w in result.warnings), "silent truncation would be a lie"


def test_empty_text_never_reaches_the_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    called = []
    monkeypatch.setattr(module, "_ANTHROPIC", fake_provider(returns="x", record=called))
    result = analyze("   ", prompt="extract")
    assert not result.ok
    assert called == [], "an empty document is not worth a paid call"


def test_explicit_models_are_respected(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    record = []
    monkeypatch.setattr(module, "_GEMINI", fake_provider(returns="{}", record=record))
    analyze("text", prompt="x", model="gemini-3.1-flash-lite")
    assert record[0]["model"] == "gemini-3.1-flash-lite"


def test_result_is_json_serializable(monkeypatch):
    """The CLI emits this straight to stdout."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(module, "_ANTHROPIC", fake_provider(returns='{"vendor": "Acme"}'))
    result = analyze("text", prompt="extract", schema=SCHEMA)
    json.dumps(result.to_dict())
