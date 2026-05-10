"""Library stickiness badge filter (v3.5 polish).

Run with: uv run python tests/test_library_stickiness_filter.py

Covers _apply_filters_and_sort's handling of the new stickiness_filter
parameter:
  - Each of the six badge values filters down to matching games
  - Empty string returns the unfiltered set
  - Unrecognised value behaves like empty (defensive — no filter)
  - Manual override is respected (game with stickiness_badge_manual
    matches the override value, not the auto-computed one)
  - Combination with status_filter intersects correctly

No pytest dependency.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.hook_metrics import (
    BADGE_FILTERS_EARLY,
    BADGE_HOOKS_PLAYERS,
    BADGE_LIMITED_DATA,
    BADGE_MARATHON,
    BADGE_MIXED_SIGNALS,
    BADGE_STANDARD_ENGAGEMENT,
)
from app.models import GameStatus
from app.routes.library import _apply_filters_and_sort


def _gws(*, name: str, status: str = "never_played",
         badge_manual: str = None,
         # Auto-computed signal inputs — pre-tuned to land on a specific badge
         completion_rate: float = None,
         completion_rate_confidence: str = None,
         cliff_metric: float = None,
         cliff_position: float = None,
         stickiness_ratio: float = None,
         review_playtime_median: int = None,
         tags: str = "",
         user_tags: str = "") -> SimpleNamespace:
    """Build a GameWithState-shaped stub. _apply_filters_and_sort
    inspects gws.game.* for the badge filter and gws.state.status.value
    for the status filter."""
    g = SimpleNamespace(
        appid=hash(name) & 0xFFFFFF,
        name=name,
        playtime_minutes=0,
        last_played_steam=None,
        installed=None,
        hltb_main_hours=20.0,
        hltb_main_extra_hours=None,
        hltb_completionist_hours=None,
        genres="",
        tags=tags,
        user_tags=user_tags,
        developer="X",
        publisher=None,
        metacritic_score=None,
        opencritic_score=None,
        steam_review_pct=None,
        steam_review_count=None,
        last_refreshed=datetime.utcnow(),
        is_active=True,
        release_date=None,
        description=None,
        completion_rate=completion_rate,
        completion_rate_confidence=completion_rate_confidence,
        completion_achievement_name_manual=None,
        cliff_metric=cliff_metric,
        cliff_position=cliff_position,
        review_playtime_median=review_playtime_median,
        stickiness_ratio=stickiness_ratio,
        playtime_median_avg_ratio=None,
        game_type="linear",
        game_type_manual=False,
        app_type=None,
        hltb_id_manual=None,
        stickiness_badge_manual=badge_manual,
    )
    g.user_tags_list = lambda: [t.strip() for t in g.user_tags.split(",") if t.strip()]
    g.tag_list = lambda: [t.strip() for t in g.tags.split(",") if t.strip()]
    g.genre_list = lambda: []
    state = SimpleNamespace(
        appid=g.appid,
        status=GameStatus(status),
        hours_played_manual=None,
        notes=None,
        updated_at=datetime.utcnow(),
        manually_set=False,
        has_technical_issue=False,
        blacklisted=False,
        dropped_strength=None,
        pinned_for_shortlist=False,
        pinned_at=None,
        personal_rating=None,
    )
    return SimpleNamespace(game=g, state=state)


# Pre-tuned stubs for each auto-computed badge. The signal-value math
# from hook_metrics drives these — see test_hook_phase1c.py for the
# reference cases.
def _hooks_player_game(name: str = "Hooks Game"):
    # +1.5 stickiness + 0 cliff (small) + +0.7 high-conf completion = +2.2
    return _gws(
        name=name,
        completion_rate=0.30, completion_rate_confidence="high",
        cliff_metric=8.0, cliff_position=0.4,
        stickiness_ratio=0.95,
    )


def _filters_early_game(name: str = "Filters Game"):
    # 0 stickiness (mid) + -1 cliff (early & large) + 0 completion = -1.0
    # Below SCORE_FILTERS_THRESHOLD = -1.0 (inclusive).
    return _gws(
        name=name,
        cliff_metric=30.0, cliff_position=0.1,
        stickiness_ratio=0.85,
    )


def _marathon_game(name: str = "Marathon Game"):
    # In-band score, high-conf low completion, very high review playtime.
    # 50h * 60 = 3000 min; needs review_playtime_median >= 3000.
    return _gws(
        name=name,
        completion_rate=0.05, completion_rate_confidence="high",
        cliff_metric=8.0, cliff_position=0.4,
        stickiness_ratio=0.85,  # neutral
        review_playtime_median=3500,
    )


def _mixed_signals_game(name: str = "Mixed Game"):
    # In-band score, at least one strong signal (stickiness +1).
    # +1.5 + 0 cliff + 0 completion = +1.5… but +1.5 hits SCORE_HOOKS_THRESHOLD.
    # Need to land in (-1.0, +1.5). Use stickiness +1 alone with NO completion
    # data — score = +1.5 exactly, which IS hooks_players. Need one less.
    # Use cliff -1 AND stickiness +1: -1 + 1.5 = +0.5, in band, two strong
    # signals → Mixed.
    return _gws(
        name=name,
        cliff_metric=25.0, cliff_position=0.5,  # mid & large → -1
        stickiness_ratio=0.95,                  # +1
        completion_rate=0.10, completion_rate_confidence="high",  # 0 (mid)
    )


def _standard_engagement_game(name: str = "Standard Game"):
    # In-band score, NO strong signals — all values land in the neutral
    # bands (stickiness 0, cliff 0, completion 0).
    return _gws(
        name=name,
        completion_rate=0.10, completion_rate_confidence="high",  # 0
        cliff_metric=8.0, cliff_position=0.4,                      # 0
        stickiness_ratio=0.85,                                      # 0
    )


def _limited_data_game(name: str = "Limited Game"):
    # All NULL — majority of contributing signals NULL → Limited data.
    return _gws(name=name)


# ---------------------------------------------------------------------------
# Empty / unspecified filter passes everything through
# ---------------------------------------------------------------------------

def test_empty_string_filter_returns_all():
    games = [_hooks_player_game(), _filters_early_game(), _limited_data_game()]
    out = _apply_filters_and_sort(games, "", "", "", "", "asc")
    assert len(out) == 3


def test_unrecognised_filter_returns_empty():
    # Defensive: unknown badge values match nothing (rather than silently
    # passing all through). The dropdown only emits valid values, but a
    # hand-edited URL shouldn't return "all games" by accident.
    games = [_hooks_player_game(), _filters_early_game()]
    out = _apply_filters_and_sort(games, "", "", "not_a_real_badge", "", "asc")
    assert out == []


# ---------------------------------------------------------------------------
# Each of the six badge values filters correctly
# ---------------------------------------------------------------------------

def test_filter_hooks_players():
    games = [
        _hooks_player_game("Keep me"),
        _filters_early_game("Drop me"),
        _limited_data_game("Drop me 2"),
    ]
    out = _apply_filters_and_sort(games, "", "", BADGE_HOOKS_PLAYERS, "", "asc")
    assert [g.game.name for g in out] == ["Keep me"]


def test_filter_filters_early():
    games = [
        _hooks_player_game("Drop"),
        _filters_early_game("Keep"),
    ]
    out = _apply_filters_and_sort(games, "", "", BADGE_FILTERS_EARLY, "", "asc")
    assert [g.game.name for g in out] == ["Keep"]


def test_filter_marathon():
    games = [
        _hooks_player_game(),
        _marathon_game("Marathon Keep"),
        _standard_engagement_game(),
    ]
    out = _apply_filters_and_sort(games, "", "", BADGE_MARATHON, "", "asc")
    assert [g.game.name for g in out] == ["Marathon Keep"]


def test_filter_mixed_signals():
    games = [
        _hooks_player_game(),
        _mixed_signals_game("Mixed Keep"),
        _standard_engagement_game(),
    ]
    out = _apply_filters_and_sort(games, "", "", BADGE_MIXED_SIGNALS, "", "asc")
    assert [g.game.name for g in out] == ["Mixed Keep"]


def test_filter_standard_engagement():
    games = [
        _hooks_player_game(),
        _standard_engagement_game("Standard Keep"),
        _limited_data_game(),
    ]
    out = _apply_filters_and_sort(games, "", "", BADGE_STANDARD_ENGAGEMENT, "", "asc")
    assert [g.game.name for g in out] == ["Standard Keep"]


def test_filter_limited_data():
    games = [
        _hooks_player_game(),
        _limited_data_game("Limited Keep 1"),
        _limited_data_game("Limited Keep 2"),
    ]
    out = _apply_filters_and_sort(games, "", "", BADGE_LIMITED_DATA, "", "asc")
    names = sorted(g.game.name for g in out)
    assert names == ["Limited Keep 1", "Limited Keep 2"]


# ---------------------------------------------------------------------------
# Manual override is respected — display-aware filter precedence
# ---------------------------------------------------------------------------

def test_manual_override_drives_filter():
    # Game whose AUTO badge would be Filters early, but user overrode to
    # Hooks players. ?stickiness=hooks_players should match it; the same
    # query against the auto-Hooks game should also match.
    auto_filters_overridden_to_hooks = _filters_early_game("Override")
    auto_filters_overridden_to_hooks.game.stickiness_badge_manual = BADGE_HOOKS_PLAYERS
    auto_hooks = _hooks_player_game("Auto Hooks")
    auto_filters_no_override = _filters_early_game("No Override")
    games = [auto_filters_overridden_to_hooks, auto_hooks, auto_filters_no_override]

    out_hooks = _apply_filters_and_sort(games, "", "", BADGE_HOOKS_PLAYERS, "", "asc")
    out_filters = _apply_filters_and_sort(games, "", "", BADGE_FILTERS_EARLY, "", "asc")
    assert sorted(g.game.name for g in out_hooks) == ["Auto Hooks", "Override"]
    assert [g.game.name for g in out_filters] == ["No Override"]


def test_manual_override_to_ineligible_type_still_filters():
    # Phase 4 allows override on software/beta/etc. types where auto would
    # short-circuit to limited_data. Filter should still match the override.
    g = _gws(name="Software Override", badge_manual=BADGE_HOOKS_PLAYERS)
    g.game.game_type = "software"
    games = [g, _limited_data_game("Just Limited")]
    out = _apply_filters_and_sort(games, "", "", BADGE_HOOKS_PLAYERS, "", "asc")
    assert [g.game.name for g in out] == ["Software Override"]


# ---------------------------------------------------------------------------
# Combination with status filter — intersect correctly
# ---------------------------------------------------------------------------

def test_status_and_stickiness_filters_intersect():
    games = [
        _gws(name="Played Hooks", status="finished",
             completion_rate=0.30, completion_rate_confidence="high",
             cliff_metric=8.0, cliff_position=0.4, stickiness_ratio=0.95),
        _gws(name="Unplayed Hooks", status="never_played",
             completion_rate=0.30, completion_rate_confidence="high",
             cliff_metric=8.0, cliff_position=0.4, stickiness_ratio=0.95),
        _gws(name="Played Limited", status="finished"),
    ]
    out = _apply_filters_and_sort(
        games, status_filter="finished", tag_filter="",
        stickiness_filter=BADGE_HOOKS_PLAYERS, sort="", direction="asc",
    )
    assert [g.game.name for g in out] == ["Played Hooks"]


def test_combination_filters_can_return_empty():
    # Status + stickiness combination with no overlap → empty result.
    # Drives the multi-filter empty-state copy in the template.
    games = [
        _gws(name="Played Limited", status="finished"),
        _gws(name="Unplayed Limited", status="never_played"),
    ]
    out = _apply_filters_and_sort(
        games, status_filter="finished", tag_filter="",
        stickiness_filter=BADGE_HOOKS_PLAYERS, sort="", direction="asc",
    )
    assert out == []


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
