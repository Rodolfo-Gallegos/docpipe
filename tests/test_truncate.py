"""Tests for docpipe.extract.truncate."""
from docpipe.extract.truncate import (
    GENERIC_PROFILE,
    keyword_profile,
    truncate_smart,
)

CAP = 800_000


def test_short_doc_is_noop():
    text = "Short agenda. Motion by Smith. Approved." * 50
    out, meta = truncate_smart(text, max_chars=CAP)
    assert out == text
    assert meta["method"] == "no_op"
    assert meta["final_chars"] == len(text)


def test_safe_tail_trim_runs_under_cap():
    """A small doc with a clean policy tail is trimmed even though we are
    nowhere near the cap. Pure token saving."""
    body = "Resolution: AUTHORIZE the contract with Acme Co. " * 1000  # ~50KB
    filler = "\n\nBOARD POLICY MANUAL\n\n" + ("Policy text. " * 5000)
    out, meta = truncate_smart(body + filler, max_chars=CAP)
    assert meta["method"] == "trimmed_safe_tail"
    # The cut lands at or just past the body (the marker follows two
    # newlines that belong to the filler section).
    assert len(body) <= meta["tail_dropped_at"] <= len(body) + 5
    assert "BOARD POLICY MANUAL" not in out
    assert "AUTHORIZE the contract with Acme Co." in out


def test_safe_marker_near_start_is_ignored():
    """A marker in the first 40% must not trigger a drop: too risky for a
    legitimate early reference to a policy section."""
    text = "POLICY MANUAL is referenced here. " * 20 + ("Body text. " * 50_000)
    out, _meta = truncate_smart(text, max_chars=CAP)
    assert out.count("Body text.") > 100


def test_aggressive_tail_only_when_over_cap():
    body = "Body content. " * 3500                                   # ~50K chars
    exhibit = "\n\nEXHIBIT A\n\n" + ("Exhibit content. " * 1800)      # ~30K chars
    out, meta = truncate_smart(body + exhibit, max_chars=CAP)
    assert meta["method"] == "no_op", "EXHIBIT A under cap must survive"
    assert "Exhibit content" in out

    big_body = "AUTHORIZE the contract. " * 30_000                    # ~720K chars
    big_exhibit = "\n\nEXHIBIT A\n\n" + ("Static text. " * 60_000)     # ~780K chars
    out2, meta2 = truncate_smart(big_body + big_exhibit, max_chars=CAP)
    assert meta2["method"] in {"trimmed_aggressive_tail", "trimmed_tail+windows"}
    assert len(out2) <= CAP


def test_content_at_end_survives_via_windows():
    """The pathological case: a 1.4M-char doc whose one real action sits at
    the very end. Step 2 windows must capture it."""
    head_filler = "Introductory remarks. " * 1000        # ~22K chars
    middle_policy = "Static policy text. " * 70_000      # ~1.4M chars, no markers
    tail_action = (
        "\n\nRESOLVED that the Board approves the employment contract "
        "with Dr. King for a starting salary in the amount of $380,000.00 "
        "with a term ending June 30, 2029."
    )
    out, meta = truncate_smart(head_filler + middle_policy + tail_action, max_chars=CAP)
    assert len(out) <= CAP
    assert "RESOLVED" in out
    assert "$380,000.00" in out
    assert "Dr. King" in out
    assert meta["method"] in {"trimmed_tail+windows", "head_windows_tail"}


def test_pathological_giant_doc_returns_under_cap():
    """5M chars with no recognizable filler or keep markers must still come
    back under the cap through the head+middle+tail fallback."""
    text = "Random sentence about miscellaneous activity. " * 100_000
    out, meta = truncate_smart(text, max_chars=CAP)
    assert len(out) <= CAP
    assert meta["method"] in {"head_windows_tail", "trimmed_tail+windows"}


def test_window_merge_keeps_order():
    chunk_a = "Filler. " * 500
    chunk_b = "AUTHORIZE the first contract. " + "Filler. " * 50 + "MOVED by Smith."
    chunk_c = "Filler. " * 500
    chunk_d = "RESOLVED that the second item is approved."
    text = (chunk_a + chunk_b + chunk_c + chunk_d) * 200  # force over cap
    out, meta = truncate_smart(text, max_chars=CAP)
    assert "AUTHORIZE the first contract" in out
    assert "RESOLVED that the second item" in out
    assert out.find("AUTHORIZE the first contract") < out.find("RESOLVED that the second")
    assert meta["method"] in {"trimmed_tail+windows", "head_windows_tail"}


# ── Profiles ────────────────────────────────────────────────────────────


def test_generic_profile_ignores_domain_vocabulary():
    """With no patterns, a policy tail is not filler and nothing is trimmed
    until the cap forces a head+tail cut."""
    text = "Body. " * 1000 + "\n\nBOARD POLICY MANUAL\n\n" + "Policy. " * 5000
    out, meta = truncate_smart(text, max_chars=CAP, profile=GENERIC_PROFILE)
    assert meta["method"] == "no_op"
    assert "BOARD POLICY MANUAL" in out


def test_keyword_profile_keeps_its_own_vocabulary():
    profile = keyword_profile(
        keep_keywords=["adverse event"],
        tail_keywords=["Bibliography"],
        window_chars=200,
    )
    filler = "Neutral prose. " * 60_000                     # ~900K chars
    signal = "\n\nA serious adverse event was recorded in cohort B.\n\n"
    tail = "\n\nBibliography\n\n" + ("Citation. " * 2000)
    out, meta = truncate_smart(
        filler + signal + filler + tail, max_chars=100_000, profile=profile
    )
    assert meta["profile"] == "keywords"
    assert "adverse event" in out
    assert "Citation." not in out
    assert len(out) <= 100_000
