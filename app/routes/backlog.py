from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app.backlog import (
    SORT_LABELS,
    STATUS_CHIP_LABELS,
    TIME_FIT_LABELS,
    VALID_SORT_KEYS,
    build_backlog_view,
    parse_backlog_query,
    valid_actions_for_status,
)
from app.templates_config import templates

router = APIRouter()


@router.get("/backlog", response_class=HTMLResponse)
async def backlog_page(request: Request):
    filters = parse_backlog_query(request.query_params)

    with db.get_db() as conn:
        # Sweep stale pins before reading. Mirrors the same call in
        # _build_picks_context so expired pins never get one final boost.
        db.expire_pins(conn)
        games = db.get_games_with_state(conn)
        affinities = db.get_all_affinities(conn)

    view = build_backlog_view(games, filters, affinities)

    return templates.TemplateResponse(request, "backlog.html", {
        "view": view,
        "filters": filters,
        "time_fit_labels": TIME_FIT_LABELS,
        "status_chip_labels": STATUS_CHIP_LABELS,
        "sort_labels": SORT_LABELS,
        "sort_keys": VALID_SORT_KEYS,
        "valid_actions_for_status": valid_actions_for_status,
    })


@router.post("/backlog/{appid}/pin", response_class=HTMLResponse)
async def pin_game(request: Request, appid: int):
    with db.get_db() as conn:
        db.set_pin(conn, appid)
    return templates.TemplateResponse(request, "partials/backlog_pin_button.html", {
        "appid": appid,
        "pinned": True,
    })


@router.post("/backlog/{appid}/unpin", response_class=HTMLResponse)
async def unpin_game(request: Request, appid: int):
    with db.get_db() as conn:
        db.clear_pin(conn, appid)
    return templates.TemplateResponse(request, "partials/backlog_pin_button.html", {
        "appid": appid,
        "pinned": False,
    })
