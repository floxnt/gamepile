from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app import database as db
from app.recommender import RecommendMode, RecommendRequest, recommend

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
async def pick_page(
    request: Request,
    minutes: int = 90,
    mode: str = "short_term",
    include_unplayed: bool = True,
    include_in_progress: bool = True,
):
    req = RecommendRequest(
        minutes=minutes,
        mode=RecommendMode(mode),
        include_unplayed=include_unplayed,
        include_in_progress=include_in_progress,
    )

    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn)

    picks = recommend(all_games, req)

    return templates.TemplateResponse(request, "pick.html", {
        "picks": picks,
        "minutes": minutes,
        "mode": mode,
        "include_unplayed": include_unplayed,
        "include_in_progress": include_in_progress,
    })


@router.post("/games/{appid}/pick", response_class=HTMLResponse)
async def mark_picked(request: Request, appid: int):
    """'I picked this' — sets status to in_progress, returns confirmation card."""
    from app.models import GameStatus
    with db.get_db() as conn:
        db.update_game_state(conn, appid, status=GameStatus.in_progress)

    return templates.TemplateResponse(request, "partials/game_card_confirm.html", {
        "appid": appid,
    })


@router.post("/games/{appid}/state", response_class=HTMLResponse)
async def update_state_from_card(request: Request, appid: int):
    """
    Used by 'Not feeling it' on the pick page.
    hx-vals sends JSON; reads status from request body.
    Returns an empty placeholder so the card collapses.
    """
    from app.models import GameStatus
    body = await request.json()
    status = GameStatus(body.get("status", "not_interested"))
    with db.get_db() as conn:
        db.update_game_state(conn, appid, status=status)

    return HTMLResponse(f'<div id="card-{appid}" class="game-card game-card--dismissed"></div>')
