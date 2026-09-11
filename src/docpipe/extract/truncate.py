"""Smart truncation for documents that exceed an LLM context budget.

Replaces the naive `text[:max_chars]` cut, which loses the signal whenever
the relevant content sits after the boundary (a 1.67M-char agenda packet
whose one real contract award appears at char 1.6M).

Four steps, each applied only if the document is still over budget:

0. **Trim a safe filler tail.** Detect end-of-document markers that mean
   the reader has crossed from substance into boilerplate (policy manuals,
   glossaries, administrative regulations) and drop from there. Runs even
   when the document already fits, since that text is pure token cost.
   Conservative: the marker must appear in the back portion of the
   document, and the dropped chunk must be substantial.

1. **Trim an aggressive tail.** Markers like "EXHIBIT A" that *might* be
   real content. Only when still over budget, where the alternative is
   losing content anyway.

2. **Keep windows.** Locate every passage matching the profile's
   `keep_patterns` and pull a fixed window of context around each.
   Overlapping windows merge, order is preserved.

3. **Head + middle + tail.** Last resort: a head snippet, as much of the
   windowed middle as fits, and a tail snippet, joined with a gap marker
   so the model can tell the text is non-contiguous.

What counts as "filler" and what counts as "worth keeping" is entirely in
the `TruncationProfile`. The default profile targets public procurement
records: tenders, contract awards, budget approvals and the meeting
records that carry them. Build your own with `keyword_profile()` or by
instantiating `TruncationProfile` directly.

Returns the truncated text plus metadata describing which path fired, so
callers can log and audit how much content was dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence, TypedDict


class TruncationMeta(TypedDict, total=False):
    original_chars: int
    final_chars: int
    method: str
    tail_dropped_at: int
    windows_count: int
    profile: str


@dataclass(frozen=True)
class TruncationProfile:
    """Domain vocabulary + tuning knobs for `truncate_smart`.

    safe_tail_patterns
        Markers that almost never appear inside content worth keeping.
        Trimmed on every document, even one already under budget.
    aggressive_tail_patterns
        Markers that may collide with real content. Trimmed only when the
        document is over budget.
    keep_patterns
        Passages worth preserving. Step 2 builds a context window around
        every match. An empty tuple skips straight to head+tail.
    window_chars
        Half-width of the context window around each match.
    tail_marker_min_offset_pct
        A tail marker is only honored past this fraction of the document.
        An "Exhibit A" referenced at 5% is a live cross-reference; the same
        string at 70% is where the attachments begin.
    min_tail_drop_chars
        Skip a tail trim that would save less than this. Not worth the risk
        of a false positive on a short document.
    gap_marker
        Inserted between non-contiguous slices. Plain newlines read as a
        section break to a model, where a bracketed "[snip]" marker reads
        as alien text and makes some models hedge.
    """

    name: str = "custom"
    safe_tail_patterns: tuple[re.Pattern[str], ...] = ()
    aggressive_tail_patterns: tuple[re.Pattern[str], ...] = ()
    keep_patterns: tuple[re.Pattern[str], ...] = ()
    window_chars: int = 5_000
    tail_marker_min_offset_pct: float = 0.40
    min_tail_drop_chars: int = 5_000
    fallback_head_chars: int = 30_000
    fallback_tail_chars: int = 10_000
    gap_marker: str = "\n\n\n"


# ── Built-in profile: public procurement ────────────────────────────────
# Tail markers: the point where minutes stop recording actions and start
# reprinting static governance text.
_PROCUREMENT_SAFE_TAIL: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bBOARD\s+POLICY\s+MANUAL\b", re.IGNORECASE),
    re.compile(r"\bPOLICY\s+MANUAL\b", re.IGNORECASE),
    re.compile(r"\bBOARD\s+POLICIES\b", re.IGNORECASE),
    re.compile(r"\bSCHOOL\s+BOARD\s+POLICY\s+\d{3,4}\b", re.IGNORECASE),
    # Numbered policy headers at line start (large-agency style).
    re.compile(r"^\s*POLICY\s+\d{3,4}(?:\.\d+)?\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bSTANDARD\s+OPERATING\s+PROCEDURES?\b", re.IGNORECASE),
    re.compile(r"\bGLOSSARY\s+OF\s+TERMS?\b", re.IGNORECASE),
    re.compile(r"\bADMINISTRATIVE\s+REGULATIONS?\b", re.IGNORECASE),
)

_PROCUREMENT_AGGRESSIVE_TAIL: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bAPPENDIX\s+[A-Z]\b", re.IGNORECASE),
    re.compile(r"\bEXHIBIT\s+[A-Z]\b", re.IGNORECASE),
    re.compile(r"\bATTACHMENT\s+[A-Z0-9]\b", re.IGNORECASE),
)

# Each pattern must imply a vendor + dollars or a real procurement event.
# Bare action verbs ("AUTHORIZE the", "RESOLVED that") were removed after
# they matched governance prose like "AUTHORIZE the Network Office to
# mediate disputes", drowning out the actual signals.
_PROCUREMENT_KEEP: tuple[re.Pattern[str], ...] = (
    # Verb + procurement noun within 80 chars (the object of the action).
    re.compile(
        r"\b(?:AUTHORIZE|APPROVE|ADOPT|RATIFY|AWARD|RENEW|EXECUTE)\b"
        r"(?:[^.\n]{0,80})\b"
        r"(?:contract|agreement|amendment|purchase\s+order|renewal|RFP|RFQ|"
        r"grant\s+award|award)\b",
        re.IGNORECASE,
    ),
    # Named-vendor phrasing. The capitalized follower keeps this from
    # matching generic phrases like "agreement with the union".
    re.compile(
        r"\b(?:Contract|Agreement|Amendment|Renewal|Partnership|MOU)\s+with\s+"
        r"(?:the\s+)?[A-Z][A-Za-z0-9&.\-]+",
    ),
    re.compile(
        r"\bRequest\s+for\s+(?:Proposal|Quotation|Bid|Qualification)s?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:RFP|RFQ|RFB|IFB)[- ]?\d", re.IGNORECASE),
    re.compile(r"\bPurchase\s+Order\s+(?:#|No\.?|number)", re.IGNORECASE),
    # Money, floored at five figures so we don't trigger on every line of a
    # budget table.
    re.compile(r"\$\s?\d{2,3},\d{3}\b(?!\s*,)"),
    re.compile(r"\$\s?\d{1,3}(?:,\d{3}){2,}"),
    re.compile(r"\bgrant\s+award\b", re.IGNORECASE),
    re.compile(r"\bin\s+the\s+amount\s+of\s+\$", re.IGNORECASE),
)

PROCUREMENT_PROFILE = TruncationProfile(
    name="procurement",
    safe_tail_patterns=_PROCUREMENT_SAFE_TAIL,
    aggressive_tail_patterns=_PROCUREMENT_AGGRESSIVE_TAIL,
    keep_patterns=_PROCUREMENT_KEEP,
)

# No vocabulary at all: pure head + middle + tail. Use when you have no
# idea what the document looks like.
GENERIC_PROFILE = TruncationProfile(name="generic")

DEFAULT_PROFILE = PROCUREMENT_PROFILE


def keyword_profile(
    keep_keywords: Iterable[str],
    tail_keywords: Iterable[str] = (),
    name: str = "keywords",
    **kwargs,
) -> TruncationProfile:
    """Build a profile from plain keywords, no regex required.

        profile = keyword_profile(
            keep_keywords=["adverse event", "dosage", "contraindicat"],
            tail_keywords=["References", "Bibliography"],
        )

    Keywords match case-insensitively on word boundaries. Anything more
    specific wants a hand-written `TruncationProfile`.
    """
    def compile_all(words: Iterable[str]) -> tuple[re.Pattern[str], ...]:
        return tuple(
            re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE) for w in words
        )

    return TruncationProfile(
        name=name,
        safe_tail_patterns=compile_all(tail_keywords),
        keep_patterns=compile_all(keep_keywords),
        **kwargs,
    )


# ── Internals ───────────────────────────────────────────────────────────


def _find_tail_drop_offset(
    text: str,
    patterns: Sequence[re.Pattern[str]],
    profile: TruncationProfile,
) -> int | None:
    """Return the char offset where the filler tail begins, or None.

    The marker must sit past `tail_marker_min_offset_pct` of the document
    AND the dropped chunk must be at least `min_tail_drop_chars` long, so a
    short document with a stray match is never gutted.
    """
    threshold = int(len(text) * profile.tail_marker_min_offset_pct)
    earliest: int | None = None
    for pat in patterns:
        for m in pat.finditer(text):
            if m.start() < threshold:
                continue
            if len(text) - m.start() < profile.min_tail_drop_chars:
                continue
            if earliest is None or m.start() < earliest:
                earliest = m.start()
            break
    return earliest


def _keep_offsets(text: str, profile: TruncationProfile) -> list[int]:
    """Sorted start offsets of every keep-pattern match."""
    offsets: set[int] = set()
    for pat in profile.keep_patterns:
        for m in pat.finditer(text):
            offsets.add(m.start())
    return sorted(offsets)


def _windows_from_offsets(
    text: str, offsets: list[int], profile: TruncationProfile
) -> list[tuple[int, int]]:
    """Build +/- window_chars spans around each offset, merging overlaps."""
    spans: list[tuple[int, int]] = []
    for off in offsets:
        start = max(0, off - profile.window_chars)
        end = min(len(text), off + profile.window_chars)
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    return spans


def _slice_and_join(
    text: str, spans: list[tuple[int, int]], profile: TruncationProfile
) -> str:
    """Join non-contiguous spans with the profile's gap marker."""
    pieces: list[str] = []
    prev_end = -1
    for start, end in spans:
        if pieces and start > prev_end:
            pieces.append(profile.gap_marker)
        pieces.append(text[start:end])
        prev_end = end
    return "".join(pieces)


