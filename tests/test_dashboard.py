"""Test fixtures for app.dashboard.

Run with: uv run python tests/test_dashboard.py

No pytest dependency — pure assertions, same pattern as test_hltb_cleaner.py.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.dashboard import (
    Pill,
    build_affinity_profile,
    compute_days_since_last_pick,
    compute_finished_this_month,
    compute_picks_per_week,
    is_backlog_pick,
    month_start_label,
    resolve_callout_mode,
)
from app.models import Game, GameState, GameStatus, GameWithState, PickHistory


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _pick(picked_at, status_at_pick=None, was_forever_at_pick=None, mode="i_only_have_tonight"):
    """Build a PickHistory for testing.

    NULL-as-include rule (mirrors is_backlog_pick docstring): rows from
    before the v3 Dashboard schema migration have NULL on both new fields
    and are charitably treated as backlog-eligible. Tests use that default
    explicitly to confirm the rule.
    """
    return PickHistory(
        id=None, appid=1, game_name="Test", picked_at=picked_at,
        time_window_minutes=None, mode=mode, candidates_at_pick="[]",
        outcome=None, outcome_recorded_at=None, rating=None,
        genre_match_rating=None, would_have_picked_other_appid=None,
        did_not_play_reason=None, actually_played_appid=None,
        status_at_pick=status_at_pick,
        was_forever_at_pick=was_forever_at_pick,
    )


def _game(appid=1, name="Test", status=GameStatus.never_played, hltb_main=10.0,
          hltb_compl=15.0, playtime=0, tags="Single-player", user_tags="",
          updated_at=None, finished_at=None):
    g = Game(
        appid=appid, name=name, playtime_minutes=playtime,
        last_played_steam=None, installed=None, hltb_main_hours=hltb_main,
        hltb_main_extra_hours=None, hltb_completionist_hours=hltb_compl,
        genres="", tags=tags, developer=None, publisher=None,
        metacritic_score=None, opencritic_score=None, steam_review_pct=None,
        steam_review_count=None, last_refreshed=datetime.utcnow(),
        is_active=True, user_tags=user_tags,
    )
    s = GameState(
        appid=appid, status=status, hours_played_manual=None, notes=None,
        updated_at=updated_at or datetime.utcnow(),
        finished_at=finished_at,
    )
    return GameWithState(game=g, state=s)


# ---------------------------------------------------------------------------
# is_backlog_pick — the NULL-as-include rule + status filter + forever filter
# ---------------------------------------------------------------------------

def test_is_backlog_pick():
    now = datetime(2026, 5, 3, 12, 0, 0)

    # NULL on both fields = legacy row; charity rule includes it.
    assert is_backlog_pick(_pick(now)) is True
    # was_forever_at_pick=True excludes regardless of status.
    assert is_backlog_pick(_pick(now, status_at_pick="in_progress", was_forever_at_pick=True)) is False
    # Backlog statuses include.
    for status in ("never_played", "played_unclassified", "in_progress"):
        assert is_backlog_pick(_pick(now, status_at_pick=status, was_forever_at_pick=False)) is True
    # Non-backlog statuses exclude (Comfort Pick of finished game is the
    # canonical case the spec wants filtered out).
    for status in ("finished", "dropped", "not_interested"):
        assert is_backlog_pick(_pick(now, status_at_pick=status, was_forever_at_pick=False)) is False


# ---------------------------------------------------------------------------
# compute_picks_per_week — 7-day window, eligibility filter
# ---------------------------------------------------------------------------

def test_compute_picks_per_week():
    now = datetime(2026, 5, 3, 12, 0, 0)
    eight_days_ago = now - timedelta(days=8)
    six_days_ago = now - timedelta(days=6)
    yesterday = now - timedelta(days=1)

    picks = [
        _pick(eight_days_ago, status_at_pick="in_progress", was_forever_at_pick=False),  # outside window
        _pick(six_days_ago, status_at_pick="never_played", was_forever_at_pick=False),    # in
        _pick(yesterday, status_at_pick="in_progress", was_forever_at_pick=False),        # in
        _pick(yesterday, status_at_pick="finished", was_forever_at_pick=False),           # excluded by status
        _pick(yesterday, status_at_pick="in_progress", was_forever_at_pick=True),         # excluded by forever
        _pick(yesterday),  # legacy row, NULL = include
    ]
    assert compute_picks_per_week(picks, now) == 3

    # Empty list → zero
    assert compute_picks_per_week([], now) == 0


# ---------------------------------------------------------------------------
# compute_finished_this_month — boundary handling
# ---------------------------------------------------------------------------

def test_compute_finished_this_month():
    now = datetime(2026, 5, 3, 12, 0, 0)
    start_of_may = datetime(2026, 5, 1, 0, 0, 0)
    end_of_april = datetime(2026, 4, 30, 23, 59, 0)

    # Keyed on finished_at as of v0.9.12 (updated_at moved on any state
    # write, so a late note edit used to re-date the completion).
    games = [
        _game(appid=1, status=GameStatus.finished, finished_at=datetime(2026, 5, 2, 10, 0, 0)),   # in
        _game(appid=2, status=GameStatus.finished, finished_at=start_of_may),                       # in (boundary)
        _game(appid=3, status=GameStatus.finished, finished_at=end_of_april),                       # out
        _game(appid=4, status=GameStatus.in_progress, finished_at=datetime(2026, 5, 2, 10, 0, 0)), # out (status)
        _game(appid=5, status=GameStatus.finished, finished_at=datetime(2026, 5, 3, 11, 0, 0)),    # in
    ]
    assert compute_finished_this_month(games, now) == 3
    assert compute_finished_this_month([], now) == 0


def test_finished_this_month_ignores_updated_at():
    """A game finished in April whose row was touched in May (note edit,
    rating change) must not count toward May. This is the bug finished_at
    was added to fix."""
    now = datetime(2026, 5, 3, 12, 0, 0)
    games = [
        _game(appid=1, status=GameStatus.finished,
              finished_at=datetime(2026, 4, 10, 9, 0, 0),
              updated_at=datetime(2026, 5, 2, 10, 0, 0)),
    ]
    assert compute_finished_this_month(games, now) == 0


def test_finished_this_month_skips_null_finished_at():
    """A finished row with no timestamp can't be attributed to a month."""
    now = datetime(2026, 5, 3, 12, 0, 0)
    games = [
        _game(appid=1, status=GameStatus.finished,
              updated_at=datetime(2026, 5, 2, 10, 0, 0), finished_at=None),
    ]
    assert compute_finished_this_month(games, now) == 0


