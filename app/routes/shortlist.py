import json
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app import prompt_state, actions, taste
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
    excluded_ids = _parse_excluded(request) | frozenset(prompt_state.skipped_appids)

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
        pending_raw = db.get_oldest_pending_pick(conn, prompt_state._dismissed)
        all_games = db.get_games_with_state(conn)

    pending_pick = None
    if pending_raw and not prompt_state.is_dismissed(pending_raw.id):
        pending_pick = pending_raw

    requested_mode = normalize_mode(request.query_params.get("mode"))
    initial_mode = requested_mode or default_mode_for_library(all_games)

    from app.routes.feedback import resume_context
    return templates.TemplateResponse(request, "pick.html", {
        "recent_picks": recent_picks,
        "pending_pick": pending_pick,
        "feedback_template": resume_context(pending_pick) if pending_pick else "partials/feedback_step1.html",
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


@router.post("/picks/reset", response_class=HTMLResponse)
async def reset_picks(request: Request, minutes: int = 90, mode: Optional[str] = None):
    prompt_state.skipped_appids.clear()
    prompt_state.skip_undo.clear()
    return templates.TemplateResponse(request, "partials/recommendations.html", _build_picks_context(request, minutes, mode))


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
      confirm_finished   — mark finished + +0.5 affinity per label (Backlog
                           "Mark finished" — high engagement is a positive signal)
    """
    with db.get_db() as conn:
        token = actions.perform(conn, appid, action, pick_id)
    target = (f"backlog-row-{appid}" if card_context == "backlog"
              else f"recent-card-{pick_id}" if card_context == "recent_pick" else f"card-{appid}")
    return templates.TemplateResponse(request, "partials/action_done.html", {
        "target_id":target, "message":actions.MESSAGES[action], "undo_token":token,
    })


@router.post("/actions/undo", response_class=HTMLResponse)
async def undo_action(request: Request, token: str = Form(...)):
    if token in prompt_state.skip_undo:
        appid = prompt_state.skip_undo.pop(token)
        prompt_state.skipped_appids.discard(appid)
    else:
        with db.get_db() as conn:
            appid = actions.undo(conn, token)
    response = HTMLResponse("Undone")
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/games/{appid}/pick", response_class=HTMLResponse)
async def mark_picked(request: Request, appid: int):
    from app.models import GameStatus
    body = await request.form()
    raw_ids = body.getlist("candidates_at_pick")
    try:
        candidates_at_pick = list(dict.fromkeys(int(v) for v in raw_ids))
        minutes_val = int(body.get("minutes", 90))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid pick details")
    mode_str = normalize_mode(body.get("mode"))
    if not mode_str or not 15 <= minutes_val <= 480 or len(candidates_at_pick) > 100 or any(v <= 0 for v in candidates_at_pick):
        raise HTTPException(status_code=400, detail="Invalid pick details")
    # Time window only meaningful for "I only have tonight"; the four other
    # modes are intent-driven and ignore the slider.
    time_window = (
        minutes_val if mode_str == RecommendMode.i_only_have_tonight.value else None
    )

    from app.backlog import is_forever_game

    with db.get_db() as conn:
        taste.write_lock(conn)
        # Capture pre-pick eligibility for the Dashboard's picks-per-week
        # filter. Read BEFORE update_game_state so we record the state the
        # user actually acted on, not the in_progress state we're about to set.
        pre = db.get_game_with_state_by_appid(conn, appid)
        if pre is None:
            raise HTTPException(status_code=404, detail="Game not found")
        status_at_pick = pre.state.status.value
        was_forever_at_pick = is_forever_game(pre.game) if pre else None

        before = actions._snapshot(conn, appid)
        db.update_game_state(conn, appid, status=GameStatus.in_progress, manually_set=True)
        # Picking a game from Shortlist auto-clears any backlog pin on it —
        # the user has already acted on the surface, no need to keep boosting.
        db.clear_pin(conn, appid)
        taste.set_signal(conn, f'quick:{appid}', appid, [])
        game = pre.game if pre else None
        game_name = game.name if game else f"App {appid}"
        pick_id = db.insert_pick_history(
            conn,
            appid=appid,
            game_name=game_name,
            mode=mode_str,
            time_window_minutes=time_window,
            candidates_at_pick=candidates_at_pick,
            status_at_pick=status_at_pick,
            was_forever_at_pick=was_forever_at_pick,
        )

        before['pick_id'] = pick_id
        after = actions._snapshot(conn, appid, pick_id)
        token = actions.record_undo(conn, appid, before, after)
    return templates.TemplateResponse(request, "partials/action_done.html", {
        "target_id":f"card-{appid}", "message":"Added to your recent picks", "undo_token":token,
    })


@router.post("/games/{appid}/state", response_class=HTMLResponse)
async def update_state_from_card(request: Request, appid: int):
    import secrets
    with db.get_db() as conn:
        if db.get_game_by_appid(conn, appid) is None:
            raise HTTPException(404, "Game not found")
    prompt_state.skipped_appids.add(appid)
    token = secrets.token_urlsafe(24)
    prompt_state.skip_undo[token] = appid
    return templates.TemplateResponse(request, "partials/action_done.html", {
        "target_id":f"card-{appid}", "message":"Skipped for this session", "undo_token":token,
    })