def truncate_smart(
    text: str,
    max_chars: int,
    profile: TruncationProfile = DEFAULT_PROFILE,
) -> tuple[str, TruncationMeta]:
    """Truncate `text` to fit under `max_chars`. See the module docstring.

    Returns the truncated string plus metadata: `method` names which step
    produced the result ("no_op", "trimmed_safe_tail",
    "trimmed_aggressive_tail", "trimmed_tail+windows", "head_windows_tail").
    """
    original_chars = len(text)
    meta: TruncationMeta = {
        "original_chars": original_chars,
        "final_chars": original_chars,
        "method": "no_op",
        "profile": profile.name,
    }

    # ── Step 0: safe-tail trim (always) ────────────────────────────────
    safe_off = _find_tail_drop_offset(text, profile.safe_tail_patterns, profile)
    if safe_off is not None:
        text = text[:safe_off]
        meta["tail_dropped_at"] = safe_off
        meta["method"] = "trimmed_safe_tail"

    if len(text) <= max_chars:
        meta["final_chars"] = len(text)
        return text, meta

    # ── Step 1: aggressive-tail trim ───────────────────────────────────
    aggressive_off = _find_tail_drop_offset(
        text, profile.aggressive_tail_patterns, profile
    )
    if aggressive_off is not None:
        text = text[:aggressive_off]
        meta["tail_dropped_at"] = aggressive_off  # overrides the safe offset
        meta["method"] = "trimmed_aggressive_tail"
        if len(text) <= max_chars:
            meta["final_chars"] = len(text)
            return text, meta

    # ── Step 2: keep windows ───────────────────────────────────────────
    offsets = _keep_offsets(text, profile)
    if offsets:
        spans = _windows_from_offsets(text, offsets, profile)
        windowed = _slice_and_join(text, spans, profile)
        meta["windows_count"] = len(spans)
        if len(windowed) <= max_chars:
            meta["method"] = "trimmed_tail+windows"
            meta["final_chars"] = len(windowed)
            return windowed, meta
        text_for_step3 = windowed
    else:
        text_for_step3 = text

    # ── Step 3: head + middle + tail ───────────────────────────────────
    head_chars = profile.fallback_head_chars
    tail_chars = profile.fallback_tail_chars
    reserved = head_chars + tail_chars + 2 * len(profile.gap_marker)
    window_budget = max(0, max_chars - reserved)

    head = text_for_step3[:head_chars]
    tail = text_for_step3[-tail_chars:] if len(text_for_step3) > tail_chars else ""
    middle = text_for_step3[head_chars : len(text_for_step3) - tail_chars]
    middle = middle[:window_budget]

    parts: list[str] = [head]
    if middle:
        parts.append(profile.gap_marker)
        parts.append(middle)
    if tail:
        parts.append(profile.gap_marker)
        parts.append(tail)
    result = "".join(parts)
    meta["method"] = "head_windows_tail"
    meta["final_chars"] = len(result)
    return result, meta
