import urllib.parse
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app.models import GameStatus, GameWithState
from app.templates_config import templates

router = APIRouter()

_ALL_STATUSES = [s.value for s in GameStatus]

# Status sort order: active/in-flight states first, completion/terminal states last.
# played sits between never_played and finished — it's "has history" but not curated.
_STATUS_SORT_ORDER = {
    "in_progress":    0,
    "never_played":   1,
    "played":         2,
    "finished":       3,
    "dropped":        4,
    "not_interested": 5,
}

_SORT_COLUMNS = ["name", "status", "playtime", "hltb_main", "last_played",
                 "metacritic", "opencritic", "steam_pct"]


def _sort_games(games: list[GameWithState], sort: str, direction: str) -> list[GameWithState]:
    if sort not in _SORT_COLUMNS:
        return sorted(games, key=lambda g: g.game.name.lower())

    reverse = direction == "desc"

    # Sentinel values for nulls-last regardless of sort direction.
    NULL_HIGH = float("inf")   # sorts last when ascending
    NULL_LOW  = float("-inf")  # sorts last when descending (reversed)

    def null_last(val):
        if val is None:
            return NULL_LOW if reverse else NULL_HIGH
        return val

    def key(gws: GameWithState):
        game, state = gws.game, gws.state
        if sort == "name":
            return game.name.lower()
        if sort == "status":
            return _STATUS_SORT_ORDER.get(state.status.value, 99)
        if sort == "playtime":
            return null_last(game.playtime_minutes)
        if sort == "hltb_main":
            return null_last(game.hltb_main_hours)
        if sort == "last_played":
            v = game.last_played_steam
            return null_last(v.timestamp() if v is not None else None)
        if sort == "metacritic":
            return null_last(game.metacritic_score)
        if sort == "opencritic":
            return null_last(game.opencritic_score)
        if sort == "steam_pct":
            return null_last(game.steam_review_pct)
        return game.name.lower()

    return sorted(games, key=key, reverse=reverse)


def _build_sort_headers(
    current_sort: str,
    current_dir: str,
    filter_params: dict,
) -> dict:
    """
    Returns a dict keyed by column ID. Each value has the info the template
    needs to render a sortable column header.

    Three-click cycle per column:
      no sort  →  asc  →  desc  →  no sort (back to default name-asc)
    """
    cols = [
        ("name",       "Name"),
        ("status",     "Status"),
        ("playtime",   "Playtime"),
        ("hltb_main",  "HLTB Main"),
        ("last_played","Last Played"),
        ("metacritic", "MC"),
        ("opencritic", "OC"),
        ("steam_pct",  "Steam %"),
    ]

    headers = {}
    for col_key, label in cols:
        if current_sort == col_key:
            if current_dir == "asc":
                next_sort, next_dir, arrow = col_key, "desc", "↑"
            else:
                next_sort, next_dir, arrow = "", "asc", "↓"
        else:
            next_sort, next_dir, arrow = col_key, "asc", None

        params = {**filter_params, "sort": next_sort, "dir": next_dir}
        # Keep the URL clean: drop empty-string params.
        params = {k: v for k, v in params.items() if v not in ("", False, None)}
        qs = urllib.parse.urlencode(params)

        headers[col_key] = {
            "label":   label,
            "arrow":   arrow,
            "active":  current_sort == col_key,
            "qs":      qs,           # query string for both /library and /library/rows
        }

    return headers


def _apply_filters_and_sort(
    all_games: list[GameWithState],
    status_filter: str,
    genre_filter: str,
    sort: str,
    direction: str,
) -> list[GameWithState]:
    if status_filter:
        all_games = [g for g in all_games if g.state.status.value == status_filter]
    if genre_filter:
        all_games = [g for g in all_games if genre_filter.lower() in g.game.genres.lower()]
    return _sort_games(all_games, sort, direction)


def _collect_genres(games: list[GameWithState]) -> list[str]:
    genres: set[str] = set()
    for gws in games:
        for g in gws.game.genre_list():
            genres.add(g)
    return sorted(genres)


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    status_filter: str = "",
    genre_filter: str = "",
    show_removed: bool = False,
    sort: str = "",
    dir: str = "asc",
):
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn, active_only=not show_removed)

    # Collect genres before filtering so the dropdown always shows all options.
    genres = _collect_genres(all_games)

    games = _apply_filters_and_sort(all_games, status_filter, genre_filter, sort, dir)

    filter_params = {
        "status_filter": status_filter,
        "genre_filter": genre_filter,
        "show_removed": "true" if show_removed else "",
    }
    sort_headers = _build_sort_headers(sort, dir, filter_params)

    return templates.TemplateResponse(request, "library.html", {
        "games": games,
        "all_statuses": _ALL_STATUSES,
        "genres": genres,
        "status_filter": status_filter,
        "genre_filter": genre_filter,
        "show_removed": show_removed,
        "sort": sort,
        "dir": dir,
        "sort_headers": sort_headers,
    })


@router.get("/library/rows", response_class=HTMLResponse)
async def library_rows(
    request: Request,
    status_filter: str = "",
    genre_filter: str = "",
    show_removed: bool = False,
    sort: str = "",
    dir: str = "asc",
):
    """Partial used by HTMX header-click sort — returns only the <tr> rows."""
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn, active_only=not show_removed)

    games = _apply_filters_and_sort(all_games, status_filter, genre_filter, sort, dir)

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
