"""Tests for docpipe.extract.dates.

The hard part is not matching a date, it is rejecting the wrong one, so
most of these cover context filtering: fiscal-year ends, effective dates,
deadlines, and forward references to a future meeting.
"""
from datetime import date

from docpipe.extract.dates import (
    PLAIN_PROFILE,
    parse_date_from_text,
    parse_date_from_url,
    parse_iso_prefix,
)


# ── URLs and file names ─────────────────────────────────────────────────


def test_url_iso_date():
    assert parse_date_from_url("2026-03-16 REGULAR MEETING.pdf") == date(2026, 3, 16)


def test_url_slash_date():
    assert parse_date_from_url("docs/03/16/2026/agenda.pdf") == date(2026, 3, 16)


def test_url_month_name():
    assert parse_date_from_url("January-13-2026-School-Board.pdf") == date(2026, 1, 13)


def test_url_without_date_returns_none():
    assert parse_date_from_url("agenda.pdf") is None


def test_url_implausible_year_is_rejected():
    assert parse_date_from_url("1998-03-16-minutes.pdf") is None


# ── Document text ───────────────────────────────────────────────────────


def test_picks_real_date_over_fiscal_year_end():
    text = (
        "Slippery Rock Area School District\n"
        "Budgeted Revenues and Expenses\n"
        "Fiscal Year Ended June 30, 2026\n"
        "Adopted June 23, 2025\n"
    )
    assert parse_date_from_text(text) == date(2025, 6, 23)


def test_skips_year_ending_variant():
    text = (
        "Annual Audit Report\n"
        "For the Year Ending December 31, 2026\n"
        "Presented at the Board Meeting of May 14, 2025\n"
    )
    assert parse_date_from_text(text) == date(2025, 5, 14)


def test_skips_effective_and_deadline_dates():
    text = (
        "Vendor Agreement\n"
        "Effective July 1, 2026\n"
        "Due Date: August 15, 2026\n"
        "Board Meeting Minutes February 10, 2026\n"
    )
    assert parse_date_from_text(text) == date(2026, 2, 10)


def test_plain_date_still_works():
    assert parse_date_from_text("Regular Board Meeting Minutes\nMarch 18, 2026\n") == date(2026, 3, 18)


def test_iso_format_still_works():
    assert parse_date_from_text("Meeting on 2026-04-22 at 7pm.") == date(2026, 4, 22)


def test_no_date_returns_none():
    assert parse_date_from_text("Just some text with no date") is None


def test_handles_empty_input():
    assert parse_date_from_text("") is None
    assert parse_date_from_text(None) is None


def test_prefers_synthetic_header_over_forward_refs():
    """Adapters that wrap API metadata emit a DATE: header. It came from
    the upstream API, so it outranks anything the prose claims."""
    text = (
        "MEETING: Committee of the Whole\n"
        "DATE: 2026-05-20T00:00:00Z\n"
        "\n"
        "Will take place at the regular meeting on Wednesday, May 27, 2026\n"
        "Agenda Item Details Meeting May 20, 2026 - Committee of the Whole\n"
    )
    assert parse_date_from_text(text) == date(2026, 5, 20)


def test_skips_will_take_place_forward_reference():
    text = (
        "Agenda for the special meeting.\n"
        "This vote will take place at the next session on June 15, 2026.\n"
        "Minutes from May 8, 2026.\n"
    )
    assert parse_date_from_text(text) == date(2026, 5, 8)


def test_skips_to_be_held_forward_reference():
    text = (
        "The next regular meeting to be held on June 5, 2026.\n"
        "These minutes are from the meeting of April 17, 2026.\n"
    )
    assert parse_date_from_text(text) == date(2026, 4, 17)


def test_held_on_past_tense_is_not_filtered():
    """"held on" without a will/to-be prefix is the canonical past-tense
    marker for the document's own date."""
    text = (
        "The next meeting will be held on June 10, 2026.\n"
        "Minutes of meeting held on May 13, 2026.\n"
    )
    assert parse_date_from_text(text) == date(2026, 5, 13)


def test_skips_scheduled_for_forward_reference():
    text = "Special Meeting scheduled for May 27, 2026.\nHeld on May 20, 2026.\n"
    assert parse_date_from_text(text) == date(2026, 5, 20)


def test_plain_profile_takes_the_first_date_it_sees():
    """Without the context filters, the fiscal-year date wins. That is the
    right behavior for documents where a date is just a date."""
    text = "Fiscal Year Ended June 30, 2026\nAdopted June 23, 2025\n"
    assert parse_date_from_text(text, profile=PLAIN_PROFILE) == date(2026, 6, 30)


# ── API timestamps ──────────────────────────────────────────────────────


def test_iso_prefix_parses_api_timestamp():
    assert parse_iso_prefix("2026-03-12T10:00:00") == date(2026, 3, 12)


def test_iso_prefix_rejects_garbage():
    assert parse_iso_prefix("not a date") is None
    assert parse_iso_prefix(None) is None
    assert parse_iso_prefix("2026-13-45T00:00:00") is None
