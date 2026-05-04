"""Test fixtures for app.game_type.

Run with: uv run python tests/test_game_type.py

No pytest dependency — pure assertions.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.game_type import (
    ALL_GAME_TYPES,
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
    SOFTWARE_APPID_LIST,
    classify_game,
    engagement_display_rules,
    is_forever_game,
    resolve_type,
)
from app.models import Game


def _game(
    appid=12345,
    name="Test",
    *,
    playtime=0,
    hltb_main=10.0,
    hltb_compl=15.0,
    genres="Action",
    categories="Single-player",  # Steam categories live in `tags` column
    user_tags="",
    app_type="game",
):
    """Build a Game with the fields classify_game inspects. Defaults make
    a vanilla single-player game with HLTB main present → LINEAR."""
    return Game(
        appid=appid, name=name, playtime_minutes=playtime,
        last_played_steam=None, installed=None,
        hltb_main_hours=hltb_main, hltb_main_extra_hours=None,
        hltb_completionist_hours=hltb_compl,
        genres=genres, tags=categories, developer=None, publisher=None,
        metacritic_score=None, opencritic_score=None,
        steam_review_pct=None, steam_review_count=None,
        last_refreshed=datetime.utcnow(), is_active=True,
        user_tags=user_tags, app_type=app_type,
    )


# ---------------------------------------------------------------------------
# Beta/playtest detection (rule 1 — highest priority)
# ---------------------------------------------------------------------------

def test_beta_keyword_in_title():
    for kw in ("Battlefield 6 Open Beta", "ARC Raiders Playtest",
               "Game Public Test", "MyGame PTS",
               "Server Test Server", "Mod Testing Branch",
               "Dev Staging Branch", "Starbound Unstable"):
        assert classify_game(_game(name=kw)) == GAME_TYPE_BETA_PLAYTEST, f"failed for {kw}"


def test_beta_overrides_multiplayer():
    """Beta detection wins over multiplayer-focus."""
    g = _game(
        name="Counter-Strike Beta",
        categories="Multi-player",
        hltb_main=None,
    )
    assert classify_game(g) == GAME_TYPE_BETA_PLAYTEST


# ---------------------------------------------------------------------------
# Software detection (rule 2)
# ---------------------------------------------------------------------------

def test_software_via_curated_appid():
    """Curated path should classify any appid in SOFTWARE_APPID_LIST as
    software. Default list may be empty (entries redundant with genre
    hints get removed); test the path itself by temporarily injecting
    a fake appid."""
    import app.game_type as gt
    original = gt.SOFTWARE_APPID_LIST
    try:
        gt.SOFTWARE_APPID_LIST = (99999999,)
        assert classify_game(_game(appid=99999999, genres="Action")) == GAME_TYPE_SOFTWARE
    finally:
        gt.SOFTWARE_APPID_LIST = original


def test_software_via_app_type_non_game():
    """app_type values like "advertising"/"music"/"video" indicate non-game."""
    for at in ("advertising", "music", "video", "tool"):
        assert classify_game(_game(app_type=at)) == GAME_TYPE_SOFTWARE, f"failed for {at}"


def test_software_via_genre_hint():
    """Steam genres like 'Utilities' / 'Animation & Modeling' → software."""
    for genre in ("Utilities", "Animation & Modeling", "Audio Production",
                  "Design & Illustration", "Education", "Web Publishing",
                  "Software Training", "Photo Editing", "Video Production"):
        assert classify_game(_game(genres=genre)) == GAME_TYPE_SOFTWARE, f"failed for {genre}"


def test_software_overrides_dlc():
    """Software detection runs before expansion — utility-DLC stays software."""
    # Genre hint path; doesn't depend on the (possibly empty) curated list.
    g = _game(app_type="dlc", genres="Utilities")
    assert classify_game(g) == GAME_TYPE_SOFTWARE


# ---------------------------------------------------------------------------
# Expansion (rule 3)
# ---------------------------------------------------------------------------

def test_expansion_via_dlc_app_type():
    g = _game(app_type="dlc", genres="Action")
    assert classify_game(g) == GAME_TYPE_EXPANSION


# ---------------------------------------------------------------------------
# Early access (rule 4)
# ---------------------------------------------------------------------------

def test_early_access_via_genre():
    g = _game(genres="Action,Early Access")
    assert classify_game(g) == GAME_TYPE_EARLY_ACCESS
    # Case-insensitive
    g = _game(genres="early access,RPG")
    assert classify_game(g) == GAME_TYPE_EARLY_ACCESS


def test_early_access_via_coming_soon_with_playtime():
    g = _game(playtime=42)
    assert classify_game(g, coming_soon=True) == GAME_TYPE_EARLY_ACCESS


def test_no_early_access_when_coming_soon_but_no_playtime():
    g = _game(playtime=0)
    # coming_soon alone is NOT enough — falls through to linear
    assert classify_game(g, coming_soon=True) == GAME_TYPE_LINEAR


# ---------------------------------------------------------------------------
# MMO (rule 5)
# ---------------------------------------------------------------------------

def test_mmo_via_user_tag():
    g = _game(user_tags="MMORPG,Fantasy")
    assert classify_game(g) == GAME_TYPE_MMO
    g = _game(user_tags="Massively Multiplayer,Action")
    assert classify_game(g) == GAME_TYPE_MMO


def test_mmo_via_steam_category():
    g = _game(categories="Single-player,MMO")
    assert classify_game(g) == GAME_TYPE_MMO


# ---------------------------------------------------------------------------
# Multiplayer / Mixed / Sandbox / No_endpoint / Linear
# ---------------------------------------------------------------------------

def test_multiplayer_only():
    g = _game(categories="Multi-player,Online PvP", hltb_main=None)
    assert classify_game(g) == GAME_TYPE_MULTIPLAYER


def test_mixed_requires_coop_user_tag():
    """sp + HLTB + Co-op user_tag → mixed. Steam categories alone aren't
    enough — the user_tag is what discriminates genuinely-Mixed games
    from Souls-style "single-player with co-op invasions"."""
    g = _game(
        categories="Single-player,Multi-player",
        user_tags="Co-op,FPS,Multiplayer",
        hltb_main=20.0, hltb_compl=40.0,
    )
    assert classify_game(g) == GAME_TYPE_MIXED


def test_mixed_via_online_coop_user_tag():
    """'Online Co-Op' substring also catches the rule (case-insensitive)."""
    g = _game(
        categories="Single-player,Multi-player",
        user_tags="Online Co-Op,Multiplayer,Action",
        hltb_main=20.0, hltb_compl=40.0,
    )
    assert classify_game(g) == GAME_TYPE_MIXED


def test_souls_style_falls_through_to_linear():
    """The motivating regression — Souls-likes carry every Steam Co-op /
    PvP subcategory but never the SteamSpy 'Co-op' user_tag. They land
    in linear under the new rule, matching user intent."""
    g = _game(
        name="Some Souls-like",
        categories="Single-player,Multi-player,Co-op,Online Co-op,PvP,Online PvP",
        user_tags="Souls-like,Difficult,Atmospheric,Lore-Rich,RPG,Multiplayer",
        hltb_main=30.0, hltb_compl=80.0,
    )
    assert classify_game(g) == GAME_TYPE_LINEAR


def test_mixed_falls_through_when_user_tags_empty():
    """Niche indies with sparse SteamSpy data default to linear —
    defensive direction (better to misclassify a rare co-op indie as
    linear than every Souls-like as mixed)."""
    g = _game(
        categories="Single-player,Multi-player,Co-op",
        user_tags="",  # no SteamSpy data
        hltb_main=20.0, hltb_compl=40.0,
    )
    assert classify_game(g) == GAME_TYPE_LINEAR


def test_mixed_requires_hltb_main():
    """Co-op user_tag without HLTB main → falls through to linear or
    unknown depending on other signals."""
    g = _game(
        categories="Single-player,Multi-player",
        user_tags="Co-op,Multiplayer",
        hltb_main=None, hltb_compl=None,
    )
    # Falls through; no HLTB → not mixed. Lands in unknown (no rogue tag,
    # no completionist ratio computable, no openworld+null match).
    assert classify_game(g) == GAME_TYPE_UNKNOWN


def test_sandbox_with_tag_under_threshold():
    """Sandbox tag + HLTB present + completionist:main <= 7x → sandbox."""
    g = _game(user_tags="Sandbox,Farming", hltb_main=50.0, hltb_compl=300.0)  # ratio=6
    assert classify_game(g) == GAME_TYPE_SANDBOX


def test_sandbox_at_exact_7x_threshold_inclusive():
    """Sandbox-tagged at exactly 7x ratio still classifies as sandbox
    (the <= boundary, not <)."""
    g = _game(user_tags="Sandbox", hltb_main=10.0, hltb_compl=70.0)  # ratio=7.0
    assert classify_game(g) == GAME_TYPE_SANDBOX


def test_sandbox_above_threshold_falls_to_no_endpoint():
    """Sandbox tag but completionist > 7x main → no_endpoint takes over."""
    g = _game(user_tags="Sandbox", hltb_main=10.0, hltb_compl=80.0)  # ratio=8
    assert classify_game(g) == GAME_TYPE_NO_ENDPOINT


def test_no_endpoint_via_roguelike_tag():
    for tag in ("Roguelike", "Rogue-like", "Rogue-lite", "Action Roguelike"):
        g = _game(user_tags=tag, hltb_main=10.0, hltb_compl=15.0)
        assert classify_game(g) == GAME_TYPE_NO_ENDPOINT, f"failed for {tag}"


def test_no_endpoint_via_high_completionist_ratio():
    g = _game(hltb_main=20.0, hltb_compl=200.0)  # ratio=10
    assert classify_game(g) == GAME_TYPE_NO_ENDPOINT


def test_no_endpoint_when_hltb_null_with_open_world_tag():
    g = _game(user_tags="Open World", hltb_main=None, hltb_compl=None)
    assert classify_game(g) == GAME_TYPE_NO_ENDPOINT


def test_linear_default():
    g = _game(genres="Action,RPG", categories="Single-player",
              hltb_main=30.0, hltb_compl=80.0, user_tags="Story Rich")
    assert classify_game(g) == GAME_TYPE_LINEAR


def test_unknown_when_no_signals():
    """Single-player with no HLTB and no triggering tags → unknown."""
    g = _game(genres="Action", categories="Single-player",
              hltb_main=None, hltb_compl=None, user_tags="Indie")
    assert classify_game(g) == GAME_TYPE_UNKNOWN


# ---------------------------------------------------------------------------
# is_forever_game derivation
# ---------------------------------------------------------------------------

def test_is_forever_uses_cached_game_type():
    """Returns True for {multiplayer, mmo, no_endpoint, sandbox} only."""
    for forever_type in (GAME_TYPE_MULTIPLAYER, GAME_TYPE_MMO,
                         GAME_TYPE_NO_ENDPOINT, GAME_TYPE_SANDBOX):
        g = _game()
        g.game_type = forever_type
        assert is_forever_game(g) is True, f"failed for {forever_type}"

    for finite_type in (GAME_TYPE_LINEAR, GAME_TYPE_MIXED,
                        GAME_TYPE_BETA_PLAYTEST, GAME_TYPE_EARLY_ACCESS,
                        GAME_TYPE_EXPANSION, GAME_TYPE_SOFTWARE,
                        GAME_TYPE_UNKNOWN):
        g = _game()
        g.game_type = finite_type
        assert is_forever_game(g) is False, f"failed for {finite_type}"


def test_is_forever_falls_back_to_classify_when_null():
    """Pre-refresh games have game_type=None — recompute on the fly."""
    g = _game(categories="Multi-player", hltb_main=None)  # would classify as multiplayer
    g.game_type = None
    assert is_forever_game(g) is True

    g = _game()  # default linear
    g.game_type = None
    assert is_forever_game(g) is False


# ---------------------------------------------------------------------------
# resolve_type
# ---------------------------------------------------------------------------

def test_resolve_type_uses_cached():
    g = _game()
    g.game_type = GAME_TYPE_LINEAR
    assert resolve_type(g) == GAME_TYPE_LINEAR


def test_resolve_type_recomputes_when_null():
    g = _game(categories="Multi-player", hltb_main=None)
    g.game_type = None
    assert resolve_type(g) == GAME_TYPE_MULTIPLAYER


# ---------------------------------------------------------------------------
# Display rules
# ---------------------------------------------------------------------------

def test_display_rules_software_hides_everything():
    rules = engagement_display_rules(GAME_TYPE_SOFTWARE)
    assert rules["show_completion_rate"] is False
    assert rules["show_cliff_metric"] is False
    assert rules["show_review_playtime"] is False
    assert rules["show_stickiness_ratio"] is False
    assert rules["show_playtime_ratio"] is False
    assert rules["categorical_badge_eligible"] is False
    assert rules["caveat_text"] is not None


def test_display_rules_linear_shows_all():
    rules = engagement_display_rules(GAME_TYPE_LINEAR)
    assert all(rules[k] for k in (
        "show_completion_rate", "show_cliff_metric", "show_review_playtime",
        "show_stickiness_ratio", "show_playtime_ratio", "categorical_badge_eligible",
    ))
    assert rules["caveat_text"] is None


def test_display_rules_multiplayer_hides_completion_metrics():
    """Achievement metrics don't apply to mp-focus games."""
    rules = engagement_display_rules(GAME_TYPE_MULTIPLAYER)
    assert rules["show_completion_rate"] is False
    assert rules["show_cliff_metric"] is False
    # But review/stickiness/ratio still apply.
    assert rules["show_review_playtime"] is True
    assert rules["show_stickiness_ratio"] is True


