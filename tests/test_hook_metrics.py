"""Test fixtures for app.hook_metrics.

Run with: uv run python tests/test_hook_metrics.py

No pytest dependency — pure assertions, same pattern as other tests/.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.hook_metrics import (
    MIN_REVIEWS_FOR_STATS,
    STICKY_PLAYTIME_THRESHOLD_MIN,
    compute_cliff_metric,
    compute_completion_rate,
    compute_completion_rate_confidence,
    compute_playtime_median_avg_ratio,
    compute_review_playtime_median,
    compute_stickiness_ratio,
    find_story_completion_achievement,
    qualitative_ratio_hint,
)


def _ach(name, percent):
    return {"name": name, "percent": percent}


# ---------------------------------------------------------------------------
# find_story_completion_achievement + compute_completion_rate
# ---------------------------------------------------------------------------

def test_find_story_completion_basic_patterns():
    cases = [
        ("Game Complete", 12.3),
        ("Roll Credits", 8.4),
        ("The End", 5.1),
        ("Final Boss", 9.0),
        ("Epilogue", 6.2),
    ]
    for name, pct in cases:
        match = find_story_completion_achievement([_ach(name, pct), _ach("Other", 90)])
        assert match is not None, f"expected match for {name!r}"
        assert match["name"] == name


def test_find_story_completion_lowest_among_matches():
    """When multiple achievements match, pick the lowest unlock %."""
    aches = [
        _ach("Story Complete", 8.4),    # match
        _ach("Final Boss Defeated", 5.1),  # match (lower)
        _ach("First Level", 90.0),      # no match
    ]
    match = find_story_completion_achievement(aches)
    assert match["name"] == "Final Boss Defeated"


def test_find_story_completion_case_insensitive():
    match = find_story_completion_achievement([_ach("ROLL CREDITS", 5.0)])
    assert match is not None
    match = find_story_completion_achievement([_ach("the eND", 5.0)])
    assert match is not None


def test_find_story_completion_no_match():
    aches = [_ach("First Steps", 90), _ach("Found a Sword", 80)]
    assert find_story_completion_achievement(aches) is None


def test_find_story_completion_empty():
    assert find_story_completion_achievement([]) is None
    assert find_story_completion_achievement(None) is None  # type: ignore


def test_find_story_completion_prefers_display_name():
    """When schema-resolved displayName is present, the heuristic matches
    against THAT (not the opaque internal name)."""
    aches = [
        {"name": "ACH00", "displayName": "Roll Credits", "percent": 5.0},
        {"name": "ACH01", "displayName": "First Boss", "percent": 90.0},
    ]
    match = find_story_completion_achievement(aches)
    assert match is not None
    assert match["name"] == "ACH00"  # matched via "credits" in displayName


def test_find_story_completion_falls_back_to_name_when_no_display():
    """When displayName is absent (schema fetch failed), match against name."""
    aches = [
        {"name": "Game Complete", "percent": 5.0},
        {"name": "First Steps", "percent": 90.0},
    ]
    match = find_story_completion_achievement(aches)
    assert match is not None
    assert match["name"] == "Game Complete"


def test_compute_completion_rate_uses_heuristic():
    aches = [_ach("First Steps", 90), _ach("Game Complete", 12)]
    assert compute_completion_rate(aches) == 0.12


def test_compute_completion_rate_fallback_to_lowest():
    aches = [_ach("First Steps", 90), _ach("Hidden Cave", 4.2), _ach("Boss Down", 15)]
    # No pattern match — falls back to lowest %.
    assert compute_completion_rate(aches) == 0.042


def test_compute_completion_rate_empty():
    assert compute_completion_rate([]) is None


# ---------------------------------------------------------------------------
# compute_completion_rate_confidence
# ---------------------------------------------------------------------------

def test_confidence_high_strong_pattern_in_display():
    """Strong pattern in displayName + percent <= 50 → high confidence."""
    aches = [
        {"name": "ACH00", "displayName": "Roll Credits", "percent": 12.0},
        {"name": "ACH01", "displayName": "First Steps", "percent": 90.0},
    ]
    assert compute_completion_rate_confidence(aches) == "high"


def test_confidence_high_via_endings_plural():
    aches = [{"name": "X", "displayName": "All Endings Seen", "percent": 8.0}]
    assert compute_completion_rate_confidence(aches) == "high"


def test_confidence_high_via_the_end():
    aches = [{"name": "X", "displayName": "The End of Fire", "percent": 21.0}]
    assert compute_completion_rate_confidence(aches) == "high"


def test_confidence_low_when_above_pct_cap():
    """Strong pattern but percent > 50 → low (it's a launch achievement)."""
    aches = [{"name": "X", "displayName": "Roll Credits", "percent": 75.0}]
    # Note: still gets matched/picked, but confidence downgrades.
    assert compute_completion_rate_confidence(aches) == "low"


def test_confidence_low_weak_pattern_only():
    """Weak pattern ('complete' / 'final') in displayName → low."""
    aches = [{"name": "X", "displayName": "Complete Set", "percent": 6.7}]
    assert compute_completion_rate_confidence(aches) == "low"
    aches = [{"name": "X", "displayName": "Final Transmission", "percent": 5.5}]
    assert compute_completion_rate_confidence(aches) == "low"


def test_confidence_low_when_internal_name_matched_but_display_weak():
    """Hades case: internal name 'AchReachedEpilogue' matches via 'epilogue',
    but displayName 'One for the Ages' has no strong pattern → low confidence
    is the honest call. We caught the achievement, but the user-visible
    text doesn't convey the signal."""
    aches = [
        {"name": "AchReachedEpilogue", "displayName": "One for the Ages", "percent": 8.4},
        {"name": "AchOther", "displayName": "Some Other Thing", "percent": 90.0},
    ]
    assert compute_completion_rate_confidence(aches) == "low"


def test_confidence_low_on_fallback():
    """No pattern match at all → fallback → confidence is low regardless."""
    aches = [{"name": "X", "displayName": "Master Marksman", "percent": 1.8}]
    assert compute_completion_rate_confidence(aches) == "low"


def test_confidence_none_on_empty():
    assert compute_completion_rate_confidence([]) is None
    assert compute_completion_rate_confidence(None) is None  # type: ignore


# ---------------------------------------------------------------------------
# compute_cliff_metric
# ---------------------------------------------------------------------------

def test_cliff_discard_top3_when_ge_10():
    # 10 achievements: discard top 3, find largest gap in remaining 7.
    aches = [
        _ach("a", 95),   # discarded
        _ach("b", 92),   # discarded
        _ach("c", 88),   # discarded
        _ach("d", 80),
        _ach("e", 78),   # gap of 2 from d
        _ach("f", 30),   # gap of 48 from e — winner
        _ach("g", 28),
        _ach("h", 25),
        _ach("i", 20),
        _ach("j", 5),    # gap of 15 from i
    ]
    result = compute_cliff_metric(aches)
    assert result is not None
    assert abs(result - 48.0) < 0.001


def test_cliff_discard_top2_when_5_to_9():
    aches = [_ach(f"a{i}", p) for i, p in enumerate([90, 85, 80, 50, 48])]
    # 5 entries → discard top 2 → remaining: 80, 50, 48 (3 entries < 4 min) → None
    assert compute_cliff_metric(aches) is None
    # 7 entries → discard top 2 → 5 remain → cliff = 80 - 50 = 30
    aches = [_ach(f"a{i}", p) for i, p in enumerate([95, 90, 80, 50, 48, 46, 44])]
    result = compute_cliff_metric(aches)
    assert result is not None
    assert abs(result - 30.0) < 0.001


def test_cliff_no_discard_when_lt_5():
    aches = [_ach(f"a{i}", p) for i, p in enumerate([90, 50, 48, 45])]
    # 4 entries → no discard → all 4 used → largest gap = 90-50 = 40
    result = compute_cliff_metric(aches)
    assert result is not None
    assert abs(result - 40.0) < 0.001


def test_cliff_too_few_post_discard():
    # 3 entries < 4 minimum → None
    assert compute_cliff_metric([_ach("a", 90), _ach("b", 50), _ach("c", 10)]) is None
    # 11 entries: discard top 3 → 8 remain (>= 4) → result is some number
    aches = [_ach(f"a{i}", 100 - i * 5) for i in range(11)]
    assert compute_cliff_metric(aches) is not None


def test_cliff_empty():
    assert compute_cliff_metric([]) is None


# ---------------------------------------------------------------------------
# Review-derived metrics
# ---------------------------------------------------------------------------

def test_review_playtime_median_basic():
    pts = [60, 120, 180, 240, 300, 360, 420, 480, 540, 600]
    assert compute_review_playtime_median(pts) == 330  # median of 10 entries


def test_review_playtime_median_too_few():
    pts = [60] * (MIN_REVIEWS_FOR_STATS - 1)
    assert compute_review_playtime_median(pts) is None


def test_review_playtime_median_empty():
    assert compute_review_playtime_median([]) is None


def test_stickiness_ratio_basic():
    # 5 of 10 reviews >= 1200 minutes (20h)
    pts = [100, 200, 300, 400, 500] + [STICKY_PLAYTIME_THRESHOLD_MIN] * 5
    assert compute_stickiness_ratio(pts) == 0.5


def test_stickiness_ratio_threshold_inclusive():
    """A reviewer at exactly 1200 minutes counts as sticky."""
    pts = [STICKY_PLAYTIME_THRESHOLD_MIN] * MIN_REVIEWS_FOR_STATS
    assert compute_stickiness_ratio(pts) == 1.0


def test_stickiness_ratio_too_few():
    pts = [STICKY_PLAYTIME_THRESHOLD_MIN] * (MIN_REVIEWS_FOR_STATS - 1)
    assert compute_stickiness_ratio(pts) is None


# ---------------------------------------------------------------------------
# SteamSpy ratio
# ---------------------------------------------------------------------------

def test_playtime_median_avg_ratio_basic():
    assert compute_playtime_median_avg_ratio(300, 600) == 0.5
    assert compute_playtime_median_avg_ratio(800, 1000) == 0.8


def test_playtime_median_avg_ratio_missing():
    assert compute_playtime_median_avg_ratio(None, 600) is None
    assert compute_playtime_median_avg_ratio(300, None) is None
    assert compute_playtime_median_avg_ratio(None, None) is None


def test_playtime_median_avg_ratio_zero():
    assert compute_playtime_median_avg_ratio(0, 600) is None
    assert compute_playtime_median_avg_ratio(300, 0) is None


# ---------------------------------------------------------------------------
# Display helper
# ---------------------------------------------------------------------------

def test_qualitative_ratio_hint():
    assert qualitative_ratio_hint(None) == ""
    assert qualitative_ratio_hint(0.1) == "long-tail pattern"
    assert qualitative_ratio_hint(0.39) == "long-tail pattern"
    assert qualitative_ratio_hint(0.4) == "uneven engagement"
    assert qualitative_ratio_hint(0.65) == "uneven engagement"
    assert qualitative_ratio_hint(0.7) == "even engagement"
    assert qualitative_ratio_hint(0.95) == "even engagement"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_find_story_completion_basic_patterns,
    test_find_story_completion_lowest_among_matches,
    test_find_story_completion_case_insensitive,
    test_find_story_completion_no_match,
    test_find_story_completion_empty,
    test_find_story_completion_prefers_display_name,
    test_find_story_completion_falls_back_to_name_when_no_display,
    test_compute_completion_rate_uses_heuristic,
    test_compute_completion_rate_fallback_to_lowest,
    test_compute_completion_rate_empty,
    test_confidence_high_strong_pattern_in_display,
    test_confidence_high_via_endings_plural,
    test_confidence_high_via_the_end,
    test_confidence_low_when_above_pct_cap,
    test_confidence_low_weak_pattern_only,
    test_confidence_low_when_internal_name_matched_but_display_weak,
    test_confidence_low_on_fallback,
    test_confidence_none_on_empty,
    test_cliff_discard_top3_when_ge_10,
    test_cliff_discard_top2_when_5_to_9,
    test_cliff_no_discard_when_lt_5,
    test_cliff_too_few_post_discard,
    test_cliff_empty,
    test_review_playtime_median_basic,
    test_review_playtime_median_too_few,
    test_review_playtime_median_empty,
    test_stickiness_ratio_basic,
    test_stickiness_ratio_threshold_inclusive,
    test_stickiness_ratio_too_few,
    test_playtime_median_avg_ratio_basic,
    test_playtime_median_avg_ratio_missing,
    test_playtime_median_avg_ratio_zero,
    test_qualitative_ratio_hint,
]


def main() -> int:
    failures = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  ✗ {fn.__name__}: {exc!r}")
        except Exception as exc:
            failures += 1
            print(f"  ✗ {fn.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\n{failures}/{len(TESTS)} failed")
        return 1
    print(f"\nall {len(TESTS)} tests pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
