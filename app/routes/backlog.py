import json
import urllib.parse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app.backlog import (
    SECTION_TITLES,
    SORT_LABELS,
    STATUS_CHIP_LABELS,
    TIME_FIT_LABELS,
    VALID_SORT_KEYS,
    build_backlog_view,
    compute_decision_hints,
    compute_session_thresholds,
    parse_backlog_query,
    valid_actions_for_status,
)
from app.templates_config import templates

router = APIRouter()


# v3.5 polish — pill-driven filter URL params. Used by _build_clear_pill_url
# to strip JUST the active pill (preserving all other filter state) when
# the user clicks the indicator's Clear button.
_PILL_QUERY_KEYS = ("genre", "tag", "developer")


def _build_clear_pill_url(query_params, pill_kind: str | None) -> str:
    """Return /backlog URL with the active pill param stripped, all other
    query state preserved. None pill_kind → /backlog with no params."""
    if not pill_kind:
        return "/backlog"
    # Re-emit every param EXCEPT the one keyed to the active pill.
    pairs = [
        (k, v) for k, v in query_params.multi_items()
        if k not in _PILL_QUERY_KEYS
    ]
    if not pairs:
        return "/backlog"
    return "/backlog?" + urllib.parse.urlencode(pairs)


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
        "clear_pill_url": _build_clear_pill_url(
            request.query_params, view.pill_filter_kind,
        ),
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


# ---------------------------------------------------------------------------
# Decision Sessions — in-memory, contextual review flow over Backlog sections
# ---------------------------------------------------------------------------

def _apply_session_action(appid: int, action: str) -> None:
    """Apply a state-change action. Same transitions as shortlist quick-action."""
    from app.affinity import apply_quick_drop_affinity, apply_quick_finished_affinity
    from app.models import GameStatus

    with db.get_db() as conn:
        game = db.get_game_by_appid(conn, appid)
        if not game:
            return
        if action in ("finished", "already_completed"):
            db.update_game_state(conn, appid, status=GameStatus.finished, manually_set=True)
        elif action == "mark_in_progress":
            db.update_game_state(conn, appid, status=GameStatus.in_progress, manually_set=True)
        elif action == "confirm_finished":
            db.update_game_state(conn, appid, status=GameStatus.finished, manually_set=True)
            db.clear_pin(conn, appid)
            apply_quick_finished_affinity(conn, game)
        elif action == "pick":
            db.update_game_state(conn, appid, status=GameStatus.in_progress, manually_set=True)
            db.clear_pin(conn, appid)
        elif action == "bounced":
            db.update_game_state(conn, appid, status=GameStatus.dropped, dropped_strength="soft", manually_set=True)
            apply_quick_drop_affinity(conn, game, "soft")
        elif action == "not_my_thing":
            db.update_game_state(conn, appid, status=GameStatus.dropped, dropped_strength="strong", manually_set=True)
            apply_quick_drop_affinity(conn, game, "strong")
        elif action == "never_recommend":
            db.update_game_state(conn, appid, blacklisted=True, manually_set=True)

def _session_context(section_key: str, queue_json: str, index: int, counts: dict):
    """Build template context for the current session card."""
    queue = json.loads(queue_json)
    if index >= len(queue):
        return None, None, queue, counts

    appid = queue[index]
    with db.get_db() as conn:
        gws = db.get_game_with_state_by_appid(conn, appid)
        all_games = db.get_games_with_state(conn)
        affinities = db.get_all_affinities(conn)

    if gws is None:
        return None, None, queue, counts

    thresholds = compute_session_thresholds(all_games)
    hints = compute_decision_hints(gws, affinities, thresholds)
    actions = valid_actions_for_status(gws.state.status)

    return gws, {
        "gws": gws,
        "game": gws.game,
        "state": gws.state,
        "hints": hints,
        "actions": actions,
        "section_key": section_key,
        "section_title": SECTION_TITLES.get(section_key, section_key),
        "queue_json": queue_json,
        "index": index,
        "total": len(queue),
        "counts": counts,
        "valid_actions_for_status": valid_actions_for_status,
    }, queue, counts


@router.post("/backlog/session/start", response_class=HTMLResponse)
async def session_start(
    request: Request,
    section_key: str = Form(...),
    appids_json: str = Form(...),
):
    counts = {"pinned": 0, "finished": 0, "bounced": 0, "not_my_thing": 0,
              "never_recommend": 0, "skipped": 0, "in_progress": 0, "other": 0}

    _, ctx, queue, counts = _session_context(
        section_key, appids_json, 0, counts,
    )
    if ctx is None:
        return templates.TemplateResponse(request, "partials/session_recap.html", {
            "section_title": SECTION_TITLES.get(section_key, section_key),
            "counts": counts, "total_reviewed": 0,
        })

    return templates.TemplateResponse(request, "partials/session_view.html", ctx)


@router.post("/backlog/session/action", response_class=HTMLResponse)
async def session_action(
    request: Request,
    section_key: str = Form(...),
    queue_json: str = Form(...),
    index: int = Form(...),
    counts_json: str = Form(...),
    appid: int = Form(...),
    action: str = Form(...),
):
    counts = json.loads(counts_json)

    if action == "skip":
        counts["skipped"] = counts.get("skipped", 0) + 1
    else:
        _apply_session_action(appid, action)

        if action in ("confirm_finished", "already_completed"):
            counts["finished"] = counts.get("finished", 0) + 1
        elif action == "bounced":
            counts["bounced"] = counts.get("bounced", 0) + 1
        elif action == "not_my_thing":
            counts["not_my_thing"] = counts.get("not_my_thing", 0) + 1
        elif action == "never_recommend":
            counts["never_recommend"] = counts.get("never_recommend", 0) + 1
        elif action in ("mark_in_progress", "pick"):
            counts["in_progress"] = counts.get("in_progress", 0) + 1
        else:
            counts["other"] = counts.get("other", 0) + 1

    next_index = index + 1
    _, ctx, queue, counts = _session_context(
        section_key, queue_json, next_index, counts,
    )

    if ctx is None:
        total = sum(counts.values())
        return templates.TemplateResponse(request, "partials/session_recap.html", {
            "section_title": SECTION_TITLES.get(section_key, section_key),
            "counts": counts,
            "total_reviewed": total,
        })

    return templates.TemplateResponse(request, "partials/session_card.html", ctx)
