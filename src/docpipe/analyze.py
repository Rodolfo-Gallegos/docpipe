"""Optional: hand the extracted text to a model and get structured data back.

Everything else in docpipe is deterministic and works with no API key. This
module is the one exception, and it stays opt-in for that reason: install
an extra, set a key, and only then does anything here run.

The division of labour it assumes:

    docpipe            find, fetch, extract, fit to a budget   (no LLM)
    this module        text + your prompt + your schema -> JSON
    your project       what the JSON means

docpipe supplies no prompt and no schema. It cannot: what counts as a
useful field depends entirely on what you are collecting, and a library
that guesses at that produces a worse result than the caller who knows.
What it does supply is the plumbing that is the same every time: picking a
provider, fitting the text to the context budget, asking for JSON and
actually getting JSON back, and failing over when a model is busy.

    from docpipe.analyze import analyze

    result = analyze(
        text,
        prompt="List every contract award, with the vendor and the amount.",
        schema={
            "type": "object",
            "properties": {
                "awards": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "vendor": {"type": "string"},
                            "amount_usd": {"type": "number"},
                        },
                        "required": ["vendor", "amount_usd"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["awards"],
            "additionalProperties": False,
        },
    )
    result.data   # parsed JSON, schema-shaped

Providers: `anthropic` (pip install 'docpipe[anthropic]') and `gemini`
(pip install 'docpipe[gemini]'). With no `provider` argument, whichever key
is present in the environment wins, Anthropic first.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from docpipe.extract.truncate import DEFAULT_PROFILE, TruncationProfile, truncate_smart
from docpipe.logger import get_logger

logger = get_logger(__name__)

# Leaves room for the prompt, the schema and the response inside a large
# context window, while keeping the cost of one call predictable. Override
# per call when you know the model and the budget.
DEFAULT_MAX_INPUT_CHARS = 400_000

PROVIDER_ENV_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}

# Primary and stand-in per provider. The stand-in exists for capacity
# errors, not for cost: a 529 on a batch of a few hundred documents should
# not end the run.
#
# The Gemini entries are the rolling aliases on purpose. Pinned Gemini
# versions get retired for new users while still appearing in the model
# list, so a pin that worked when it was written returns 404 later with no
# warning. Pass `model=` to pin deliberately; the default should keep
# working in a project nobody has touched for a year.
DEFAULT_MODELS = {
    "anthropic": ("claude-opus-5", "claude-sonnet-5"),
    "gemini": ("gemini-flash-latest", "gemini-flash-lite-latest"),
}

# Substrings that mean "busy, try the other model" rather than "this
# request is wrong". Matched against the exception's repr, because the
# providers surface these as different exception types.
_TRANSIENT_HINTS = (
    "429", "500", "502", "503", "529",
    "overloaded", "unavailable", "rate_limit", "rate limit",
    "resource_exhausted", "resource exhausted", "quota",
    "timeout", "timed out", "deadline", "internal error",
    "high demand", "capacity",
)


class AnalysisError(RuntimeError):
    """The call could not be made or the response could not be used."""


@dataclass
class AnalysisResult:
    ok: bool
    provider: str
    model: Optional[str] = None
    data: Optional[Any] = None
    text: Optional[str] = None
    input_chars: int = 0
    truncation: Optional[dict[str, Any]] = None
    used_fallback: bool = False
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def available_providers() -> list[str]:
    """Providers with a key in the environment, most preferred first."""
    return [
        name for name in ("anthropic", "gemini")
        if any(os.getenv(var) for var in PROVIDER_ENV_KEYS[name])
    ]


def _is_transient(exc: BaseException) -> bool:
    message = repr(exc).lower()
    return any(hint in message for hint in _TRANSIENT_HINTS)


def _resolve_provider(provider: Optional[str]) -> str:
    if provider:
        if provider not in PROVIDER_ENV_KEYS:
            raise AnalysisError(
                f"Unknown provider {provider!r}. Known: {', '.join(PROVIDER_ENV_KEYS)}."
            )
        return provider

    found = available_providers()
    if not found:
        raise AnalysisError(
            "No provider credentials found. Set one of: "
            + ", ".join(sorted({v for vs in PROVIDER_ENV_KEYS.values() for v in vs}))
        )
    return found[0]


def _extract_json(raw: str) -> Any:
    """Parse a JSON response, tolerating a fenced block around it.

    Both providers honour a schema and return bare JSON. This is here for
    the case where a schema was not supplied but JSON was still asked for
    in the prompt, which is a reasonable thing for a caller to do.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return json.loads(text)