# ---------------------------------------------------------------------------
# compute_days_since_last_pick — None / 0 / N
# ---------------------------------------------------------------------------

def test_compute_days_since_last_pick():
    now = datetime(2026, 5, 3, 12, 0, 0)
    assert compute_days_since_last_pick(None, now) is None
    assert compute_days_since_last_pick(_pick(now), now) == 0
    assert compute_days_since_last_pick(_pick(now - timedelta(days=12)), now) == 12
    # A pick "in the future" (clock skew) clamps to 0, doesn't go negative.
    assert compute_days_since_last_pick(_pick(now + timedelta(hours=1)), now) == 0


# ---------------------------------------------------------------------------
# build_affinity_profile — top-N, neutral exclusion, low-confidence,
# negatives section unlock threshold
# ---------------------------------------------------------------------------

def test_build_affinity_profile_basic():
    affinities = {
        ("genre", "action"): (3.4, 5),
        ("genre", "rpg"): (2.8, 4),
        ("genre", "indie"): (1.5, 3),
        ("genre", "puzzle"): (0.4, 1),       # neutral, excluded
        ("tag", "souls-like"): (4.1, 8),
        ("developer", "fromsoftware"): (5.2, 7),
        ("developer", "obsidian"): (0.6, 2), # positive, low-confidence
    }
    profile = build_affinity_profile(affinities)

    assert not profile.is_empty
    assert [p.label for p in profile.genres] == ["Action", "Rpg", "Indie"]  # puzzle excluded
    assert profile.tags[0].label == "Souls-Like"
    assert profile.developers[0].label == "Fromsoftware"
    assert profile.negatives == []  # nothing below -1.0

    # Low-confidence flag: pick_count < 3
    obsidian = next(p for p in profile.developers if p.label == "Obsidian")
    assert obsidian.low_confidence is True
    fromsoft = next(p for p in profile.developers if p.label == "Fromsoftware")
    assert fromsoft.low_confidence is False


