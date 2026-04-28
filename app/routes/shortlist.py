import json
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app import prompt_state
from app.affinity import apply_quick_drop_affinity
from app.recommender import RecommendMode, RecommendRequest, recommend
from app.templates_config import templates

router = APIRouter()


def _bool_param(request: Request, name: str, default: bool = True) -> bool:
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
    return "surprise_me" if raw == "surprise" else raw


def _build_picks_context(request: Request, minutes: int, mode: str) -> dict:
    mode = _normalize_mode(mode)
    include_unplayed = _bool_param(request, "include_unplayed", default=True)
    include_in_progress = _bool_param(request, "include_in_progress", default=True)
    excluded_ids = _parse_excluded(request)

    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn)
        affinities = db.get_all_affinities(conn)

    req = RecommendRequest(
        minutes=minutes,
        mode=RecommendMode(mode),
        include_unplayed=include_unplayed,
        include_in_progress=include_in_progress,
        excluded_ids=excluded_ids,
        affinities=affinities,
    )

    picks = recommend(all_games, req)
    picks_appids = json.dumps([p.gws.game.appid for p in picks])

    return {
        "picks": picks,
        "picks_appids": picks_appids,
        "minutes": minutes,
        "mode": mode,
        "include_unplayed": include_unplayed,
        "include_in_progress": include_in_progress,
        "has_exclusions": bool(excluded_ids),
    }


@router.get("/", response_class=HTMLResponse)
async def pick_page(request: Request):
    """Full page. Always starts with the recent-picks view — recommendations
    are session-only state loaded via HTMX after the user clicks Find Games."""
    with db.get_db() as conn:
        recent_picks = db.get_recent_picks(conn, limit=8)
        pending_raw = db.get_oldest_pending_pick(conn)

    pending_pick = None
    if pending_raw and not prompt_state.is_dismissed(pending_raw.id):
        pending_pick = pending_raw

    return templates.TemplateResponse(request, "pick.html", {
        "recent_picks": recent_picks,
        "pending_pick": pending_pick,
        # Default form state used to pre-fill controls when the panel opens
        "minutes": 90,
        "mode": "both",
        "include_unplayed": True,
        "include_in_progress": True,
    })


@router.get("/recent-picks", response_class=HTMLResponse)
async def recent_picks_partial(request: Request):
    """Partial: <div id='main-content'> containing the 8 most recent picks."""
    with db.get_db() as conn:
        recent_picks = db.get_recent_picks(conn, limit=8)
    return templates.TemplateResponse(request, "partials/recent_picks.html", {
        "recent_picks": recent_picks,
    })


@router.get("/picks", response_class=HTMLResponse)
async def picks_partial(
    request: Request,
    minutes: int = 90,
    mode: str = "both",
):
    """Partial: <div id='main-content'> containing the recommendation cards."""
    ctx = _build_picks_context(request, minutes, mode)
    return templates.TemplateResponse(request, "partials/recommendations.html", ctx)


@router.post("/games/{appid}/quick-action", response_class=HTMLResponse)
async def quick_action(
    request: Request,
    appid: int,
    action: str = Form(...),
    pick_id: Optional[int] = Form(None),
    card_context: str = Form("recommendation"),  # "recent_pick" | "recommendation"
):
    """
    Quick-action buttons on both card types.

    Actions:
      finished         — mark finished, no affinity (correcting historical data)
      bounced          — dropped + soft(-0.5) affinity per genre/tag/dev
      not_my_thing     — dropped + strong(-1.0) affinity per genre/tag/dev
      never_recommend  — blacklisted=True, no affinity
      already_completed — alias for finished on recommendation cards
    """
    from app.models import GameStatus

    with db.get_db() as conn:
        game = db.get_game_by_appid(conn, appid)
        if not game:
            return HTMLResponse("Not found", status_code=404)

        if action in ("finished", "already_completed"):
            db.update_game_state(conn, appid, status=GameStatus.finished, manually_set=True)
            if pick_id:
                db.update_pick_outcome(conn, pick_id, outcome="played_and_finished")

        elif action == "bounced":
            db.update_game_state(
                conn, appid,
                status=GameStatus.dropped,
                dropped_strength="soft",
                manually_set=True,
            )
            if game:
                apply_quick_drop_affinity(conn, game, "soft")
            if pick_id:
                db.update_pick_outcome(conn, pick_id, outcome="played_and_dropped")

        elif action == "not_my_thing":
            db.update_game_state(
                conn, appid,
                status=GameStatus.dropped,
                dropped_strength="strong",
                manually_set=True,
            )
            if game:
                apply_quick_drop_affinity(conn, game, "strong")
            if pick_id:
                db.update_pick_outcome(conn, pick_id, outcome="played_and_dropped")

        elif action == "never_recommend":
            db.update_game_state(conn, appid, blacklisted=True, manually_set=True)

    # Recommendation cards get fully dismissed after any action.
    if card_context == "recommendation":
        return HTMLResponse(
            f'<div id="card-{appid}" class="game-card game-card--dismissed"></div>'
        )

    # Recent pick cards reload as updated state.
    if pick_id is None:
        return HTMLResponse(f'<div id="recent-card-{appid}"></div>')

    with db.get_db() as conn:
        updated_picks = db.get_recent_picks(conn, limit=8)
    rp = next((p for p in updated_picks if p.pick.id == pick_id), None)
    if not rp:
        return HTMLResponse(f'<div id="recent-card-{pick_id}"></div>')

    return templates.TemplateResponse(request, "partials/recent_pick_card.html", {
        "rp": rp,
    })


@router.post("/games/{appid}/pick", response_class=HTMLResponse)
async def mark_picked(request: Request, appid: int):
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
    from app.models import GameStatus
    body = await request.json()
    status = GameStatus(body.get("status", "not_interested"))
    with db.get_db() as conn:
        db.update_game_state(conn, appid, status=status, manually_set=True)
    return HTMLResponse(f'<div id="card-{appid}" class="game-card game-card--dismissed"></div>')