def analyze(
    text: str,
    prompt: str,
    schema: Optional[dict] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    fallback_model: Optional[str] = None,
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
    truncation_profile: TruncationProfile = DEFAULT_PROFILE,
    max_output_tokens: int = 16_000,
) -> AnalysisResult:
    """Send `text` plus `prompt` to a model, optionally constrained by `schema`.

    Oversized input is reduced with `truncate_smart` rather than cut, and
    the truncation metadata comes back on the result so the caller can see
    what was dropped before trusting the answer.

    Raises `AnalysisError` only for setup problems (no credentials, unknown
    provider, missing package). A failed call comes back as a result with
    `ok=False`, so a batch can keep going.
    """
    resolved = _resolve_provider(provider)
    result = AnalysisResult(ok=False, provider=resolved, input_chars=len(text))

    if not text or not text.strip():
        result.error = "nothing to analyze: the text is empty"
        return result

    if len(text) > max_input_chars:
        text, meta = truncate_smart(text, max_input_chars, profile=truncation_profile)
        result.truncation = dict(meta)
        result.input_chars = len(text)
        result.warnings.append(
            f"Input was reduced from {meta['original_chars']} to {len(text)} chars "
            f"via {meta['method']}. Anything the profile dropped is invisible to the model."
        )

    primary, stand_in = DEFAULT_MODELS[resolved]
    primary = model or primary
    stand_in = fallback_model or stand_in

    caller = _ANTHROPIC if resolved == "anthropic" else _GEMINI

    for attempt, chosen in enumerate((primary, stand_in)):
        if chosen is None or (attempt and chosen == primary):
            break
        try:
            raw = caller(text, prompt, schema, chosen, max_output_tokens)
        except AnalysisError:
            raise
        except Exception as e:
            if attempt == 0 and _is_transient(e) and stand_in and stand_in != primary:
                logger.warning(
                    f"{resolved} model {primary} looks busy ({type(e).__name__}); "
                    f"retrying with {stand_in}"
                )
                continue
            result.error = f"{type(e).__name__}: {e}"
            result.model = chosen
            result.used_fallback = attempt > 0
            return result

        result.model = chosen
        result.used_fallback = attempt > 0
        result.text = raw
        if schema:
            try:
                result.data = _extract_json(raw)
            except ValueError as e:
                result.error = f"the model did not return usable JSON: {e}"
                return result
        else:
            try:
                result.data = _extract_json(raw)
            except ValueError:
                # No schema was given, so prose is a legitimate answer.
                pass
        result.ok = True
        return result

    result.error = "no model was attempted"
    return result


# ── Providers ───────────────────────────────────────────────────────────
# Each takes the same arguments and returns the model's raw text. Imports
# are local so the base install needs neither SDK.


def _ANTHROPIC(
    text: str, prompt: str, schema: Optional[dict], model: str, max_output_tokens: int
) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise AnalysisError(
            "The anthropic package is required. "
            "Install with: pip install 'docpipe[anthropic]'"
        ) from e

    client = anthropic.Anthropic()
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_output_tokens,
        "messages": [{
            "role": "user",
            "content": f"{prompt}\n\n<document>\n{text}\n</document>",
        }],
    }
    if schema:
        request["output_config"] = {
            "format": {"type": "json_schema", "schema": schema}
        }

    response = client.messages.create(**request)

    # A refusal is a 200 with no usable content, so check before reading it.
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        raise AnalysisError(
            "The model declined this request"
            + (f" ({getattr(details, 'category', None)})" if details else "")
        )

    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise AnalysisError("The response carried no text block")


# Gemini's response_schema is an OpenAPI subset, not full JSON Schema: it
# rejects keys it does not know, including `additionalProperties` - which
# Anthropic, in the other direction, requires. A caller should be able to
# write one schema and have it work on either provider, so we translate.
_GEMINI_SCHEMA_KEYS = {
    "type", "format", "description", "nullable", "enum", "items",
    "properties", "required", "minItems", "maxItems", "anyOf",
    "propertyOrdering",
}


def _clean_schema_for_gemini(node: Any) -> Any:
    """Drop schema keys Gemini does not accept, recursively."""
    if isinstance(node, list):
        return [_clean_schema_for_gemini(item) for item in node]
    if not isinstance(node, dict):
        return node

    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _GEMINI_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {k: _clean_schema_for_gemini(v) for k, v in value.items()}
        else:
            cleaned[key] = _clean_schema_for_gemini(value)
    return cleaned


def _GEMINI(
    text: str, prompt: str, schema: Optional[dict], model: str, max_output_tokens: int
) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise AnalysisError(
            "The google-genai package is required. "
            "Install with: pip install 'docpipe[gemini]'"
        ) from e

    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    config: dict[str, Any] = {"max_output_tokens": max_output_tokens}
    if schema:
        config["response_mime_type"] = "application/json"
        config["response_schema"] = _clean_schema_for_gemini(schema)

    response = client.models.generate_content(
        model=model,
        contents=f"{prompt}\n\n<document>\n{text}\n</document>",
        config=types.GenerateContentConfig(**config),
    )
    if not getattr(response, "text", None):
        raise AnalysisError("The response carried no text")
    return response.text