def test_display_rules_beta_includes_caveat():
    rules = engagement_display_rules(GAME_TYPE_BETA_PLAYTEST)
    assert rules["caveat_text"] is not None
    assert "beta" in rules["caveat_text"].lower() or "playtest" in rules["caveat_text"].lower()
    # Achievement metrics shown but with caveat.
    assert rules["show_completion_rate"] is True


def test_display_rules_unknown_fallback():
    rules = engagement_display_rules("not_a_real_type")
    # Falls back to unknown's ruleset.
    assert rules["caveat_text"] is not None


def test_display_rules_none_input_falls_back():
    rules = engagement_display_rules(None)
    assert rules["caveat_text"] is not None


def test_display_rules_all_eleven_types_have_entries():
    """Every defined type has a display-rules entry — guard against
    forgetting to add a new type to the table."""
    for gt in ALL_GAME_TYPES:
        rules = engagement_display_rules(gt)
        # Returns a dict with all 7 expected keys.
        for k in ("show_completion_rate", "show_cliff_metric",
                  "show_review_playtime", "show_stickiness_ratio",
                  "show_playtime_ratio", "categorical_badge_eligible",
                  "caveat_text"):
            assert k in rules, f"{gt} missing key {k}"


# ---------------------------------------------------------------------------
# Priority-order regression checks
# ---------------------------------------------------------------------------

