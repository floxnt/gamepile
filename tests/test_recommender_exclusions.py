"""Recommender global-exclusion filter.

Run with: uv run python tests/test_recommender_exclusions.py

Covers `_is_globally_excluded` — the universal hard filter applied
before any Shortlist mode-specific eligibility check.

  - blacklisted, not_interested, strong-drop are excluded (pre-existing)
  - software / beta_playtest game types are excluded (new in this commit)
  - linear / multiplayer / mmo / sandbox / no_endpoint / mixed / early_access
    / expansion / unknown remain eligible

No pytest dependency.
"""

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.game_type import (
    GAME_TYPE_BETA_PLAYTEST,
    GAME_TYPE_EARLY_ACCESS,
    GAME_TYPE_EXPANSION,
    GAME_TYPE_LINEAR,
    GAME_TYPE_MIXED,
    GAME_TYPE_MMO,
    GAME_TYPE_MULTIPLAYER,
    GAME_TYPE_NO_ENDPOINT,
    GAME_TYPE_SANDBOX,
    GAME_TYPE_SOFTWARE,
    GAME_TYPE_UNKNOWN,
)
from app.models import GameStatus
from app.recommender import _is_globally_excluded


def _gws(
    *,
    game_type: str = GAME_TYPE_LINEAR,
    status: str = "never_played",
    blacklisted: bool = False,
    dropped_strength: str = None,
):
    """Build a GameWithState-shaped stub. The exclusion check only
    inspects state.blacklisted, state.status, state.dropped_strength,
    and game.game_type (via resolve_type, which short-circuits on the
    cached column)."""
    g = SimpleNamespace(
        appid=hash(game_type) & 0xFFFFFF,
        name=f"Test {game_type}",
        game_type=game_type,
        # resolve_type only consults game_type when it's set — these
        # fields are never read in that case but exist for safety.
        genres="Action",
        tags="",
        user_tags="",
        hltb_main_hours=10.0,
        hltb_completionist_hours=15.0,
        playtime_minutes=0,
        app_type="game",
    )
    g.genre_list = lambda: [s.strip() for s in g.genres.split(",") if s.strip()]
    g.tag_list = lambda: [s.strip() for s in g.tags.split(",") if s.strip()]
    g.user_tags_list = lambda: [s.strip() for s in g.user_tags.split(",") if s.strip()]
    state = SimpleNamespace(
        appid=g.appid,
        status=GameStatus(status),
        blacklisted=blacklisted,
        dropped_strength=dropped_strength,
    )
    return SimpleNamespace(game=g, state=state)


# ---------------------------------------------------------------------------
# Pre-existing exclusions (regression guard)
# ---------------------------------------------------------------------------

def test_blacklisted_excluded():
    assert _is_globally_excluded(_gws(blacklisted=True)) is True


def test_not_interested_excluded():
    assert _is_globally_excluded(_gws(status="not_interested")) is True


def test_strong_drop_excluded():
    assert _is_globally_excluded(
        _gws(status="dropped", dropped_strength="strong")
    ) is True


def test_soft_drop_not_excluded():
    """Soft drops remain eligible — only 'strong' drops are universally
    excluded. Mode-specific logic decides if a soft drop surfaces."""
    assert _is_globally_excluded(
        _gws(status="dropped", dropped_strength="soft")
    ) is False


# ---------------------------------------------------------------------------
# New game-type exclusions
# ---------------------------------------------------------------------------

def test_software_type_excluded():
    """Wallpaper Engine / Lossless Scaling / 3DMark shape — these are
    not games and should never be recommended."""
    assert _is_globally_excluded(_gws(game_type=GAME_TYPE_SOFTWARE)) is True


def test_beta_playtest_type_excluded():
    """Beta and playtest builds are transient and not appropriate
    'Find Games' candidates."""
    assert _is_globally_excluded(_gws(game_type=GAME_TYPE_BETA_PLAYTEST)) is True


# ---------------------------------------------------------------------------
# Types that should remain eligible
# ---------------------------------------------------------------------------

def test_eligible_game_types_not_excluded():
    """Every non-software, non-beta type is eligible. Early access and
    expansion explicitly are NOT excluded — both surface real games."""
    eligible_types = (
        GAME_TYPE_LINEAR,
        GAME_TYPE_MULTIPLAYER,
        GAME_TYPE_NO_ENDPOINT,
        GAME_TYPE_MIXED,
        GAME_TYPE_MMO,
        GAME_TYPE_SANDBOX,
        GAME_TYPE_EXPANSION,
        GAME_TYPE_EARLY_ACCESS,
        GAME_TYPE_UNKNOWN,
    )
    for gt in eligible_types:
        assert _is_globally_excluded(_gws(game_type=gt)) is False, \
            f"{gt} should not be globally excluded"


TESTS = [
    test_blacklisted_excluded,
    test_not_interested_excluded,
    test_strong_drop_excluded,
    test_soft_drop_not_excluded,
    test_software_type_excluded,
    test_beta_playtest_type_excluded,
    test_eligible_game_types_not_excluded,
]


def main() -> None:
    failures = 0
    print(f"Running {len(TESTS)} test(s)…")
    for fn in TESTS:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            failures += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}: unexpected {type(e).__name__}: {e}")
            failures += 1

    print()
    if failures:
        print(f"FAILED {failures}/{len(TESTS)}")
        sys.exit(1)
    print(f"All {len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
