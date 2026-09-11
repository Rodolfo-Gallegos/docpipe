"""Deterministic date extraction from document text and from URLs.

No LLM involved: regex over the document head is faster, free, and easy to
debug. Formal documents state their date near the top, so we only scan the
first few KB (`search_head_chars`) and take the first plausible match.

Recognized formats, in priority order:

1. A synthetic header line the adapters emit (`DATE: 2026-05-20`), which
   comes from upstream metadata and outranks anything in the body.
2. Month-name dates: "JULY 21, 2025", "Sept. 3rd, 2024".
3. ISO dates: "2025-09-15".
4. US numeric: "9/15/2025", "09-15-25".
5. Month + ordinal day with no year ("Wednesday, May 20th"), inferring the
   year from the following 500 chars.
6. Month + year with no day ("MAY 2024"), normalized to day 1.

The hard part is not matching a date, it is rejecting the wrong one. A
document's head is full of dates that are not the document's date: fiscal
year ends, effective dates, deadlines, and forward references to the next
meeting. `DateProfile.exclude_*` patterns inspect the text immediately
before each match and skip those. Lookback stops at the previous sentence
boundary so a phrase from the prior sentence cannot taint the current date.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_MONTH_ALT = (
    r"january|jan|february|feb|march|mar|april|apr|may|june|jun|"
    r"july|jul|august|aug|september|sept|sep|october|oct|november|nov|"
    r"december|dec"
)

_MONTH_NAME_RE = re.compile(
    rf"\b(?P<month>{_MONTH_ALT})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)

# Fallback for documents that print "Wednesday, May 20th" without restating
# the year (it lives only in the footer). The ordinal suffix is required so
# we don't greedily capture "May 12" out of running prose.
_MONTH_DAY_NO_YEAR_RE = re.compile(
    rf"\b(?P<month>{_MONTH_ALT})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)\b",
    re.IGNORECASE,
)

_MONTH_YEAR_RE = re.compile(
    rf"\b(?P<month>{_MONTH_ALT})\.?\s+(?P<year>20\d{{2}})\b",
    re.IGNORECASE,
)

_LOOSE_YEAR_RE = re.compile(r"\b(20[1-3]\d)\b")
_ISO_RE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\b")
_SLASH_RE = re.compile(r"\b(?P<month>\d{1,2})[/-](?P<day>\d{1,2})[/-](?P<year>\d{2,4})\b")
_SENTENCE_BOUNDARY = re.compile(r"[.!?\n]")

# Phrases that directly precede a date which is NOT the document's date.
# Anchored: must run right up to the date, at most punctuation between.
DEFAULT_EXCLUDE_ANCHORED = re.compile(
    r"(?ix)(?:"
    r"fiscal\s+year\s+(?:ended|ending|ends|end)|"
    r"year\s+(?:ended|ending|ends)|"
    r"fy\s+(?:ended|ending)|"
    r"effective(?:\s+(?:on|date))?|"
    r"deadline|"
    r"due\s+(?:by|date|on)|"
    r"expires?(?:\s+on)?|"
    r"valid\s+(?:until|through)|"
    r"as\s+of"
    r")\s*[:,-]?\s*$"
)

# Forward references, which may have words between the phrase and the date
# ("will take place at the next session on June 15, 2026").
#
# `held` requires a "will be" / "to be" prefix: the bare "held on May 20"
# is the canonical past-tense marker for the actual date, not a forward
# reference.
DEFAULT_EXCLUDE_LOOSE = re.compile(
    r"(?ix)(?:"
    r"(?:will\s+)?take\s+place|"
    r"(?:will\s+be|to\s+be)\s+held|"
    r"scheduled\s+(?:for|on)|"
    r"(?:next|upcoming|future)\s+(?:regular\s+|special\s+|board\s+|committee\s+|work\s+session\s+)?meeting|"
    r"will\s+(?:re)?convene|"
    r"adjourn(?:ed|s)?\s+(?:to|until)"
    r")"
)

# Synthetic header emitted by adapters that wrap upstream JSON metadata as
# text. Authoritative: it came from the API, not from prose.
DEFAULT_HEADER_RE = re.compile(
    r"(?im)^\s*DATE:\s*(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?!\d)"
)


@dataclass(frozen=True)
class DateProfile:
    """Tuning for `parse_date_from_text`.

    earliest_year / future_year_slack
        Plausibility window. A match outside it is skipped, not returned.
    search_head_chars
        How far into the document to look. Formal documents state their
        date up top; scanning further only finds noise.
    exclude_anchored / exclude_loose
        Set either to None to disable that filter.
    allow_month_day_no_year / allow_month_year
        The two lossy last-resort passes. Disable when a wrong-but-plausible
        date is worse for you than no date at all.
    """

    name: str = "default"
    earliest_year: int = 2015
    future_year_slack: int = 1
    search_head_chars: int = 3_500
    header_search_chars: int = 500
    header_pattern: Optional[re.Pattern[str]] = DEFAULT_HEADER_RE
    exclude_anchored: Optional[re.Pattern[str]] = DEFAULT_EXCLUDE_ANCHORED
    exclude_loose: Optional[re.Pattern[str]] = DEFAULT_EXCLUDE_LOOSE
    context_lookback_chars: int = 80
    allow_month_day_no_year: bool = True
    allow_month_year: bool = True


DEFAULT_PROFILE = DateProfile(name="meeting")

# No context filtering: take the first plausible date in the head. Use for
# documents where a date is a date (invoices, letters, press releases).
PLAIN_PROFILE = DateProfile(
    name="plain",
    header_pattern=None,
    exclude_anchored=None,
    exclude_loose=None,
)


def _is_wanted_context(text: str, match_start: int, profile: DateProfile) -> bool:
    """False when the match is preceded by an excluded phrase."""
    if profile.exclude_anchored is None and profile.exclude_loose is None:
        return True
    lookback_start = max(0, match_start - profile.context_lookback_chars)
    preceding = text[lookback_start:match_start]
    # Cut to the start of the current clause so the previous sentence
    # cannot taint this date.
    last_break = -1
    for m in _SENTENCE_BOUNDARY.finditer(preceding):
        last_break = m.end()
    if last_break >= 0:
        preceding = preceding[last_break:]
    if profile.exclude_anchored is not None and profile.exclude_anchored.search(preceding):
        return False
    if profile.exclude_loose is not None and profile.exclude_loose.search(preceding):
        return False
    return True


def _is_plausible_year(year: int, profile: DateProfile) -> bool:
    return profile.earliest_year <= year <= date.today().year + profile.future_year_slack


def _normalize_year(year: int) -> int:
    # A two-digit year in a contemporary document always means 20YY.
    return 2000 + year if year < 100 else year


def _try_build(year: int, month: int, day: int, profile: DateProfile) -> Optional[date]:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if not _is_plausible_year(year, profile):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date_from_text(
    text: Optional[str],
    profile: DateProfile = DEFAULT_PROFILE,
) -> Optional[date]:
    """Return the first plausible document date found in the head, or None."""
    if not text:
        return None
    head = text[: profile.search_head_chars]

    if profile.header_pattern is not None:
        header_match = profile.header_pattern.search(head[: profile.header_search_chars])
        if header_match:
            d = _try_build(
                int(header_match.group("year")),
                int(header_match.group("month")),
                int(header_match.group("day")),
                profile,
            )
            if d is not None:
                return d

    for m in _MONTH_NAME_RE.finditer(head):
        if not _is_wanted_context(head, m.start(), profile):
            continue
        d = _try_build(
            int(m.group("year")),
            _MONTHS[m.group("month").lower()],
            int(m.group("day")),
            profile,
        )
        if d is not None:
            return d

    for m in _ISO_RE.finditer(head):
        if not _is_wanted_context(head, m.start(), profile):
            continue
        d = _try_build(
            int(m.group("year")), int(m.group("month")), int(m.group("day")), profile
        )
        if d is not None:
            return d

    for m in _SLASH_RE.finditer(head):
        if not _is_wanted_context(head, m.start(), profile):
            continue
        d = _try_build(
            _normalize_year(int(m.group("year"))),
            int(m.group("month")),
            int(m.group("day")),
            profile,
        )
        if d is not None:
            return d

    # Month + ordinal day, no inline year. Infer the year from the next 500
    # chars (the following sentence usually repeats it), else today.
    if profile.allow_month_day_no_year:
        today_year = date.today().year
        max_year = today_year + profile.future_year_slack
        for m in _MONTH_DAY_NO_YEAR_RE.finditer(head):
            if not _is_wanted_context(head, m.start(), profile):
                continue
            window = text[m.end() : m.end() + 500]
            inferred_year: Optional[int] = None
            for ym in _LOOSE_YEAR_RE.finditer(window):
                y = int(ym.group(1))
                if _is_plausible_year(y, profile) and y <= max_year:
                    inferred_year = y
                    break
            d = _try_build(
                inferred_year if inferred_year is not None else today_year,
                _MONTHS[m.group("month").lower()],
                int(m.group("day")),
                profile,
            )
            if d is not None:
                return d

    # Month + year, no day. Day 1 keeps it sortable and renders as a useful
    # month/year label.
    if profile.allow_month_year:
        for m in _MONTH_YEAR_RE.finditer(head):
            if not _is_wanted_context(head, m.start(), profile):
                continue
            d = _try_build(
                int(m.group("year")), _MONTHS[m.group("month").lower()], 1, profile
            )
            if d is not None:
                return d

    return None


# ── URL / filename dates ────────────────────────────────────────────────
# A separate, stricter pass: file names carry dates in a handful of shapes
# and none of the surrounding-context problems.

_URL_DATE_PATTERNS = (
    re.compile(r"(\d{4})[-_/](\d{1,2})[-_/](\d{1,2})"),
    re.compile(r"(\d{1,2})[-_/](\d{1,2})[-_/](\d{4})"),
    re.compile(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"[\s\-_]+(\d{1,2}),?[\s\-_]+(\d{4})",
        re.IGNORECASE,
    ),
)


def parse_date_from_url(
    url: str,
    profile: DateProfile = DEFAULT_PROFILE,
) -> Optional[date]:
    """Pull a date out of a URL or file name, or None.

    Handles "2026-03-16 REGULAR MEETING.pdf", "docs/03/16/2026/agenda.pdf",
    and "January-13-2026-School-Board.pdf".
    """
    for pattern in _URL_DATE_PATTERNS:
        for match in pattern.finditer(url):
            groups = match.groups()
            try:
                if groups[0].lower() in _MONTHS:
                    month = _MONTHS[groups[0].lower()]
                    candidate = _try_build(int(groups[2]), month, int(groups[1]), profile)
                elif len(groups[0]) == 4:
                    candidate = _try_build(
                        int(groups[0]), int(groups[1]), int(groups[2]), profile
                    )
                else:
                    candidate = _try_build(
                        int(groups[2]), int(groups[0]), int(groups[1]), profile
                    )
            except (ValueError, KeyError):
                continue
            if candidate is not None:
                return candidate
    return None


def parse_iso_prefix(value: Optional[str]) -> Optional[date]:
    """Parse the leading `YYYY-MM-DD` of an API timestamp, or None.

    For JSON feeds that return "2026-03-12T10:00:00" and friends. Does no
    plausibility filtering: the value came from a machine, not from prose.
    """
    if not value:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