def test_software_overrides_mmo():
    """A 'World of MMO Builder' utility shouldn't be MMO."""
    g = _game(name="Some Tool", genres="Utilities", user_tags="MMO")
    assert classify_game(g) == GAME_TYPE_SOFTWARE


def test_no_endpoint_via_completionist_overrides_sandbox_at_above_threshold():
    """Sandbox tag + ratio > 7x → no_endpoint, not sandbox."""
    g = _game(user_tags="Sandbox,Open World", hltb_main=10.0, hltb_compl=100.0)
    assert classify_game(g) == GAME_TYPE_NO_ENDPOINT


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_beta_keyword_in_title,
    test_beta_overrides_multiplayer,
    test_software_via_curated_appid,
    test_software_via_app_type_non_game,
    test_software_via_genre_hint,
    test_software_overrides_dlc,
    test_expansion_via_dlc_app_type,
    test_early_access_via_genre,
    test_early_access_via_coming_soon_with_playtime,
    test_no_early_access_when_coming_soon_but_no_playtime,
    test_mmo_via_user_tag,
    test_mmo_via_steam_category,
    test_multiplayer_only,
    test_mixed_requires_coop_user_tag,
    test_mixed_via_online_coop_user_tag,
    test_souls_style_falls_through_to_linear,
    test_mixed_falls_through_when_user_tags_empty,
    test_mixed_requires_hltb_main,
    test_sandbox_with_tag_under_threshold,
    test_sandbox_at_exact_7x_threshold_inclusive,
    test_sandbox_above_threshold_falls_to_no_endpoint,
    test_no_endpoint_via_roguelike_tag,
    test_no_endpoint_via_high_completionist_ratio,
    test_no_endpoint_when_hltb_null_with_open_world_tag,
    test_linear_default,
    test_unknown_when_no_signals,
    test_is_forever_uses_cached_game_type,
    test_is_forever_falls_back_to_classify_when_null,
    test_resolve_type_uses_cached,
    test_resolve_type_recomputes_when_null,
    test_display_rules_software_hides_everything,
    test_display_rules_linear_shows_all,
    test_display_rules_multiplayer_hides_completion_metrics,
    test_display_rules_beta_includes_caveat,
    test_display_rules_unknown_fallback,
    test_display_rules_none_input_falls_back,
    test_display_rules_all_eleven_types_have_entries,
    test_software_overrides_mmo,
    test_no_endpoint_via_completionist_overrides_sandbox_at_above_threshold,
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
