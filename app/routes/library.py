from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import database as db
from app.models import GameStatus

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_ALL_STATUSES = [s.value for s in GameStatus]


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    status_filter: str = "",
    genre_filter: str = "",
    show_removed: bool = False,
):
    active_only = not show_removed
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn, active_only=active_only)

    if status_filter:
        all_games = [g for g in all_games if g.state.status.value == status_filter]

    if genre_filter:
        all_games = [
            g for g in all_games
            if genre_filter.lower() in g.game.genres.lower()
        ]

    genres: set[str] = set()
    for gws in all_games:
        for g in gws.game.genre_list():
            genres.add(g)

    return templates.TemplateResponse(request, "library.html", {
        "games": all_games,
        "all_statuses": _ALL_STATUSES,
        "genres": sorted(genres),
        "status_filter": status_filter,
        "genre_filter": genre_filter,
        "show_removed": show_removed,
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
