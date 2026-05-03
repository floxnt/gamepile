from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app.dashboard import build_dashboard_data
from app.templates_config import templates

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    now = datetime.utcnow()
    seven_days_ago = now - timedelta(days=7)

    with db.get_db() as conn:
        games = db.get_games_with_state(conn)
        picks_last_7d = db.get_picks_since(conn, seven_days_ago)
        most_recent_pick = db.get_most_recent_pick(conn)
        affinities = db.get_all_affinities(conn)

    data = build_dashboard_data(
        games=games,
        picks_last_7d=picks_last_7d,
        most_recent_pick=most_recent_pick,
        affinities=affinities,
        now=now,
    )

    return templates.TemplateResponse(request, "dashboard.html", {
        "data": data,
    })
