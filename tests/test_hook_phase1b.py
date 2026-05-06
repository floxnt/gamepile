"""Test fixtures for app.hook_metrics Phase 1b categorical signal.

Run with: uv run python tests/test_hook_phase1b.py

No pytest dependency — pure assertions, same pattern as other tests/.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.hook_metrics import (
    BADGE_AVERAGE,
    BADGE_FILTERS_HARD,
    BADGE_INSUFFICIENT_DATA,
    BADGE_STICKY,
    SIGNAL_FILTERS_HARD,
    SIGNAL_NEUTRAL,
    SIGNAL_NO_DATA,
    SIGNAL_STICKY,
    categorize_cliff,
    categorize_completion,
    categorize_stickiness,
    compute_stickiness_signal,
)


# ---------------------------------------------------------------------------
# Stub game builder — only the fields compute_stickiness_signal touches.
# resolve_type short-circuits when game_type is set, so the genre/tag list
# helpers only need to exist for the rare null-game_type fallback path.
# ---------------------------------------------------------------------------

def _game(**kw):
    defaults = {
        "appid": 999,
        "name": "Stub",
        "game_type": "linear",
        "completion_rate": None,
        "completion_rate_confidence": None,
        "cliff_metric": None,
        "stickiness_ratio": None,
        "hltb_main_hours": None,
        "hltb_completionist_hours": None,
        "playtime_minutes": 0,
        "app_type": None,
        "genres": "",
        "tags": "",
        "user_tags": "",
    }
    defaults.update(kw)
    g = SimpleNamespace(**defaults)
    g.genre_list = lambda: [x.strip() for x in g.genres.split(",") if x.strip()]
    g.tag_list = lambda: [x.strip() for x in g.tags.split(",") if x.strip()]
    g.user_tags_list = lambda: [x.strip() for x in g.user_tags.split(",") if x.strip()]
    return g


# ---------------------------------------------------------------------------
# categorize_cliff
# ---------------------------------------------------------------------------

def test_cliff_none_returns_no_data():
    assert categorize_cliff(None) == SIGNAL_NO_DATA


def test_cliff_below_threshold_returns_neutral():
    assert categorize_cliff(0.0) == SIGNAL_NEUTRAL
    assert categorize_cliff(7.1) == SIGNAL_NEUTRAL
    assert categorize_cliff(19.99) == SIGNAL_NEUTRAL


def test_cliff_at_or_above_threshold_returns_filters_hard():
    assert categorize_cliff(20.0) == SIGNAL_FILTERS_HARD
    assert categorize_cliff(35.2) == SIGNAL_FILTERS_HARD


def test_cliff_never_returns_sticky():
    # Even a 0pp drop is not a positive signal on its own.
    assert categorize_cliff(0.0) != SIGNAL_STICKY


# ---------------------------------------------------------------------------
# categorize_stickiness
# ---------------------------------------------------------------------------

def test_stickiness_none_returns_no_data():
    assert categorize_stickiness(None) == SIGNAL_NO_DATA


def test_stickiness_at_or_below_filters_threshold():
    assert categorize_stickiness(0.0) == SIGNAL_FILTERS_HARD
    assert categorize_stickiness(0.50) == SIGNAL_FILTERS_HARD


def test_stickiness_in_between_returns_neutral():
    assert categorize_stickiness(0.51) == SIGNAL_NEUTRAL
    assert categorize_stickiness(0.81) == SIGNAL_NEUTRAL
    assert categorize_stickiness(0.89) == SIGNAL_NEUTRAL


def test_stickiness_at_or_above_sticky_threshold():
    assert categorize_stickiness(0.90) == SIGNAL_STICKY
    assert categorize_stickiness(1.0) == SIGNAL_STICKY


# ---------------------------------------------------------------------------
# categorize_completion
# ---------------------------------------------------------------------------

def test_completion_none_returns_no_data():
    assert categorize_completion(None, "high") == SIGNAL_NO_DATA
    assert categorize_completion(None, None) == SIGNAL_NO_DATA


def test_completion_low_confidence_collapses_to_no_data():
    # Even a value that would qualify as sticky returns no_data when
    # the heuristic fired off a fallback or weak-pattern match.
    assert categorize_completion(0.50, "low") == SIGNAL_NO_DATA
    assert categorize_completion(0.01, "low") == SIGNAL_NO_DATA
    assert categorize_completion(0.50, None) == SIGNAL_NO_DATA


def test_completion_high_confidence_at_or_above_sticky():
    assert categorize_completion(0.15, "high") == SIGNAL_STICKY
    assert categorize_completion(0.50, "high") == SIGNAL_STICKY


def test_completion_high_confidence_at_or_below_filters():
    assert categorize_completion(0.0, "high") == SIGNAL_FILTERS_HARD
    assert categorize_completion(0.03, "high") == SIGNAL_FILTERS_HARD


def test_completion_high_confidence_in_between_neutral():
    assert categorize_completion(0.04, "high") == SIGNAL_NEUTRAL
    assert categorize_completion(0.07, "high") == SIGNAL_NEUTRAL
    assert categorize_completion(0.14, "high") == SIGNAL_NEUTRAL


# ---------------------------------------------------------------------------
# compute_stickiness_signal — three-signal evaluation (linear / mixed)
# ---------------------------------------------------------------------------

def test_signal_linear_two_sticky_one_neutral_returns_sticky():
    # cliff=neutral, stickiness=sticky, completion=sticky → BADGE_STICKY
    g = _game(
        game_type="linear",
        cliff_metric=5.0,
        stickiness_ratio=0.95,
        completion_rate=0.20,
        completion_rate_confidence="high",
    )
    badge, sticky_count, filters_count = compute_stickiness_signal(g)
    assert badge == BADGE_STICKY
    assert sticky_count == 2
    assert filters_count == 0


def test_signal_linear_two_filters_one_neutral_returns_filters():
    g = _game(
        game_type="linear",
        cliff_metric=25.0,
        stickiness_ratio=0.40,
        completion_rate=0.07,
        completion_rate_confidence="high",
    )
    badge, sticky_count, filters_count = compute_stickiness_signal(g)
    assert badge == BADGE_FILTERS_HARD
    assert sticky_count == 0
    assert filters_count == 2


def test_signal_linear_one_each_with_neutral_returns_average():
    g = _game(
        game_type="linear",
        cliff_metric=25.0,
        stickiness_ratio=0.95,
        completion_rate=0.07,
        completion_rate_confidence="high",
    )
    badge, sticky_count, filters_count = compute_stickiness_signal(g)
    assert badge == BADGE_AVERAGE
    assert sticky_count == 1
    assert filters_count == 1


def test_signal_linear_all_neutral_returns_average():
    g = _game(
        game_type="linear",
        cliff_metric=10.0,
        stickiness_ratio=0.75,
        completion_rate=0.07,
        completion_rate_confidence="high",
    )
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_AVERAGE


def test_signal_linear_two_no_data_returns_insufficient():
    # cliff present (neutral), stickiness no_data, completion no_data
    g = _game(
        game_type="linear",
        cliff_metric=5.0,
        stickiness_ratio=None,
        completion_rate=None,
    )
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_INSUFFICIENT_DATA


def test_signal_linear_three_no_data_returns_insufficient():
    g = _game(
        game_type="linear",
        cliff_metric=None,
        stickiness_ratio=None,
        completion_rate=None,
    )
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_INSUFFICIENT_DATA


def test_signal_linear_low_confidence_completion_treated_as_no_data():
    # low-confidence completion shouldn't contribute toward sticky vote.
    # cliff=neutral, stickiness=sticky, completion=no_data → 1 sticky out
    # of 3, which is below the 2-of-3 threshold → BADGE_AVERAGE.
    g = _game(
        game_type="linear",
        cliff_metric=5.0,
        stickiness_ratio=0.95,
        completion_rate=0.20,
        completion_rate_confidence="low",
    )
    badge, sticky_count, filters_count = compute_stickiness_signal(g)
    assert badge == BADGE_AVERAGE
    assert sticky_count == 1
    assert filters_count == 0


def test_signal_mixed_evaluates_all_three():
    # Same logic as linear; just confirm rule routing.
    g = _game(
        game_type="mixed",
        cliff_metric=5.0,
        stickiness_ratio=0.95,
        completion_rate=0.20,
        completion_rate_confidence="high",
    )
    badge, sticky_count, _ = compute_stickiness_signal(g)
    assert badge == BADGE_STICKY
    assert sticky_count == 2


def test_signal_expansion_evaluates_all_three():
    g = _game(
        game_type="expansion",
        cliff_metric=25.0,
        stickiness_ratio=0.40,
        completion_rate=0.0,
        completion_rate_confidence="high",
    )
    badge, sticky_count, filters_count = compute_stickiness_signal(g)
    assert badge == BADGE_FILTERS_HARD
    assert sticky_count == 0
    assert filters_count == 3


# ---------------------------------------------------------------------------
# compute_stickiness_signal — single-signal path (multiplayer/mmo/etc.)
# ---------------------------------------------------------------------------

def test_signal_multiplayer_stickiness_only_sticky():
    # Cliff/completion suppressed; stickiness alone drives the verdict.
    # Pass non-null cliff/completion to confirm they're ignored.
    g = _game(
        game_type="multiplayer",
        cliff_metric=25.0,
        stickiness_ratio=0.95,
        completion_rate=0.0,
        completion_rate_confidence="high",
    )
    badge, sticky_count, filters_count = compute_stickiness_signal(g)
    assert badge == BADGE_STICKY
    assert sticky_count == 1
    assert filters_count == 0


def test_signal_mmo_stickiness_filters():
    g = _game(game_type="mmo", stickiness_ratio=0.30)
    badge, _, filters_count = compute_stickiness_signal(g)
    assert badge == BADGE_FILTERS_HARD
    assert filters_count == 1


def test_signal_no_endpoint_neutral_stickiness_returns_average():
    g = _game(game_type="no_endpoint", stickiness_ratio=0.75)
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_AVERAGE


def test_signal_sandbox_null_stickiness_returns_insufficient():
    g = _game(game_type="sandbox", stickiness_ratio=None)
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_INSUFFICIENT_DATA


# ---------------------------------------------------------------------------
# compute_stickiness_signal — ineligible types always insufficient
# ---------------------------------------------------------------------------

def test_signal_beta_playtest_always_insufficient():
    # Even with three perfect sticky inputs, beta_playtest's rules say
    # categorical_badge_eligible=False.
    g = _game(
        game_type="beta_playtest",
        cliff_metric=0.0,
        stickiness_ratio=1.0,
        completion_rate=0.50,
        completion_rate_confidence="high",
    )
    badge, sticky_count, filters_count = compute_stickiness_signal(g)
    assert badge == BADGE_INSUFFICIENT_DATA
    assert sticky_count == 0
    assert filters_count == 0


def test_signal_early_access_always_insufficient():
    g = _game(
        game_type="early_access",
        stickiness_ratio=0.95,
    )
    assert compute_stickiness_signal(g)[0] == BADGE_INSUFFICIENT_DATA


def test_signal_unknown_always_insufficient():
    g = _game(
        game_type="unknown",
        stickiness_ratio=0.95,
    )
    assert compute_stickiness_signal(g)[0] == BADGE_INSUFFICIENT_DATA


def test_signal_software_always_insufficient():
    # Section is hidden upstream; the function still returns a sensible
    # value rather than crashing on the no-metrics-evaluated path.
    g = _game(game_type="software")
    assert compute_stickiness_signal(g)[0] == BADGE_INSUFFICIENT_DATA


# ---------------------------------------------------------------------------
# compute_stickiness_signal — edge cases
# ---------------------------------------------------------------------------

def test_signal_one_sticky_one_filters_one_neutral_returns_average():
    # Spec: "one each of sticky/filters_hard with the rest neutral" → Average.
    g = _game(
        game_type="linear",
        cliff_metric=25.0,           # filters
        stickiness_ratio=0.95,       # sticky
        completion_rate=0.07,        # neutral
        completion_rate_confidence="high",
    )
    badge, sticky_count, filters_count = compute_stickiness_signal(g)
    assert badge == BADGE_AVERAGE
    assert sticky_count == 1
    assert filters_count == 1


def test_signal_two_sticky_one_filters_returns_average():
    # Sticky majority but a filters_hard contribution disqualifies sticky.
    # Falls to Average.
    g = _game(
        game_type="linear",
        cliff_metric=25.0,           # filters
        stickiness_ratio=0.95,       # sticky
        completion_rate=0.20,        # sticky
        completion_rate_confidence="high",
    )
    badge, sticky_count, filters_count = compute_stickiness_signal(g)
    assert badge == BADGE_AVERAGE
    assert sticky_count == 2
    assert filters_count == 1


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    test_funcs = [
        v for k, v in globals().items()
        if k.startswith("test_") and callable(v)
    ]
    print(f"Running {len(test_funcs)} test(s)…")
    failures = []
    for fn in test_funcs:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as exc:
            failures.append((fn.__name__, exc))
            print(f"  ✗ {fn.__name__}: {exc}")
    if failures:
        print(f"\n{len(failures)} failure(s):")
        for name, exc in failures:
            print(f"  {name}: {exc}")
        sys.exit(1)
    print(f"\nAll {len(test_funcs)} tests passed.")


if __name__ == "__main__":
    main()
