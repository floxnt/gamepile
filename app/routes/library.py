import urllib.parse
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app.models import GameStatus, GameWithState
from app.templates_config import templates

router = APIRouter()

# GameStatus values are still passed to the inline edit form (the user
# can change a game's status via the Edit button), but Status is no
# longer a displayed Library column or filter (removed in v0.8.7 per
# test-group feedback that the in-library badge was inaccurate and
# double-tracked with Shortlist's status display).
_ALL_STATUSES = [s.value for s in GameStatus]

_SORT_COLUMNS = [
    "name", "game_type", "tags",
    "hltb_main", "hltb_compl",
    "playtime", "steam_reviews",
    "metacritic", "median_unlock", "user_achievement",
]

# Sentinel values for nulls-last regardless of sort direction.
_NULL_HIGH = float("inf")
_NULL_LOW = float("-inf")


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
            from app.backlog import compute_game_type
            return compute_game_type(game)
        if sort == "tags":
            # Tags column sortable as of v0.8.7. Sort by the first tag
            # (most natural reading order — matches what the user sees
            # in the cell when only the first few render). Games with
            # no tags sort last via the existing nulls-last sentinel.
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
            # Single combined Steam Reviews column as of v0.8.7. Sort by
            # percent (the more meaningful axis); review count appears in
            # parentheses for context but is not the primary sort key.
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
    # "Avg. Achievement %" is the user-facing label for the v0.7.0
    # median-of-per-achievement-global-unlock-% column. The label was
    # corrected from "Median unlock %" in v0.8.7 — the v0.7.0 lock was
    # about COMPUTATION (median wins for robustness against right-skewed
    # distributions), not terminology. The internal sort key stays
    # "median_unlock" for URL backward-compatibility with bookmarked
    # sort links.
    ("median_unlock",    "Avg. Achievement %"),
    ("user_achievement", "My Achievement %"),
]


def _build_sort_headers(
    current_sort: str,
    current_dir: str,
    filter_params: dict,
) -> dict:
    """
    Returns a dict keyed by column ID. Each value has the info the template
    needs to render a sortable column header.

    Empty sort param renders as the default sort (Title asc) so the arrow
    is always visible somewhere. Cycle for the default column is asc ↔ desc;
    cycle for every other column is asc → desc → default (Title asc).
    """
    effective_sort = current_sort or "name"
    effective_dir = current_dir if current_sort else "asc"

    headers = {}
    for col_key, label in _COLUMN_LABELS:
        if effective_sort == col_key:
            if effective_dir == "asc":
                next_sort, next_dir, arrow = col_key, "desc", "↑"
            else:
                # Reset back to the default sort (Title asc).
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


def _apply_filters_and_sort(
    all_games: list[GameWithState],
    tag_filter: str,
    sort: str,
    direction: str,
) -> list[GameWithState]:
    if tag_filter:
        needle = tag_filter.lower()
        all_games = [
            g for g in all_games
            if any(t.lower() == needle for t in g.game.user_tags_list())
        ]
    return _sort_games(all_games, sort, direction)


def _collect_tags(games: list[GameWithState]) -> list[str]:
    tags: set[str] = set()
    for gws in games:
        for t in gws.game.user_tags_list():
            tags.add(t)
    return sorted(tags, key=str.lower)


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    tag_filter: str = "",
    show_removed: bool = False,
    sort: str = "",
    dir: str = "asc",
):
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn, active_only=not show_removed)

    tags = _collect_tags(all_games)

    games = _apply_filters_and_sort(all_games, tag_filter, sort, dir)

    filter_params = {
        "tag_filter": tag_filter,
        "show_removed": "true" if show_removed else "",
    }
    sort_headers = _build_sort_headers(sort, dir, filter_params)

    return templates.TemplateResponse(request, "library.html", {
        "games": games,
        "all_statuses": _ALL_STATUSES,
        "tags": tags,
        "tag_filter": tag_filter,
        "show_removed": show_removed,
        "sort": sort,
        "dir": dir,
        "sort_headers": sort_headers,
    })


@router.get("/library/rows", response_class=HTMLResponse)
async def library_rows(
    request: Request,
    tag_filter: str = "",
    show_removed: bool = False,
    sort: str = "",
    dir: str = "asc",
):
    """Partial used by HTMX header-click sort — returns only the <tr> rows."""
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn, active_only=not show_removed)

    games = _apply_filters_and_sort(all_games, tag_filter, sort, dir)

    return templates.TemplateResponse(request, "partials/library_rows.html", {
        "games": games,
        "all_statuses": _ALL_STATUSES,
    })


@router.post("/games/{appid}/state", response_class=HTMLResponse)
async def update_state(
    request: Request,
    appid: int,
    status: str = Form(...),
    hours_played_manual: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    hours: Optional[float] = None
    if hours_played_manual and hours_played_manual.strip():
        try:
            hours = float(hours_played_manual)
        except ValueError:
            pass

    with db.get_db() as conn:
        db.update_game_state(
            conn,
            appid,
            status=GameStatus(status),
            hours_played_manual=hours,
            notes=notes or None,
            manually_set=True,
        )
        all_games = db.get_games_with_state(conn)

    gws = next((g for g in all_games if g.game.appid == appid), None)
    if gws is None:
        return HTMLResponse("Not found", status_code=404)

    return templates.TemplateResponse(request, "partials/library_row.html", {
        "gws": gws,
        "all_statuses": _ALL_STATUSES,
    })


@router.get("/games/{appid}/edit", response_class=HTMLResponse)
async def edit_row(request: Request, appid: int):
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn)

    gws = next((g for g in all_games if g.game.appid == appid), None)
    if gws is None:
        return HTMLResponse("Not found", status_code=404)

    return templates.TemplateResponse(request, "partials/library_row_edit.html", {
        "gws": gws,
        "all_statuses": _ALL_STATUSES,
    })


@router.get("/games/{appid}/cancel-edit", response_class=HTMLResponse)
async def cancel_edit(request: Request, appid: int):
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn)

    gws = next((g for g in all_games if g.game.appid == appid), None)
    if gws is None:
        return HTMLResponse("Not found", status_code=404)

    return templates.TemplateResponse(request, "partials/library_row.html", {
        "gws": gws,
        "all_statuses": _ALL_STATUSES,
    })
