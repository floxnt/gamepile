import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app.game_type import (
    ALL_GAME_TYPES,
    GAME_TYPE_LABELS,
    resolve_type,
)
from app.models import GameWithState
from app.templates_config import templates

router = APIRouter()

_SORT_COLUMNS = [
    "name", "game_type", "tags",
    "hltb_main", "hltb_compl",
    "playtime", "steam_reviews",
    "metacritic", "median_unlock", "user_achievement",
]

_NULL_HIGH = float("inf")
_NULL_LOW = float("-inf")

_HLTB_MAIN_BUCKETS: list[tuple[str, str, float, float]] = [
    ("lt5",    "<5h",     0,   5),
    ("5-10",   "5–10h",   5,  10),
    ("10-20",  "10–20h", 10,  20),
    ("20-30",  "20–30h", 20,  30),
    ("30-40",  "30–40h", 30,  40),
    ("40-60",  "40–60h", 40,  60),
    ("60-100", "60–100h", 60, 100),
    ("100p",   "100h+",  100, _NULL_HIGH),
]

_HLTB_COMPL_BUCKETS: list[tuple[str, str, float, float]] = [
    ("lt15",    "<15h",     0,  15),
    ("15-30",   "15–30h",  15,  30),
    ("30-60",   "30–60h",  30,  60),
    ("60-100",  "60–100h", 60, 100),
    ("100-150", "100–150h", 100, 150),
    ("150p",    "150h+",   150, _NULL_HIGH),
]

_GAME_TYPE_OPTIONS = [(t, GAME_TYPE_LABELS[t]) for t in ALL_GAME_TYPES]


def _sort_games(games: list[GameWithState], sort: str, direction: str) -> list[GameWithState]:
    if sort not in _SORT_COLUMNS:
        sort = "name"

    reverse = direction == "desc"

    def null_last(val):
        if val is None:
            return _NULL_LOW if reverse else _NULL_HIGH
        return val

    def null_last_str(val):
        if val is None or val == "":
            return "￿" if not reverse else ""
        return val.lower()

    def key(gws: GameWithState):
        game = gws.game
        if sort == "name":
            return game.name.lower()
        if sort == "game_type":
            return resolve_type(game)
        if sort == "tags":
            tags = game.user_tags_list()
            return null_last_str(tags[0] if tags else None)
        if sort == "playtime":
            return null_last(game.playtime_minutes)
        if sort == "hltb_main":
            return null_last(game.hltb_main_hours)
        if sort == "hltb_compl":
            return null_last(game.hltb_completionist_hours)
        if sort == "metacritic":
            return null_last(game.metacritic_score)
        if sort == "steam_reviews":
            return null_last(game.steam_review_pct)
        if sort == "median_unlock":
            return null_last(game.median_achievement_unlock_pct)
        if sort == "user_achievement":
            return null_last(game.user_achievement_pct)
        return game.name.lower()

    return sorted(games, key=key, reverse=reverse)


_COLUMN_LABELS: list[tuple[str, str]] = [
    ("name",             "Title"),
    ("game_type",        "Type"),
    ("tags",             "Tags"),
    ("hltb_main",        "HLTB Main"),
    ("hltb_compl",       "HLTB Completionist"),
    ("playtime",         "Playtime"),
    ("steam_reviews",    "Steam Reviews"),
    ("metacritic",       "Metacritic"),
    ("median_unlock",    "Avg. Achievement %"),
    ("user_achievement", "My Achievement %"),
]


def _build_sort_headers(
    current_sort: str,
    current_dir: str,
    filter_params: dict,
) -> dict:
    effective_sort = current_sort or "name"
    effective_dir = current_dir if current_sort else "asc"

    headers = {}
    for col_key, label in _COLUMN_LABELS:
        if effective_sort == col_key:
            if effective_dir == "asc":
                next_sort, next_dir, arrow = col_key, "desc", "↑"
            else:
                next_sort, next_dir, arrow = "", "asc", "↓"
        else:
            next_sort, next_dir, arrow = col_key, "asc", None

        params = {**filter_params, "sort": next_sort, "dir": next_dir}
        params = {k: v for k, v in params.items() if v not in ("", False, None)}
        qs = urllib.parse.urlencode(params)

        headers[col_key] = {
            "label":  label,
            "arrow":  arrow,
            "active": effective_sort == col_key,
            "qs":     qs,
        }

    return headers


