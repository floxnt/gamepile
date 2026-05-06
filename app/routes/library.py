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
# played_unclassified sits between never_played and finished — has history but uncurated.
_STATUS_SORT_ORDER = {
    "in_progress":         0,
    "never_played":        1,
    "played_unclassified": 2,
    "finished":            3,
    "dropped":             4,
    "not_interested":      5,
}

_SORT_COLUMNS = [
    "name", "status", "game_type", "stickiness", "developer",
    "hltb_main", "hltb_compl",
    "playtime", "steam_pct", "steam_reviews",
    "metacritic",
]

# Stickiness sort order: Sticky first (most engagement), Average,
# Filters players hard, then Insufficient data sinks to the bottom
# regardless of direction (matches the null-last pattern of other
# columns). Higher number = lower priority in ascending sort.
_STICKINESS_SORT_ORDER = {
    "sticky":            0,
    "average":           1,
    "filters_hard":      2,
    "insufficient_data": 3,
}

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
        game, state = gws.game, gws.state
        if sort == "name":
            return game.name.lower()
        if sort == "status":
            return _STATUS_SORT_ORDER.get(state.status.value, 99)
        if sort == "game_type":
            from app.backlog import compute_game_type
            return compute_game_type(game)
        if sort == "stickiness":
            # Insufficient data sinks last regardless of direction. Other
            # values use the explicit priority order above.
            from app.hook_metrics import compute_stickiness_signal
            badge = compute_stickiness_signal(game)[0]
            rank = _STICKINESS_SORT_ORDER.get(badge, 99)
            if badge == "insufficient_data":
                return _NULL_LOW if reverse else _NULL_HIGH
            return rank
        if sort == "developer":
            return null_last_str(game.developer)
        if sort == "playtime":
            return null_last(game.playtime_minutes)
        if sort == "hltb_main":
            return null_last(game.hltb_main_hours)
        if sort == "hltb_compl":
            return null_last(game.hltb_completionist_hours)
        if sort == "metacritic":
            return null_last(game.metacritic_score)
        if sort == "steam_pct":
            return null_last(game.steam_review_pct)
        if sort == "steam_reviews":
            return null_last(game.steam_review_count)
        return game.name.lower()

    return sorted(games, key=key, reverse=reverse)


_COLUMN_LABELS: list[tuple[str, str]] = [
    ("name",          "Title"),
    ("status",        "Status"),
    ("game_type",     "Type"),
    ("stickiness",    "Stickiness"),
    ("developer",     "Developer"),
    ("hltb_main",     "HLTB Main"),
    ("hltb_compl",    "HLTB Compl."),
    ("playtime",      "Playtime"),
    ("steam_pct",     "Steam %"),
    ("steam_reviews", "Steam Reviews"),
    ("metacritic",    "Metacritic"),
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
    status_filter: str,
    tag_filter: str,
    sort: str,
    direction: str,
) -> list[GameWithState]:
    if status_filter:
        all_games = [g for g in all_games if g.state.status.value == status_filter]
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
    status_filter: str = "",
    tag_filter: str = "",
    show_removed: bool = False,
    sort: str = "",
    dir: str = "asc",
):
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn, active_only=not show_removed)

    tags = _collect_tags(all_games)

    games = _apply_filters_and_sort(all_games, status_filter, tag_filter, sort, dir)

    filter_params = {
        "status_filter": status_filter,
        "tag_filter": tag_filter,
        "show_removed": "true" if show_removed else "",
    }
    sort_headers = _build_sort_headers(sort, dir, filter_params)

    return templates.TemplateResponse(request, "library.html", {
        "games": games,
        "all_statuses": _ALL_STATUSES,
        "tags": tags,
        "status_filter": status_filter,
        "tag_filter": tag_filter,
        "show_removed": show_removed,
        "sort": sort,
        "dir": dir,
        "sort_headers": sort_headers,
    })


@router.get("/library/rows", response_class=HTMLResponse)
async def library_rows(
    request: Request,
    status_filter: str = "",
    tag_filter: str = "",
    show_removed: bool = False,
    sort: str = "",
    dir: str = "asc",
):
    """Partial used by HTMX header-click sort — returns only the <tr> rows."""
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn, active_only=not show_removed)

    games = _apply_filters_and_sort(all_games, status_filter, tag_filter, sort, dir)

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
