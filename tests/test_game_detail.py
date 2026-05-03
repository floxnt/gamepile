"""Test fixtures for app.game_detail.

Run with: uv run python tests/test_game_detail.py

No pytest dependency — pure assertions.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.game_detail import (
    AffinityCategory,
    PickHistoryRow,
    compute_per_game_affinity_pills,
    format_pick_history_rows,
    parse_status_form_value,
    relative_time,
    valid_status_transitions,
)
from app.models import Game, GameStatus, PickHistory


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _game(genres="Action,RPG", user_tags="Soulslike,Atmospheric", developer="FromSoftware"):
    return Game(
        appid=1, name="Test Game", playtime_minutes=0, last_played_steam=None,
        installed=None, hltb_main_hours=10.0, hltb_main_extra_hours=None,
        hltb_completionist_hours=15.0, genres=genres, tags="Single-player",
        developer=developer, publisher=None, metacritic_score=None,
        opencritic_score=None, steam_review_pct=None, steam_review_count=None,
        last_refreshed=datetime.utcnow(), is_active=True, user_tags=user_tags,
    )


def _pick(picked_at=None, mode="i_only_have_tonight", outcome=None,
          rating=None, did_not_play_reason=None, actually_played_appid=None,
          time_window_minutes=90, genre_match_rating=None):
    return PickHistory(
        id=None, appid=1, game_name="Test", picked_at=picked_at or datetime(2026, 5, 3),
        time_window_minutes=time_window_minutes, mode=mode,
        candidates_at_pick="[]", outcome=outcome, outcome_recorded_at=None,
        rating=rating, genre_match_rating=genre_match_rating,
        would_have_picked_other_appid=None,
        did_not_play_reason=did_not_play_reason,
        actually_played_appid=actually_played_appid,
    )


# ---------------------------------------------------------------------------
# valid_status_transitions
# ---------------------------------------------------------------------------

def test_status_transitions_from_never_played():
    options = valid_status_transitions(GameStatus.never_played)
    values = [v for v, _, _ in options]
    # Current state listed first, marked is_current
    assert values[0] == "never_played"
    assert options[0][2] is True
    # Valid transitions present
    for v in ("played_unclassified", "not_interested", "in_progress", "finished"):
        assert v in values
    # is_current is True only for the current state
    currents = [v for v, _, c in options if c]
    assert currents == ["never_played"]


def test_status_transitions_from_dropped_soft():
    # Current dropped/soft renders as 'dropped_soft' selected
    options = valid_status_transitions(GameStatus.dropped, current_dropped_strength="soft")
    assert options[0][0] == "dropped_soft"
    assert options[0][2] is True
    # Transitions out of dropped: in_progress, finished, not_interested
    values = [v for v, _, _ in options]
    for v in ("in_progress", "finished", "not_interested"):
        assert v in values


def test_status_transitions_from_dropped_strong():
    options = valid_status_transitions(GameStatus.dropped, current_dropped_strength="strong")
    assert options[0][0] == "dropped_strong"
    assert options[0][2] is True


def test_status_transitions_labels():
    options = valid_status_transitions(GameStatus.played_unclassified)
    labels = {v: lbl for v, lbl, _ in options}
    assert labels["dropped_soft"] == "Bounced off it"
    assert labels["dropped_strong"] == "Not my thing"
    assert labels["in_progress"] == "In progress"
    assert labels["finished"] == "Finished"


def test_status_transitions_no_duplicates():
    """Current state shouldn't be duplicated even if it appears in transitions list."""
    options = valid_status_transitions(GameStatus.in_progress)
    values = [v for v, _, _ in options]
    assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# parse_status_form_value
# ---------------------------------------------------------------------------

def test_parse_status_form_value():
    assert parse_status_form_value("dropped_soft") == ("dropped", "soft")
    assert parse_status_form_value("dropped_strong") == ("dropped", "strong")
    assert parse_status_form_value("in_progress") == ("in_progress", None)
    assert parse_status_form_value("finished") == ("finished", None)
    assert parse_status_form_value("never_played") == ("never_played", None)


# ---------------------------------------------------------------------------
# compute_per_game_affinity_pills
# ---------------------------------------------------------------------------

def test_per_game_affinity_pills_empty():
    g = _game()
    cats = compute_per_game_affinity_pills(g, {})
    assert all(isinstance(c, AffinityCategory) for c in cats)
    assert all(c.pills == [] for c in cats)


def test_per_game_affinity_pills_basic():
    g = _game(genres="Action", user_tags="Soulslike", developer="FromSoftware")
    affinities = {
        ("genre", "action"): (3.4, 5),
        ("tag", "soulslike"): (4.1, 8),
        ("developer", "fromsoftware"): (5.2, 7),
    }
    cats = compute_per_game_affinity_pills(g, affinities)
    by_name = {c.name: c.pills for c in cats}
    assert len(by_name["Genres"]) == 1
    assert by_name["Genres"][0].label == "Action"
    assert by_name["Genres"][0].weight == 3.4
    assert by_name["Tags"][0].label == "Soulslike"
    assert by_name["Developers"][0].label == "FromSoftware"


