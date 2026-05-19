"""Phase 4 — manual stickiness badge override.

DORMANT (v0.7.0): exercises app.hook_metrics.compute_stickiness_signal_display,
which was retained-dormant when hook-point/stickiness was removed from the
live UI. Tests stay green as correctness guarantee — do NOT delete on a
future cleanup pass. See SPEC_HOOK_RETIREMENT.md.

Run with: uv run python tests/test_phase4_stickiness_override.py

Covers:
  - compute_stickiness_signal_display — wrapper return shape, override
    vs auto resolution, ineligible-type pass-through
  - All five active BADGE_* constants honored as overrides
  - Limited_data display-layer pass-through
  - Game type's normal hide rules don't suppress the override on
    software / beta_playtest / early_access / unknown

No pytest dependency — pure assertions.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.hook_metrics import (
    ACTIVE_BADGES,
    BADGE_FILTERS_EARLY,
    BADGE_HOOKS_PLAYERS,
    BADGE_LIMITED_DATA,
    BADGE_LABELS,
    BADGE_MARATHON,
    BADGE_MIXED_SIGNALS,
    BADGE_STANDARD_ENGAGEMENT,
    compute_stickiness_signal,
    compute_stickiness_signal_display,
)


def _game(**kw):
    """Stub game with the fields the signal helpers and Phase 4 override
    inspect. Mirrors test_hook_phase1c.py's _game shape with the new
    stickiness_badge_manual field defaulted to None."""
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
        "hltb_id_manual": None,
        "playtime_minutes": 0,
        "app_type": None,
        "genres": "",
        "tags": "",
        "user_tags": "",
        "stickiness_badge_manual": None,
    }
    defaults.update(kw)
    g = SimpleNamespace(**defaults)
    g.genre_list = lambda: [x.strip() for x in g.genres.split(",") if x.strip()]
    g.tag_list = lambda: [x.strip() for x in g.tags.split(",") if x.strip()]
    g.user_tags_list = lambda: [x.strip() for x in g.user_tags.split(",") if x.strip()]
    return g


# ---------------------------------------------------------------------------
# ACTIVE_BADGES + BADGE_LABELS contract
# ---------------------------------------------------------------------------

def test_active_badges_excludes_limited_data():
    # Limited data is the "no signal" sink — manually asserting it is
    # meaningless. The route layer validates against ACTIVE_BADGES.
    assert BADGE_LIMITED_DATA not in ACTIVE_BADGES


def test_active_badges_has_seven_entries():
    from app.hook_metrics import BADGE_USUALLY_HOOKS, BADGE_OFTEN_FILTERS
    assert len(ACTIVE_BADGES) == 7
    assert set(ACTIVE_BADGES) == {
        BADGE_HOOKS_PLAYERS,
        BADGE_USUALLY_HOOKS,
        BADGE_FILTERS_EARLY,
        BADGE_OFTEN_FILTERS,
        BADGE_MARATHON,
        BADGE_MIXED_SIGNALS,
        BADGE_STANDARD_ENGAGEMENT,
    }


def test_badge_labels_cover_all_badges():
    # Display layer needs labels for limited_data too — the no-data
    # state on Library / Game Detail.
    assert BADGE_LIMITED_DATA in BADGE_LABELS
    for b in ACTIVE_BADGES:
        assert b in BADGE_LABELS


# ---------------------------------------------------------------------------
# compute_stickiness_signal_display — return shape
# ---------------------------------------------------------------------------

def test_display_returns_5_tuple():
    g = _game()
    result = compute_stickiness_signal_display(g)
    assert len(result) == 5


def test_display_no_override_matches_auto_path():
    # No override set → display.badge == auto.badge, is_overridden == False.
    g = _game(
        completion_rate=0.30, completion_rate_confidence="high",
        cliff_metric=8.0, cliff_position=0.4,
        stickiness_ratio=0.95,
    )
    auto = compute_stickiness_signal(g)
    disp = compute_stickiness_signal_display(g)
    assert disp[0] == auto[0]  # badge
    assert disp[1] == auto[0]  # auto_badge equals same value
    assert disp[2] == auto[1]  # score
    assert disp[3] == auto[2]  # breakdown
    assert disp[4] is False    # is_overridden


# ---------------------------------------------------------------------------
# Override resolution — manual wins, auto is preserved for display
# ---------------------------------------------------------------------------

def test_override_replaces_displayed_badge():
    # Game would auto-compute Hooks players. Override forces Filters early.
    g = _game(
        completion_rate=0.30, completion_rate_confidence="high",
        cliff_metric=8.0, cliff_position=0.4,
        stickiness_ratio=0.95,  # → +1.5 + 0 + +0.7 = +2.2 → Hooks
        stickiness_badge_manual=BADGE_FILTERS_EARLY,
    )
    badge, auto_badge, score, breakdown, is_overridden = compute_stickiness_signal_display(g)
    assert badge == BADGE_FILTERS_EARLY
    assert auto_badge == BADGE_HOOKS_PLAYERS
    assert is_overridden is True
    # Score and breakdown reflect the AUTO computation, unchanged.
    assert abs(score - 2.2) < 1e-9
    assert "stickiness" in breakdown
    assert breakdown["stickiness"]["value"] == 1


def test_override_preserves_breakdown_for_template_display():
    # The template uses breakdown to render "Auto would say: …" with
    # the per-signal score detail. Verify it survives the override.
    g = _game(
        completion_rate=0.06, completion_rate_confidence="high",
        cliff_metric=25.0, cliff_position=0.1,  # early-large cliff
        stickiness_ratio=0.45,
        stickiness_badge_manual=BADGE_HOOKS_PLAYERS,
    )
    _, _, _, breakdown, _ = compute_stickiness_signal_display(g)
    # All three contributing signals should be present.
    assert "stickiness" in breakdown
    assert "cliff" in breakdown
    assert "completion" in breakdown


def test_override_each_active_badge_round_trips():
    g = _game()
    for badge in ACTIVE_BADGES:
        g.stickiness_badge_manual = badge
        result = compute_stickiness_signal_display(g)
        assert result[0] == badge
        assert result[4] is True


# ---------------------------------------------------------------------------
# Override on ineligible game types — auto would be limited_data,
# override surfaces anyway
# ---------------------------------------------------------------------------

def test_override_surfaces_on_software():
    g = _game(game_type="software", stickiness_badge_manual=BADGE_HOOKS_PLAYERS)
    badge, auto_badge, _, breakdown, is_overridden = compute_stickiness_signal_display(g)
    assert badge == BADGE_HOOKS_PLAYERS
    assert auto_badge == BADGE_LIMITED_DATA
    assert breakdown == {}  # auto path short-circuited; nothing to itemise
    assert is_overridden is True


def test_override_surfaces_on_beta_playtest():
    g = _game(game_type="beta_playtest", stickiness_badge_manual=BADGE_FILTERS_EARLY)
    badge, auto_badge, _, _, is_overridden = compute_stickiness_signal_display(g)
    assert badge == BADGE_FILTERS_EARLY
    assert auto_badge == BADGE_LIMITED_DATA
    assert is_overridden is True


def test_override_surfaces_on_early_access():
    g = _game(game_type="early_access", stickiness_badge_manual=BADGE_MARATHON)
    badge, auto_badge, _, _, is_overridden = compute_stickiness_signal_display(g)
    assert badge == BADGE_MARATHON
    assert auto_badge == BADGE_LIMITED_DATA
    assert is_overridden is True


def test_override_surfaces_on_unknown():
    g = _game(game_type="unknown", stickiness_badge_manual=BADGE_MIXED_SIGNALS)
    badge, auto_badge, _, _, is_overridden = compute_stickiness_signal_display(g)
    assert badge == BADGE_MIXED_SIGNALS
    assert auto_badge == BADGE_LIMITED_DATA
    assert is_overridden is True


# ---------------------------------------------------------------------------
# Override on data-sparse eligible types — auto would be limited_data
# but for a different reason (insufficient signal density), override
# still surfaces
# ---------------------------------------------------------------------------

def test_override_surfaces_on_data_sparse_linear():
    # Linear game with all three signals NULL → auto returns
    # BADGE_LIMITED_DATA via the "majority NULL" gate. Override still wins.
    g = _game(game_type="linear", stickiness_badge_manual=BADGE_HOOKS_PLAYERS)
    badge, auto_badge, _, _, is_overridden = compute_stickiness_signal_display(g)
    assert badge == BADGE_HOOKS_PLAYERS
    assert auto_badge == BADGE_LIMITED_DATA
    assert is_overridden is True


# ---------------------------------------------------------------------------
# Empty-string defensive handling — DB layer should write NULL for empty,
# but the display helper shouldn't blow up on an empty string slipping through
# ---------------------------------------------------------------------------

def test_empty_string_override_treated_as_no_override():
    g = _game(stickiness_badge_manual="")
    result = compute_stickiness_signal_display(g)
    assert result[4] is False  # is_overridden — empty string is falsy


# ---------------------------------------------------------------------------
# (v0.7.0: test_library_sort_uses_displayed_badge removed — it exercised
# app.routes.library._STICKINESS_SORT_ORDER which was deleted when the
# Library stickiness column was removed. The display-helper coverage above
# remains the correctness guarantee for the retained-dormant pipeline.)
# ---------------------------------------------------------------------------


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
