from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app.backlog import is_forever_game
from app.game_detail import (
    compute_per_game_affinity_pills,
    format_pick_history_rows,
    parse_status_form_value,
    valid_status_transitions,
)
from app.models import GameStatus
from app.templates_config import templates

router = APIRouter()


def _build_full_context(appid: int) -> Optional[dict]:
    """Load everything the full page needs in one DB connection."""
    with db.get_db() as conn:
        gws = db.get_game_with_state_by_appid(conn, appid)
        if gws is None:
            return None
        affinities = db.get_all_affinities(conn)
        picks = db.get_picks_for_appid(conn, appid)
        # Resolve any retroactive-pick game names for the history section.
        retro_appids = {p.actually_played_appid for p in picks if p.actually_played_appid}
        retro_appids |= {p.would_have_picked_other_appid for p in picks if p.would_have_picked_other_appid}
        retro_appids.discard(None)
        name_map: dict = {}
        for retro_appid in retro_appids:
            game = db.get_game_by_appid(conn, retro_appid)
            if game:
                name_map[retro_appid] = game.name

    affinity_categories = compute_per_game_affinity_pills(gws.game, affinities)
    pick_rows = format_pick_history_rows(picks, name_map)
    status_options = valid_status_transitions(gws.state.status, gws.state.dropped_strength)

    return {
        "gws": gws,
        "game": gws.game,
        "state": gws.state,
        "affinity_categories": affinity_categories,
        "affinity_has_any": any(c.pills for c in affinity_categories),
        "pick_rows": pick_rows,
        "status_options": status_options,
        "is_forever": is_forever_game(gws.game),
    }


def _status_bar_context(appid: int) -> Optional[dict]:
    """Subset of context needed to re-render just the status bar partial."""
    with db.get_db() as conn:
        gws = db.get_game_with_state_by_appid(conn, appid)
    if gws is None:
        return None
    return {
        "game": gws.game,
        "state": gws.state,
        "status_options": valid_status_transitions(gws.state.status, gws.state.dropped_strength),
    }


@router.get("/games/{appid}", response_class=HTMLResponse)
async def game_detail_page(request: Request, appid: int):
    ctx = _build_full_context(appid)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Game not found")
    return templates.TemplateResponse(request, "game_detail.html", ctx)


@router.post("/games/{appid}/status", response_class=HTMLResponse)
async def update_status(
    request: Request,
    appid: int,
    status: str = Form(...),
):
    """Set status (and dropped_strength when applicable). Marks manually_set."""
    parsed_status, dropped_strength = parse_status_form_value(status)
    try:
        new_status = GameStatus(parsed_status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown status: {status}")

    with db.get_db() as conn:
        if dropped_strength is not None:
            db.update_game_state(
                conn, appid,
                status=new_status,
                dropped_strength=dropped_strength,
                manually_set=True,
            )
        else:
            db.update_game_state(conn, appid, status=new_status, manually_set=True)

    ctx = _status_bar_context(appid)
    if ctx is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "partials/game_detail_status_bar.html", ctx)


@router.post("/games/{appid}/reset_status", response_class=HTMLResponse)
async def reset_status(request: Request, appid: int):
    """Clear manually_set and re-run infer_status against current playtime
    + HLTB + last_played. Status may demote (e.g. finished → played_unclassified).
    """
    with db.get_db() as conn:
        result = db.reset_status_to_inferred(conn, appid)
    if result is None:
        raise HTTPException(status_code=404)
    ctx = _status_bar_context(appid)
    return templates.TemplateResponse(request, "partials/game_detail_status_bar.html", ctx)


@router.post("/games/{appid}/notes", response_class=HTMLResponse)
async def update_notes(
    request: Request,
    appid: int,
    notes: str = Form(""),
):
    with db.get_db() as conn:
        db.set_notes(conn, appid, notes)
        gws = db.get_game_with_state_by_appid(conn, appid)
    if gws is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "partials/game_detail_notes.html", {
        "state": gws.state,
        "saved": True,
    })


@router.post("/games/{appid}/rating", response_class=HTMLResponse)
async def update_rating(
    request: Request,
    appid: int,
    rating: Optional[int] = Form(None),
    clear: Optional[str] = Form(None),
):
    """Set or clear personal rating. Pass clear=1 to unset; otherwise rating must be 1-5."""
    if clear:
        new_value: Optional[int] = None
    else:
        if rating is None or not (1 <= rating <= 5):
            raise HTTPException(status_code=400, detail="Rating must be 1–5 or clear=1")
        new_value = rating

    with db.get_db() as conn:
        db.set_personal_rating(conn, appid, new_value)
        gws = db.get_game_with_state_by_appid(conn, appid)
    if gws is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "partials/game_detail_rating.html", {
        "state": gws.state,
    })


@router.post("/games/{appid}/hours_played_manual", response_class=HTMLResponse)
async def update_hours_played_manual(
    request: Request,
    appid: int,
    hours: Optional[float] = Form(None),
    clear: Optional[str] = Form(None),
):
    if clear:
        new_value: Optional[float] = None
    else:
        if hours is None or hours < 0:
            raise HTTPException(status_code=400, detail="Hours must be ≥ 0 or clear=1")
        new_value = hours

    with db.get_db() as conn:
        db.set_hours_played_manual(conn, appid, new_value)
        gws = db.get_game_with_state_by_appid(conn, appid)
    if gws is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "partials/game_detail_hours.html", {
        "state": gws.state,
        "saved": True,
    })