def _bucket_range(bucket_key: str, buckets: list) -> tuple[float, float] | None:
    for key, _label, lo, hi in buckets:
        if key == bucket_key:
            return (lo, hi)
    return None


def _apply_filters(
    games: list[GameWithState],
    tags: list[str],
    hltb_main: str,
    hltb_compl: str,
    game_type_filter: str,
) -> list[GameWithState]:
    if tags:
        tag_set = {t.lower() for t in tags}
        games = [
            g for g in games
            if any(t.lower() in tag_set for t in g.game.user_tags_list())
        ]

    rng = _bucket_range(hltb_main, _HLTB_MAIN_BUCKETS)
    if rng:
        lo, hi = rng
        games = [
            g for g in games
            if g.game.hltb_main_hours is not None
            and lo <= g.game.hltb_main_hours < hi
        ]

    rng = _bucket_range(hltb_compl, _HLTB_COMPL_BUCKETS)
    if rng:
        lo, hi = rng
        games = [
            g for g in games
            if g.game.hltb_completionist_hours is not None
            and lo <= g.game.hltb_completionist_hours < hi
        ]

    if game_type_filter:
        games = [
            g for g in games
            if resolve_type(g.game) == game_type_filter
        ]

    return games


def _collect_tags(games: list[GameWithState]) -> list[str]:
    tags: set[str] = set()
    for gws in games:
        for t in gws.game.user_tags_list():
            tags.add(t)
    return sorted(tags, key=str.lower)


def _parse_tags(raw: str) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _filter_params_dict(
    tags: str,
    show_removed: bool,
    hltb_main: str,
    hltb_compl: str,
    game_type_filter: str,
) -> dict:
    return {
        "tags": tags,
        "show_removed": "true" if show_removed else "",
        "hltb_main": hltb_main,
        "hltb_compl": hltb_compl,
        "game_type": game_type_filter,
    }


def _active_filter_count(
    tags: list[str],
    show_removed: bool,
    hltb_main: str,
    hltb_compl: str,
    game_type_filter: str,
) -> int:
    c = 0
    if tags:
        c += 1
    if show_removed:
        c += 1
    if hltb_main:
        c += 1
    if hltb_compl:
        c += 1
    if game_type_filter:
        c += 1
    return c


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    tags: str = "",
    show_removed: bool = False,
    hltb_main: str = "",
    hltb_compl: str = "",
    game_type: str = "",
    sort: str = "",
    dir: str = "asc",
):
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn, active_only=not show_removed)

    all_tags = _collect_tags(all_games)
    tag_list = _parse_tags(tags)

    games = _apply_filters(all_games, tag_list, hltb_main, hltb_compl, game_type)
    games = _sort_games(games, sort, dir)

    fp = _filter_params_dict(tags, show_removed, hltb_main, hltb_compl, game_type)
    sort_headers = _build_sort_headers(sort, dir, fp)

    return templates.TemplateResponse(request, "library.html", {
        "games": games,
        "all_tags": all_tags,
        "active_tags": tag_list,
        "tags_csv": tags,
        "show_removed": show_removed,
        "hltb_main": hltb_main,
        "hltb_compl": hltb_compl,
        "game_type_filter": game_type,
        "sort": sort,
        "dir": dir,
        "sort_headers": sort_headers,
        "hltb_main_buckets": _HLTB_MAIN_BUCKETS,
        "hltb_compl_buckets": _HLTB_COMPL_BUCKETS,
        "game_type_options": _GAME_TYPE_OPTIONS,
        "filter_count": _active_filter_count(
            tag_list, show_removed, hltb_main, hltb_compl, game_type,
        ),
    })