def test_per_game_affinity_pills_neutral_excluded():
    g = _game(genres="Action", user_tags="Soulslike", developer=None)
    affinities = {
        ("genre", "action"): (0.4, 1),       # below cutoff, excluded
        ("tag", "soulslike"): (3.0, 5),
    }
    cats = compute_per_game_affinity_pills(g, affinities)
    by_name = {c.name: c.pills for c in cats}
    assert by_name["Genres"] == []
    assert len(by_name["Tags"]) == 1


def test_per_game_affinity_pills_low_confidence_flag():
    g = _game(genres="Action", user_tags="", developer=None)
    affinities = {("genre", "action"): (3.0, 2)}  # pick_count < 3
    cats = compute_per_game_affinity_pills(g, affinities)
    pill = cats[0].pills[0]
    assert pill.low_confidence is True

    affinities = {("genre", "action"): (3.0, 5)}
    cats = compute_per_game_affinity_pills(g, affinities)
    assert cats[0].pills[0].low_confidence is False


def test_per_game_affinity_pills_dedup():
    """Dedup precedence: developer > tag > genre. 'Action' as both genre and
    tag should appear only under tag (the higher-precision kind that won)."""
    g = _game(genres="Action", user_tags="Action", developer=None)
    affinities = {
        ("genre", "action"): (2.0, 5),
        ("tag", "action"): (3.0, 5),
    }
    cats = compute_per_game_affinity_pills(g, affinities)
    by_name = {c.name: c.pills for c in cats}
    # Only the tag-side wins; genre side should not have an "Action" pill.
    assert by_name["Genres"] == []
    assert len(by_name["Tags"]) == 1
    assert by_name["Tags"][0].weight == 3.0


# ---------------------------------------------------------------------------
# format_pick_history_rows
# ---------------------------------------------------------------------------

def test_format_pick_basic_outcome():
    p = _pick(
        picked_at=datetime(2026, 5, 3),
        mode="continue_something",
        outcome="played_and_finished",
        rating=5,
        genre_match_rating=5,
    )
    rows = format_pick_history_rows([p])
    assert len(rows) == 1
    r = rows[0]
    assert r.date_label == "May 3, 2026"
    assert r.mode_label == "Continue something"
    assert r.window_label == "90 min window"
    assert "Played and finished" in r.outcome_label
    assert "Rating: 5/5" in r.outcome_label
    assert "Genre fit: 5/5" in r.outcome_label
    assert r.rating == 5


def test_format_pick_did_not_play_with_retroactive():
    p = _pick(
        picked_at=datetime(2026, 4, 18),
        mode="i_only_have_tonight",
        did_not_play_reason="picked_another_game",
        actually_played_appid=42,
    )
    rows = format_pick_history_rows([p], game_name_by_appid={42: "Hades"})
    r = rows[0]
    assert "Did not play" in r.outcome_label
    assert "picked another game" in r.outcome_label
    assert "Picked instead: Hades" in r.outcome_label


def test_format_pick_no_window():
    p = _pick(mode="surprise_me", time_window_minutes=None, outcome="played_still_going", rating=4)
    rows = format_pick_history_rows([p])
    assert rows[0].window_label == "no window"
    assert rows[0].mode_label == "Surprise me"


def test_format_pick_legacy_mode():
    p = _pick(mode="both", outcome="played_and_finished", rating=4)
    rows = format_pick_history_rows([p])
    assert rows[0].mode_label == "I only have tonight"  # legacy alias


def test_format_pick_no_outcome_yet():
    p = _pick(outcome=None)
    rows = format_pick_history_rows([p])
    assert "No outcome recorded yet" in rows[0].outcome_label


# ---------------------------------------------------------------------------
# relative_time
# ---------------------------------------------------------------------------

def test_relative_time():
    now = datetime(2026, 5, 3, 12, 0, 0)
    assert relative_time(None, now) == "Never"
    assert relative_time(now, now) == "just now"
    assert relative_time(now - timedelta(seconds=30), now) == "just now"
    assert relative_time(now - timedelta(minutes=5), now) == "5 minutes ago"
    assert relative_time(now - timedelta(minutes=1), now) == "1 minute ago"
    assert relative_time(now - timedelta(hours=3), now) == "3 hours ago"
    assert relative_time(now - timedelta(hours=1), now) == "1 hour ago"
    assert relative_time(now - timedelta(days=5), now) == "5 days ago"
    assert relative_time(now - timedelta(days=45), now) == "1 month ago"
    assert relative_time(now - timedelta(days=400), now) == "1 year ago"
    # Future dates clamp to "just now"
    assert relative_time(now + timedelta(hours=1), now) == "just now"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_status_transitions_from_never_played,
    test_status_transitions_from_dropped_soft,
    test_status_transitions_from_dropped_strong,
    test_status_transitions_labels,
    test_status_transitions_no_duplicates,
    test_parse_status_form_value,
    test_per_game_affinity_pills_empty,
    test_per_game_affinity_pills_basic,
    test_per_game_affinity_pills_neutral_excluded,
    test_per_game_affinity_pills_low_confidence_flag,
    test_per_game_affinity_pills_dedup,
    test_format_pick_basic_outcome,
    test_format_pick_did_not_play_with_retroactive,
    test_format_pick_no_window,
    test_format_pick_legacy_mode,
    test_format_pick_no_outcome_yet,
    test_relative_time,
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
