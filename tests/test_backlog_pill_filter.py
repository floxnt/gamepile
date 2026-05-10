"""Backlog pill-driven filter (v3.5 polish — Dashboard click-pill-to-filter).

Run with: uv run python tests/test_backlog_pill_filter.py

Covers:
  - parse_backlog_query reads ?genre, ?tag, ?developer (single-value)
  - Existing chip-tag rename ?tag → ?tag_chip works via getlist
  - Each pill filter dimension matches case-insensitively
  - Filter intersects with existing bucket structure (game still gets
    bucketed normally) and with chip filters (both apply, AND semantics)
  - is_empty_pill_only flag fires only when pill is the SOLE filter
  - pill_kind_value precedence (genre > tag > developer when multiple set)

No pytest dependency.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from starlette.datastructures import QueryParams

from app.backlog import (
    BacklogFilters,
    _passes_developer,
    _passes_genre,
    _passes_tag_pill,
    build_backlog_view,
    parse_backlog_query,
)
from app.models import GameStatus


def _gws(*, name: str, status: str = "never_played",
         genres: str = "", developer: str = None,
         user_tags: str = "",
         hltb_main_hours: float = 20.0,
         playtime_minutes: int = 0,
         dropped_strength: str = None) -> SimpleNamespace:
    g = SimpleNamespace(
        appid=hash(name) & 0xFFFFFF,
        name=name,
        playtime_minutes=playtime_minutes,
        last_played_steam=None,
        installed=None,
        hltb_main_hours=hltb_main_hours,
        hltb_main_extra_hours=None,
        hltb_completionist_hours=None,
        genres=genres,
        tags="",
        user_tags=user_tags,
        developer=developer,
        publisher=None,
        metacritic_score=None,
        opencritic_score=None,
        steam_review_pct=None,
        steam_review_count=None,
        last_refreshed=datetime(2026, 5, 1),
        is_active=True,
        release_date=None,
        description=None,
        completion_rate=None,
        completion_rate_confidence=None,
        completion_achievement_name_manual=None,
        cliff_metric=None,
        cliff_position=None,
        review_playtime_median=None,
        stickiness_ratio=None,
        playtime_median_avg_ratio=None,
        game_type="linear",
        game_type_manual=False,
        app_type=None,
        hltb_id_manual=None,
        stickiness_badge_manual=None,
    )
    g.user_tags_list = lambda: [t.strip() for t in g.user_tags.split(",") if t.strip()]
    g.tag_list = lambda: []
    g.genre_list = lambda: [t.strip() for t in g.genres.split(",") if t.strip()]
    state = SimpleNamespace(
        appid=g.appid,
        status=GameStatus(status),
        hours_played_manual=None,
        notes=None,
        updated_at=datetime(2026, 5, 1),
        manually_set=False,
        has_technical_issue=False,
        blacklisted=False,
        dropped_strength=dropped_strength,
        pinned_for_shortlist=False,
        pinned_at=None,
        personal_rating=None,
    )
    return SimpleNamespace(game=g, state=state)


# ---------------------------------------------------------------------------
# parse_backlog_query — new pill params + chip rename
# ---------------------------------------------------------------------------

def test_parse_extracts_genre():
    f = parse_backlog_query(QueryParams("genre=Action"))
    assert f.genre == "Action"
    assert f.tag_pill == ""
    assert f.developer == ""


def test_parse_extracts_tag_pill():
    f = parse_backlog_query(QueryParams("tag=Co-op"))
    assert f.tag_pill == "Co-op"
    assert f.genre == ""


def test_parse_extracts_developer():
    f = parse_backlog_query(QueryParams("developer=FromSoftware"))
    assert f.developer == "FromSoftware"


def test_parse_strips_whitespace():
    f = parse_backlog_query(QueryParams("genre=  Action  "))
    assert f.genre == "Action"


def test_parse_empty_pill_param_is_unset():
    f = parse_backlog_query(QueryParams("genre=&tag=&developer="))
    assert f.genre == ""
    assert f.tag_pill == ""
    assert f.developer == ""
    assert not f.pill_active()


def test_parse_chip_rename_tag_chip_works():
    # Rename: chip filter is now ?tag_chip= (multi-value), not ?tag=.
    f = parse_backlog_query(QueryParams("tag_chip=Indie&tag_chip=Roguelike"))
    assert f.tags == frozenset({"indie", "roguelike"})
    # And the pill ?tag= is independent.
    assert f.tag_pill == ""


def test_parse_pill_tag_does_not_leak_into_chip():
    # ?tag=Co-op is the pill (single-value via qp.get), NOT the chip
    # (multi-value via qp.getlist). They share neither URL param nor field.
    f = parse_backlog_query(QueryParams("tag=Co-op"))
    assert f.tag_pill == "Co-op"
    assert f.tags == frozenset()  # chip set is unaffected


def test_parse_combined_pill_and_chip_tags():
    f = parse_backlog_query(QueryParams("tag=Co-op&tag_chip=Indie"))
    assert f.tag_pill == "Co-op"
    assert f.tags == frozenset({"indie"})


# ---------------------------------------------------------------------------
# is_active / pill_active / pill_kind_value
# ---------------------------------------------------------------------------

def test_pill_active_only_for_pill_fields():
    f = BacklogFilters(genre="Action")
    assert f.pill_active() is True
    assert f.is_active() is True
    assert f.chip_filters_active() is False


def test_pill_active_false_for_chip_filters():
    f = BacklogFilters(tags=frozenset({"indie"}))
    assert f.pill_active() is False
    assert f.chip_filters_active() is True
    assert f.is_active() is True


def test_pill_kind_value_genre_precedence():
    # Defensive: when somehow multiple pills are set, the precedence is
    # genre > tag > developer. URL flow only ever sets one at a time.
    f = BacklogFilters(genre="Action", tag_pill="Co-op", developer="From")
    assert f.pill_kind_value() == ("genre", "Action")


def test_pill_kind_value_tag_when_no_genre():
    f = BacklogFilters(tag_pill="Co-op", developer="From")
    assert f.pill_kind_value() == ("tag", "Co-op")


def test_pill_kind_value_developer_when_alone():
    f = BacklogFilters(developer="FromSoftware")
    assert f.pill_kind_value() == ("developer", "FromSoftware")


def test_pill_kind_value_empty_when_none_set():
    assert BacklogFilters().pill_kind_value() == (None, None)


# ---------------------------------------------------------------------------
# Per-pill filter helpers — case-insensitive matching
# ---------------------------------------------------------------------------

def test_passes_genre_match():
    g = _gws(name="X", genres="Action, Adventure, Indie")
    assert _passes_genre(g, "Action") is True
    assert _passes_genre(g, "action") is True   # case-insensitive
    assert _passes_genre(g, "ACTION") is True
    assert _passes_genre(g, "Strategy") is False


def test_passes_genre_empty_filter_passes_all():
    g = _gws(name="X", genres="Action")
    assert _passes_genre(g, "") is True
    g2 = _gws(name="Y", genres="")
    assert _passes_genre(g2, "") is True


def test_passes_tag_pill_case_insensitive():
    g = _gws(name="X", user_tags="Co-op,Roguelike,Indie")
    assert _passes_tag_pill(g, "Co-op") is True
    assert _passes_tag_pill(g, "co-op") is True
    assert _passes_tag_pill(g, "roguelike") is True
    assert _passes_tag_pill(g, "Strategy") is False


def test_passes_developer_case_insensitive():
    g = _gws(name="X", developer="FromSoftware")
    assert _passes_developer(g, "FromSoftware") is True
    assert _passes_developer(g, "fromsoftware") is True
    assert _passes_developer(g, "FROMSOFTWARE") is True
    assert _passes_developer(g, "Larian") is False


def test_passes_developer_handles_none():
    g = _gws(name="X", developer=None)
    assert _passes_developer(g, "FromSoftware") is False
    assert _passes_developer(g, "") is True


# ---------------------------------------------------------------------------
# Filter intersects with existing bucketing — game still bucketed normally
# ---------------------------------------------------------------------------

def test_pill_filter_preserves_section_classification():
    # Build view with: 1 in_progress Action game, 1 never_played Action game,
    # 1 never_played Strategy game. Filter to genre=Action.
    games = [
        _gws(name="In Progress Action", status="in_progress",
             genres="Action", playtime_minutes=600),  # 10h of 20h main
        _gws(name="New Action", status="never_played", genres="Action"),
        _gws(name="New Strategy", status="never_played", genres="Strategy"),
    ]
    f = BacklogFilters(genre="Action")
    view = build_backlog_view(games, f, affinities={})
    # Section keys vary with status — verify both Action games surface in
    # appropriate sections, Strategy is filtered out.
    surfaced_names = {g.game.name for sec in view.sections for g in sec.rows}
    assert surfaced_names == {"In Progress Action", "New Action"}
    # Verify multiple sections present (in_progress + never_played) — the
    # filter doesn't collapse the bucket structure.
    section_keys = {s.key for s in view.sections}
    assert len(section_keys) >= 2


def test_pill_filter_intersects_with_chip_filter():
    # Game must pass BOTH the pill and the chip filters.
    games = [
        _gws(name="Action+Indie", genres="Action", user_tags="Indie"),
        _gws(name="Action only", genres="Action", user_tags=""),
        _gws(name="Indie only", genres="Strategy", user_tags="Indie"),
    ]
    f = BacklogFilters(genre="Action", tags=frozenset({"indie"}))
    view = build_backlog_view(games, f, affinities={})
    surfaced = {g.game.name for sec in view.sections for g in sec.rows}
    assert surfaced == {"Action+Indie"}


def test_pill_filter_no_match_returns_empty_sections():
    games = [_gws(name="Strategy", genres="Strategy")]
    f = BacklogFilters(genre="Action")
    view = build_backlog_view(games, f, affinities={})
    assert view.sections == []


# ---------------------------------------------------------------------------
# is_empty_pill_only — drives the targeted empty-state copy
# ---------------------------------------------------------------------------

def test_pill_only_empty_sets_flag():
    games = [_gws(name="Strategy", genres="Strategy")]
    f = BacklogFilters(genre="Action")
    view = build_backlog_view(games, f, affinities={})
    assert view.is_empty_pill_only is True
    assert view.is_empty_due_to_filters is True  # both flags fire; template branches on the more-specific one first
    assert view.pill_filter_kind == "genre"
    assert view.pill_filter_value == "Action"


def test_combined_filter_empty_does_not_set_pill_only_flag():
    # Pill + chip both active, both empty — targeted copy doesn't apply.
    games = [_gws(name="Strategy", genres="Strategy")]
    f = BacklogFilters(genre="Action", tags=frozenset({"indie"}))
    view = build_backlog_view(games, f, affinities={})
    assert view.is_empty_pill_only is False
    assert view.is_empty_due_to_filters is True


def test_pill_match_does_not_set_pill_only_flag():
    # Pill matches → not empty → flag stays False.
    games = [_gws(name="Action Game", genres="Action")]
    f = BacklogFilters(genre="Action")
    view = build_backlog_view(games, f, affinities={})
    assert view.is_empty_pill_only is False
    assert view.sections  # non-empty


def test_no_pill_set_no_pill_only_flag():
    games = [_gws(name="Strategy", genres="Strategy")]
    f = BacklogFilters(tags=frozenset({"action"}))  # chip-only filter, returns empty
    view = build_backlog_view(games, f, affinities={})
    assert view.is_empty_pill_only is False
    assert view.pill_filter_kind is None


def test_truly_empty_backlog_no_pill_only_flag():
    # Backlog has zero games at all — not a filter problem. is_empty_pill_only
    # explicitly requires stats.total_count > 0 (the backlog itself isn't
    # empty), so a genuinely-empty library never triggers the pill-targeted
    # copy even if a pill filter is set in the URL. Falls through to
    # is_empty_no_filters, the existing 'Refresh your library' message.
    f = BacklogFilters(genre="Action")
    view = build_backlog_view([], f, affinities={})
    assert view.is_empty_pill_only is False
    # Template precedence: pill_only > due_to_filters > no_filters. With
    # the backlog empty, no_filters fires regardless of the pill param.
    assert view.is_empty_no_filters is True


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
