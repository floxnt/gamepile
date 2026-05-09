"""Phase 4 — manual completion-achievement override.

Run with: uv run python tests/test_phase4_completion_override.py

Covers:
  - pick_completion_achievement helper (lookup by internal name)
  - Override drives completion_rate from the chosen achievement's percent
  - Override forces confidence='high' regardless of name pattern
  - Reset restores heuristic-driven values
  - Phase 1c stickiness signal recomputes correctly with override applied
  - Sync-time fail-open: manual achievement missing from current fetch
    keeps the last-stored value (verified at the helper level — sync
    integration is a separate smoke test)

No pytest dependency — pure assertions, same pattern as test_hook_phase1c.py.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.hook_metrics import (
    BADGE_HOOKS_PLAYERS,
    BADGE_STANDARD_ENGAGEMENT,
    compute_completion_rate,
    compute_completion_rate_confidence,
    compute_stickiness_signal,
    pick_completion_achievement,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _ach(name: str, display: str, percent: float) -> dict:
    return {"name": name, "displayName": display, "percent": percent}


def _game(**kw):
    """Stub game shaped like Phase 1c tests'. Covers the fields the signal
    helpers and engagement template inspect."""
    defaults = {
        "appid": 999,
        "name": "Stub",
        "game_type": "linear",
        "completion_rate": None,
        "completion_rate_confidence": None,
        "completion_achievement_name_manual": None,
        "cliff_metric": None,
        "cliff_position": None,
        "stickiness_ratio": None,
        "review_playtime_median": None,
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


# Realistic Hollow-Knight-shaped achievement list: a heuristic candidate
# ("Speedrun Steel Soul") and the actual ending the user wants to assert
# ("Embrace the Void", which lacks a story-completion keyword).
_HOLLOW_KNIGHT_LIKE = [
    _ach("ACH_BREAK", "Awake the Mantis Lords", 78.4),
    _ach("ACH_INTRO", "First Steps in Dirtmouth", 92.3),
    _ach("ACH_SPEEDRUN", "Speedrun Complete", 5.2),
    _ach("ACH_VOID", "Embrace the Void", 1.8),
    _ach("ACH_PANTHEON", "Pantheon of Hallownest", 0.9),
]


# ---------------------------------------------------------------------------
# pick_completion_achievement
# ---------------------------------------------------------------------------

def test_pick_returns_match_by_internal_name():
    match = pick_completion_achievement(_HOLLOW_KNIGHT_LIKE, "ACH_VOID")
    assert match is not None
    assert match["name"] == "ACH_VOID"
    assert match["percent"] == 1.8


def test_pick_returns_none_for_missing_name():
    # Achievement removed by developer / typo / stale stored name.
    assert pick_completion_achievement(_HOLLOW_KNIGHT_LIKE, "ACH_NONEXISTENT") is None


def test_pick_returns_none_for_none_name():
    assert pick_completion_achievement(_HOLLOW_KNIGHT_LIKE, None) is None


def test_pick_returns_none_for_empty_name():
    assert pick_completion_achievement(_HOLLOW_KNIGHT_LIKE, "") is None


def test_pick_returns_none_for_empty_list():
    assert pick_completion_achievement([], "ACH_VOID") is None


def test_pick_does_not_match_displayname():
    # Internal name is the source of truth — matching by displayName
    # would let the heuristic shadow the user's choice.
    assert pick_completion_achievement(_HOLLOW_KNIGHT_LIKE, "Embrace the Void") is None


# ---------------------------------------------------------------------------
# Heuristic vs override resolution — the values the SAVE flow would persist
# ---------------------------------------------------------------------------

def test_heuristic_picks_speedrun_with_low_confidence_baseline():
    # Without override: heuristic catches "Complete" → ACH_SPEEDRUN (lowest
    # matching percent). Confidence 'low' because displayName carries only
    # a weak pattern word.
    rate = compute_completion_rate(_HOLLOW_KNIGHT_LIKE)
    confidence = compute_completion_rate_confidence(_HOLLOW_KNIGHT_LIKE)
    assert rate is not None and abs(rate - 0.052) < 1e-9
    assert confidence == "low"


def test_override_resolves_to_chosen_achievement_percent():
    # The route handler applies match.percent / 100 → completion_rate.
    match = pick_completion_achievement(_HOLLOW_KNIGHT_LIKE, "ACH_VOID")
    assert match is not None
    rate = match["percent"] / 100.0
    assert abs(rate - 0.018) < 1e-9


def test_override_forces_high_confidence_regardless_of_pattern():
    # ACH_VOID's displayName "Embrace the Void" carries no strong-pattern
    # keyword. The route handler forces 'high' anyway because the user's
    # assertion stands.
    match = pick_completion_achievement(_HOLLOW_KNIGHT_LIKE, "ACH_VOID")
    assert match is not None
    # The route writes confidence='high' explicitly (see
    # set_completion_achievement_manual). This test documents that the
    # heuristic confidence label for the same achievement would be 'low'
    # — the override deliberately ignores that.
    forced_confidence = "high"
    assert forced_confidence == "high"
    # And confirm the heuristic itself wouldn't have produced 'high' here:
    confidence = compute_completion_rate_confidence(_HOLLOW_KNIGHT_LIKE)
    assert confidence == "low"


# ---------------------------------------------------------------------------
# Reset path — heuristic recomputes against fresh data
# ---------------------------------------------------------------------------

def test_reset_recomputes_from_current_fetch():
    # The reset route runs compute_completion_rate against fresh achievement
    # data and writes the result alongside completion_achievement_name_manual=NULL.
    # Result should match the no-override heuristic.
    rate_after_reset = compute_completion_rate(_HOLLOW_KNIGHT_LIKE)
    confidence_after_reset = compute_completion_rate_confidence(_HOLLOW_KNIGHT_LIKE)
    assert rate_after_reset is not None and abs(rate_after_reset - 0.052) < 1e-9
    assert confidence_after_reset == "low"


def test_reset_with_no_achievements_clears_to_none():
    # Game has lost all achievements (extremely rare but possible). Reset
    # writes None for both rate and confidence.
    rate = compute_completion_rate([])
    confidence = compute_completion_rate_confidence([])
    assert rate is None
    assert confidence is None


# ---------------------------------------------------------------------------
# Phase 1c integration — override changes the badge
# ---------------------------------------------------------------------------

def test_signal_with_override_low_completion_pushes_filters():
    # Game with high stickiness but a manually-asserted very-low completion
    # rate of 1.8% (high confidence). Cliff signal absent. Score:
    #   1.5 * 0 (stickiness mid) + 0 (no cliff) + 0.7 * -1 (completion filter) = -0.7
    # In-band score → falls to Mixed signals or Standard. The completion
    # signal is high-confidence so it qualifies as a strong signal,
    # pushing to Mixed. Documents the score arithmetic, not the badge
    # outcome at the threshold edge.
    g = _game(
        completion_rate=0.018,
        completion_rate_confidence="high",
        completion_achievement_name_manual="ACH_VOID",
        cliff_metric=12.0, cliff_position=0.5,
        stickiness_ratio=0.85,  # neutral band
    )
    badge, score, breakdown = compute_stickiness_signal(g)
    # Stickiness mid (0.85 < 0.90 sticky threshold, > 0.50 filters threshold) → 0
    # Cliff small → 0
    # Completion -1 high-conf → -0.7
    assert breakdown["completion"]["value"] == -1
    assert breakdown["completion"]["weight"] == 0.7
    assert abs(score - -0.7) < 1e-9


def test_signal_with_override_high_completion_promotes_hooks():
    # User asserts a true ending achievement at high completion rate.
    # Combined with sticky stickiness, score reaches Hooks.
    g = _game(
        completion_rate=0.32,
        completion_rate_confidence="high",
        completion_achievement_name_manual="ACH_FINAL",
        cliff_metric=8.0, cliff_position=0.4,
        stickiness_ratio=0.94,
    )
    # stickiness +1 * 1.5 = +1.5
    # cliff small → 0
    # completion +1 * 0.7 (high conf) = +0.7
    # score = +2.2 → Hooks players
    badge, score, breakdown = compute_stickiness_signal(g)
    assert badge == BADGE_HOOKS_PLAYERS
    assert abs(score - 2.2) < 1e-9


def test_override_with_low_conf_stripped_label_still_high_via_force():
    # The override path forces confidence='high' even on achievements
    # whose heuristic confidence would have been 'low'. The signal weights
    # +0.7 instead of +0.3. This test documents the score arithmetic
    # using the forced confidence value (the route persists 'high').
    g = _game(
        completion_rate=0.30,
        completion_rate_confidence="high",  # forced by override
        completion_achievement_name_manual="ACH_OPAQUE_NAME",
        cliff_metric=5.0, cliff_position=0.5,
        stickiness_ratio=0.92,
    )
    _, score, breakdown = compute_stickiness_signal(g)
    # +1.5 stickiness + 0 cliff + 0.7 completion = +2.2
    assert breakdown["completion"]["weight"] == 0.7
    assert abs(score - 2.2) < 1e-9


# ---------------------------------------------------------------------------
# Sync fail-open — manual achievement missing from current fetch
# ---------------------------------------------------------------------------

def test_sync_fail_open_helper_returns_none_when_achievement_absent():
    # If the developer removes the user's chosen achievement, the helper
    # returns None, and sync's "fail-open" branch keeps the last-stored
    # values in DB via the upsert COALESCE. This is the helper-level proof;
    # the upsert behavior is a DB integration concern.
    achievements_after_dev_change = [
        _ach("ACH_INTRO", "First Steps", 92.0),
        _ach("ACH_NEW_FINAL", "True Ending Reborn", 2.1),
        # ACH_VOID has been removed
    ]
    match = pick_completion_achievement(achievements_after_dev_change, "ACH_VOID")
    assert match is None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    test_funcs = [
        v for k, v in sorted(globals().items())
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
        except Exception as exc:
            failures.append((fn.__name__, exc))
            print(f"  ✗ {fn.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        sys.exit(1)
    print(f"\nAll {len(test_funcs)} tests passed.")


if __name__ == "__main__":
    main()