def test_build_affinity_profile_negatives_section():
    # No entry below -1.0 → negatives section stays empty even if there are
    # mildly-negative pills.
    affinities = {
        ("genre", "action"): (3.0, 5),
        ("tag", "casual"): (-0.7, 4),  # negative but above unlock threshold
    }
    profile = build_affinity_profile(affinities)
    assert profile.negatives == []

    # One entry below -1.0 → unlocks. Top 3 of weight < -0.5.
    affinities = {
        ("genre", "action"): (3.0, 5),
        ("tag", "multiplayer"): (-1.8, 6),    # unlocks
        ("tag", "casual"): (-1.2, 4),
        ("tag", "battle royale"): (-1.0, 3),  # exactly -1.0; included in cooler-on (weight < -0.5)
        ("genre", "racing"): (-0.6, 2),       # included (weight < -0.5)
        ("genre", "fps"): (-0.4, 1),          # excluded (|weight| ≤ 0.5)
    }
    profile = build_affinity_profile(affinities)
    assert len(profile.negatives) == 3
    # Sorted ascending = most-negative first
    assert profile.negatives[0].label == "Multiplayer"
    assert profile.negatives[0].weight == -1.8
    # 4th-place (-0.6) is excluded by top-3 cap
    labels = [p.label for p in profile.negatives]
    assert "Racing" not in labels


def test_build_affinity_profile_empty():
    assert build_affinity_profile({}).is_empty is True
    # All entries within the neutral band → still empty
    affinities = {("genre", "rpg"): (0.3, 1)}
    profile = build_affinity_profile(affinities)
    assert profile.is_empty is True


# ---------------------------------------------------------------------------
# resolve_callout_mode — Forever excluded from in_progress check
# ---------------------------------------------------------------------------

def test_resolve_callout_mode():
    # No in_progress games → tonight
    games = [_game(status=GameStatus.never_played)]
    assert resolve_callout_mode(games) == "i_only_have_tonight"

    # In-progress, not forever → continue
    games = [_game(status=GameStatus.in_progress, hltb_main=10.0, hltb_compl=15.0)]
    assert resolve_callout_mode(games) == "continue_something"

    # In-progress + Forever (multiplayer-only) → tonight (excluded from check)
    games = [_game(status=GameStatus.in_progress, tags="Multi-player", hltb_main=None, hltb_compl=None)]
    assert resolve_callout_mode(games) == "i_only_have_tonight"

    # Mix: one Forever in_progress, one regular in_progress → continue (regular wins)
    games = [
        _game(appid=1, status=GameStatus.in_progress, tags="Multi-player", hltb_main=None),
        _game(appid=2, status=GameStatus.in_progress, hltb_main=10.0, hltb_compl=15.0),
    ]
    assert resolve_callout_mode(games) == "continue_something"


# ---------------------------------------------------------------------------
# month_start_label
# ---------------------------------------------------------------------------

def test_month_start_label():
    assert month_start_label(datetime(2026, 5, 3)) == "May 1"
    assert month_start_label(datetime(2026, 1, 31)) == "January 1"
    assert month_start_label(datetime(2026, 12, 1)) == "December 1"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_is_backlog_pick,
    test_compute_picks_per_week,
    test_compute_finished_this_month,
    test_finished_this_month_ignores_updated_at,
    test_finished_this_month_skips_null_finished_at,
    test_compute_days_since_last_pick,
    test_build_affinity_profile_basic,
    test_build_affinity_profile_negatives_section,
    test_build_affinity_profile_empty,
    test_resolve_callout_mode,
    test_month_start_label,
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
