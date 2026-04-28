import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app import prompt_state
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
    raw = request.query_params.get("excluded", "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)


def _normalize_mode(raw: str) -> str:
    """Accept legacy 'surprise' as a synonym for 'surprise_me'."""
    return "surprise_me" if raw == "surprise" else raw


@router.get("/", response_class=HTMLResponse)
async def pick_page(
    request: Request,
    minutes: int = 90,
    mode: str = "both",
):
    mode = _normalize_mode(mode)
    include_unplayed = _bool_param(request, "include_unplayed", default=True)
    include_in_progress = _bool_param(request, "include_in_progress", default=True)
    excluded_ids = _parse_excluded(request)

    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn)
        affinities = db.get_all_affinities(conn)
        pending_raw = db.get_oldest_pending_pick(conn)

    # Filter out picks dismissed for this session.
    pending_pick = None
    if pending_raw and not prompt_state.is_dismissed(pending_raw.id):
        pending_pick = pending_raw

    req = RecommendRequest(
        minutes=minutes,
        mode=RecommendMode(mode),
        include_unplayed=include_unplayed,
        include_in_progress=include_in_progress,
        excluded_ids=excluded_ids,
        affinities=affinities,
    )

    picks = recommend(all_games, req)

    # Pre-serialise all 5 appids for the "I picked this" hx-vals embed.
    picks_appids = json.dumps([p.gws.game.appid for p in picks])

    return templates.TemplateResponse(request, "pick.html", {
        "picks": picks,
        "picks_appids": picks_appids,
        "minutes": minutes,
        "mode": mode,
        "include_unplayed": include_unplayed,
        "include_in_progress": include_in_progress,
        "has_exclusions": bool(excluded_ids),
        "pending_pick": pending_pick,
    })


@router.post("/games/{appid}/pick", response_class=HTMLResponse)
async def mark_picked(request: Request, appid: int):
    """
    'I picked this' — sets status to in_progress and records a pick_history row.
    Expects JSON body from hx-vals: {candidates_at_pick, mode, minutes}.
    """
    from app.models import GameStatus

    try:
        body = await request.json()
    except Exception:
        body = {}

    candidates_at_pick: list[int] = body.get("candidates_at_pick") or []
    mode_str = _normalize_mode(body.get("mode", "both"))
    minutes_val = int(body.get("minutes", 90))
    time_window = None if mode_str == "surprise_me" else minutes_val

    with db.get_db() as conn:
        db.update_game_state(conn, appid, status=GameStatus.in_progress, manually_set=True)
        # Look up the game name for the pick_history record.
        game = db.get_game_by_appid(conn, appid)
        game_name = game.name if game else f"App {appid}"
        db.insert_pick_history(
            conn,
            appid=appid,
            game_name=game_name,
            mode=mode_str,
            time_window_minutes=time_window,
            candidates_at_pick=candidates_at_pick,
        )

    return templates.TemplateResponse(request, "partials/game_card_confirm.html", {
        "appid": appid,
    })


@router.post("/games/{appid}/state", response_class=HTMLResponse)
async def update_state_from_card(request: Request, appid: int):
    """
    Used by 'Not feeling it' on the pick page.
    hx-vals sends JSON; reads status from request body.
    """
    from app.models import GameStatus
    body = await request.json()
    status = GameStatus(body.get("status", "not_interested"))
    with db.get_db() as conn:
        db.update_game_state(conn, appid, status=status, manually_set=True)

    return HTMLResponse(f'<div id="card-{appid}" class="game-card game-card--dismissed"></div>')
