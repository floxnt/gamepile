from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app.backlog import is_forever_game
from app.fetchers import hltb as hltb_fetcher
from app.game_detail import (
    compute_per_game_affinity_pills,
    format_pick_history_rows,
    parse_status_form_value,
    valid_status_transitions,
)
from app.game_type import (
    ALL_GAME_TYPES,
    GAME_TYPE_LABELS,
    resolve_type,
)
from app.models import GameStatus
from app.templates_config import templates


def _build_game_type_options(game) -> list:
    """[(value, label, is_current), ...] for the Type dropdown.
    Unlike statuses (which gate options on transitions), all 11 game
    types are always selectable — the user can correct any
    misclassification."""
    current = resolve_type(game)
    return [(t, GAME_TYPE_LABELS[t], t == current) for t in ALL_GAME_TYPES]

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
        "game_type_options": _build_game_type_options(gws.game),
        "is_forever": is_forever_game(gws.game),
        # Phase 4 — error pane variable for the HLTB-override form. None
        # on full-page load; populated only when /hltb_id POST returns
        # the data partial after a parse / fetch failure.
        "hltb_error": None,
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
        "game_type_options": _build_game_type_options(gws.game),
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


@router.post("/games/{appid}/game_type", response_class=HTMLResponse)
async def update_game_type(
    request: Request,
    appid: int,
    game_type: str = Form(...),
):
    """Set game_type via dropdown; marks game_type_manual=True so refresh
    inference doesn't override on subsequent runs."""
    if game_type not in ALL_GAME_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown game_type: {game_type}")
    with db.get_db() as conn:
        if db.get_game_by_appid(conn, appid) is None:
            raise HTTPException(status_code=404)
        db.set_game_type(conn, appid, game_type, manual=True)
    ctx = _status_bar_context(appid)
    return templates.TemplateResponse(request, "partials/game_detail_status_bar.html", ctx)


@router.post("/games/{appid}/reset_game_type", response_class=HTMLResponse)
async def reset_game_type(request: Request, appid: int):
    """Clear game_type_manual and immediately re-run classify_game.
    Mirrors reset_status — clearing the flag without re-classifying
    leaves the cached value stale until next refresh."""
    with db.get_db() as conn:
        result = db.reset_game_type_to_inferred(conn, appid)
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
    """Set or clear personal rating (0-10 half-star scale)."""
    if clear:
        new_value: Optional[int] = None
    else:
        if rating is None or not (1 <= rating <= 10):
            raise HTTPException(status_code=400, detail="Rating must be 1–10 or clear=1")
        new_value = rating

    with db.get_db() as conn:
        db.set_personal_rating(conn, appid, new_value)
        gws = db.get_game_with_state_by_appid(conn, appid)
    if gws is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "partials/game_detail_rating.html", {
        "game": gws.game,
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


# ---------------------------------------------------------------------------
# Phase 4 — Manual HLTB ID override
# ---------------------------------------------------------------------------


def _data_partial_context(appid: int) -> Optional[dict]:
    """Subset needed to re-render the Game Detail external-data partial
    after an HLTB-override save / reset. Just the game row — the template
    pulls everything else through Jinja globals."""
    with db.get_db() as conn:
        game = db.get_game_by_appid(conn, appid)
    if game is None:
        return None
    return {"game": game, "hltb_error": None}


@router.post("/games/{appid}/hltb_id", response_class=HTMLResponse)
async def update_hltb_id(
    request: Request,
    appid: int,
    hltb_id_input: str = Form(...),
):
    """Persist a manual HLTB ID and refetch HLTB data inline. Accepts a
    bare integer ('12345') or a howlongtobeat.com URL (we parse the ID
    out). Returns the data-section partial on success; on bad input or
    failed fetch, returns the partial with an error pane and does NOT
    persist."""
    parsed_id = hltb_fetcher.parse_hltb_id_input(hltb_id_input)
    if parsed_id is None:
        ctx = _data_partial_context(appid)
        if ctx is None:
            raise HTTPException(status_code=404)
        ctx["hltb_error"] = (
            "Couldn't parse an HLTB ID from that input. "
            "Use a number like 12345 or a howlongtobeat.com/game/12345 URL."
        )
        return templates.TemplateResponse(
            request, "partials/game_detail_data.html", ctx,
        )

    with db.get_db() as conn:
        game = db.get_game_by_appid(conn, appid)
    if game is None:
        raise HTTPException(status_code=404)

    result = await hltb_fetcher.fetch_hltb_by_id(parsed_id)
    if not result.found:
        # Fetch failed (bad ID, network, parse). Don't persist — surface
        # an inline error so the user can correct without losing state.
        ctx = _data_partial_context(appid)
        if ctx is None:
            raise HTTPException(status_code=404)
        ctx["hltb_error"] = (
            f"HLTB returned no record for ID {parsed_id}. "
            "Double-check the ID at howlongtobeat.com."
        )
        return templates.TemplateResponse(
            request, "partials/game_detail_data.html", ctx,
        )

    with db.get_db() as conn:
        db.set_hltb_id_manual(
            conn, appid,
            hltb_id=parsed_id,
            main_hours=result.hltb_main_hours,
            main_extra_hours=result.hltb_main_extra_hours,
            completionist_hours=result.hltb_completionist_hours,
        )

    ctx = _data_partial_context(appid)
    if ctx is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "partials/game_detail_data.html", ctx,
    )


@router.post("/games/{appid}/reset_hltb_id", response_class=HTMLResponse)
async def reset_hltb_id(request: Request, appid: int):
    """Clear the manual HLTB ID and re-run name-based search inline so
    HLTB values reflect the auto-derived match immediately."""
    with db.get_db() as conn:
        game = db.get_game_by_appid(conn, appid)
    if game is None:
        raise HTTPException(status_code=404)

    result = await hltb_fetcher.fetch_hltb(game.name)
    if result.found:
        main = result.hltb_main_hours
        main_extra = result.hltb_main_extra_hours
        completionist = result.hltb_completionist_hours
    else:
        # Name-search miss — clear the manual ID and null out the cached
        # HLTB values so the user sees the same result a fresh refresh
        # would produce. Better than leaving stale values that pretend
        # the auto path "works" when it doesn't.
        main = None
        main_extra = None
        completionist = None

    with db.get_db() as conn:
        db.clear_hltb_id_manual(
            conn, appid,
            main_hours=main,
            main_extra_hours=main_extra,
            completionist_hours=completionist,
        )

    ctx = _data_partial_context(appid)
    if ctx is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "partials/game_detail_data.html", ctx,
    )
