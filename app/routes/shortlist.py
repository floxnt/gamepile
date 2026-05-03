import json
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app import prompt_state
from app.affinity import apply_quick_drop_affinity
from app.recommender import (
    RecommendMode,
    RecommendRequest,
    default_mode_for_library,
    normalize_mode,
    recommend,
)
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


def _resolve_mode(raw: Optional[str], games_for_default) -> str:
    canonical = normalize_mode(raw)
    if canonical:
        return canonical
    return default_mode_for_library(games_for_default)


def _build_picks_context(request: Request, minutes: int, mode: Optional[str]) -> dict:
    include_unplayed = _bool_param(request, "include_unplayed", default=True)
    include_in_progress = _bool_param(request, "include_in_progress", default=True)
    excluded_ids = _parse_excluded(request)

    with db.get_db() as conn:
        # Sweep expired pins (>14 days) before loading games — must happen
        # before get_games_with_state so swept pins don't get one final boost.
        db.expire_pins(conn)
        all_games = db.get_games_with_state(conn)
        affinities = db.get_all_affinities(conn)

    canonical_mode = _resolve_mode(mode, all_games)

    req = RecommendRequest(
        minutes=minutes,
        mode=RecommendMode(canonical_mode),
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
        "mode": canonical_mode,
        "include_unplayed": include_unplayed,
        "include_in_progress": include_in_progress,
        "has_exclusions": bool(excluded_ids),
    }


@router.get("/", response_class=HTMLResponse)
async def shortlist_page(request: Request):
    """Full page. Always starts with the recent-picks view — recommendations
    are session-only state loaded via HTMX after the user clicks Find Games.

    Optional ?mode=<canonical> query param preselects the radio (used by the
    empty-Backlog CTA to deep-link into Comfort Pick).
    """
    with db.get_db() as conn:
        recent_picks = db.get_recent_picks(conn, limit=8)
        pending_raw = db.get_oldest_pending_pick(conn)
        all_games = db.get_games_with_state(conn)

    pending_pick = None
    if pending_raw and not prompt_state.is_dismissed(pending_raw.id):
        pending_pick = pending_raw

    requested_mode = normalize_mode(request.query_params.get("mode"))
    initial_mode = requested_mode or default_mode_for_library(all_games)

    return templates.TemplateResponse(request, "pick.html", {
        "recent_picks": recent_picks,
        "pending_pick": pending_pick,
        "minutes": 90,
        "mode": initial_mode,
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
    mode: Optional[str] = None,
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
    Quick-action buttons used by Shortlist cards, recent-pick cards, and
    backlog rows.

    Actions:
      finished           — mark finished, no affinity (correcting historical data)
      bounced            — dropped + soft(-0.5) affinity per genre/tag/dev
      not_my_thing       — dropped + strong(-1.0) affinity per genre/tag/dev
      never_recommend    — blacklisted=True, no affinity
      already_completed  — alias for finished
      mark_in_progress   — mark in progress (used by backlog overflow menu)
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

        elif action == "mark_in_progress":
            db.update_game_state(conn, appid, status=GameStatus.in_progress, manually_set=True)

        elif action == "pick":
            # Backlog "I picked this" — status nudge only. No pick_history row
            # (this isn't a recommendation outcome) and any pin auto-clears.
            db.update_game_state(conn, appid, status=GameStatus.in_progress, manually_set=True)
            db.clear_pin(conn, appid)

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

    if card_context == "recommendation":
        return HTMLResponse(
            f'<div id="card-{appid}" class="game-card game-card--dismissed"></div>'
        )

    if card_context == "backlog":
        # Dismiss the row in-place. Section counts and stats become slightly
        # stale until the user reloads /backlog — acceptable given the
        # alternative (re-rendering the whole page on every action) is heavy.
        return HTMLResponse(
            f'<div id="backlog-row-{appid}" class="backlog-row backlog-row--dismissed"></div>'
        )

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
    mode_str = normalize_mode(body.get("mode")) or RecommendMode.i_only_have_tonight.value
    minutes_val = int(body.get("minutes", 90))
    # Time window only meaningful for "I only have tonight"; the four other
    # modes are intent-driven and ignore the slider.
    time_window = (
        minutes_val if mode_str == RecommendMode.i_only_have_tonight.value else None
    )

    with db.get_db() as conn:
        db.update_game_state(conn, appid, status=GameStatus.in_progress, manually_set=True)
        # Picking a game from Shortlist auto-clears any backlog pin on it —
        # the user has already acted on the surface, no need to keep boosting.
        db.clear_pin(conn, appid)
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
