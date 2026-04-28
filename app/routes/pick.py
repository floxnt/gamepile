from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app.recommender import RecommendMode, RecommendRequest, recommend
from app.templates_config import templates

router = APIRouter()


def _bool_param(request: Request, name: str, default: bool = True) -> bool:
    """
    Read a boolean query param that may appear twice (hidden + checkbox pattern).
    Starlette's QueryParams._dict uses the last duplicate value, which is what
    we want: hidden input submits "false" first, checked checkbox submits "true"
    second → last value is "true". Unchecked: only hidden submits "false".
    """
    raw = request.query_params.get(name)
    if raw is None:
        return default
    return raw.lower() not in ("false", "0", "no")


def _parse_excluded(request: Request) -> frozenset:
    """Parse ?excluded=1234,5678 into a frozenset of ints."""
    raw = request.query_params.get("excluded", "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)


@router.get("/", response_class=HTMLResponse)
async def pick_page(
    request: Request,
    minutes: int = 90,
    mode: str = "both",
):
    include_unplayed = _bool_param(request, "include_unplayed", default=True)
    include_in_progress = _bool_param(request, "include_in_progress", default=True)
    excluded_ids = _parse_excluded(request)

    req = RecommendRequest(
        minutes=minutes,
        mode=RecommendMode(mode),
        include_unplayed=include_unplayed,
        include_in_progress=include_in_progress,
        excluded_ids=excluded_ids,
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
        "has_exclusions": bool(excluded_ids),
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
