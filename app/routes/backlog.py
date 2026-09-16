import json
import urllib.parse

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import database as db
from app import actions, decision_sessions
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
        "resume_sessions": [s for s in decision_sessions.sessions.values() if s.index < len(s.queue)],
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
        actions.perform(conn, appid, "pin")
    return templates.TemplateResponse(request, "partials/backlog_pin_button.html", {
        "appid": appid,
        "pinned": True,
    })


@router.post("/backlog/{appid}/unpin", response_class=HTMLResponse)
async def unpin_game(request: Request, appid: int):
    with db.get_db() as conn:
        actions.perform(conn, appid, "unpin")
    return templates.TemplateResponse(request, "partials/backlog_pin_button.html", {
        "appid": appid,
        "pinned": False,
    })


def _session(session_id):
    session=decision_sessions.sessions.get(session_id)
    if session is None:
        raise HTTPException(404,"This review session has ended. Start another from Backlog.")
    return session


def _session_context(session):
    with db.get_db() as conn:
        gws=db.get_game_with_state_by_appid(conn,session.queue[session.index]) if session.index < len(session.queue) else None
        games=db.get_games_with_state(conn)
        affinities=db.get_all_affinities(conn)
    return {
        "session":session,"section_title":SECTION_TITLES[session.section],
        "section_key":session.section,"index":session.index,"total":len(session.queue),
        "counts":session.counts,"total_reviewed":sum(session.counts.values()),
        "gws":gws,"hints":compute_decision_hints(gws,affinities,compute_session_thresholds(games)) if gws else [],
        "actions":valid_actions_for_status(gws.state.status) if gws else [],
    }


@router.post("/backlog/session/start",response_class=HTMLResponse)
async def session_start(request:Request,section_key:str=Form(...),appids_json:str=Form(...)):
    try:
        queue=json.loads(appids_json)
        if not isinstance(queue,list) or len(queue)>20000 or any(type(v) is not int or v <= 0 for v in queue):
            raise ValueError()
        queue=list(dict.fromkeys(queue))
    except (ValueError,TypeError):
        raise HTTPException(400,"Invalid review queue")
    if section_key not in SECTION_TITLES:
        raise HTTPException(400,"Unknown backlog section")
    with db.get_db() as conn:
        owned={g.game.appid for g in db.get_games_with_state(conn)}
    if not set(queue).issubset(owned):
        raise HTTPException(400,"Some games are no longer in this library")
    session=decision_sessions.create(section_key,queue)
    response=HTMLResponse("")
    response.headers["HX-Redirect"]=f"/backlog/session/{session.id}"
    return response


@router.get("/backlog/session/{session_id}",response_class=HTMLResponse)
async def session_page(request:Request,session_id:str):
    return templates.TemplateResponse(request,"session.html",_session_context(_session(session_id)))


@router.post("/backlog/session/action",response_class=HTMLResponse)
async def session_action(request:Request,session_id:str=Form(...),index:int=Form(...),appid:int=Form(...),action:str=Form(...)):
    session=_session(session_id)
    # A retry from the previous card returns the current card, without
    # repeating its state change or incrementing counters again.
    if index == session.index and index < len(session.queue):
        if appid != session.queue[index]:
            raise HTTPException(400,"Game does not match this review card")
        with db.get_db() as conn:
            gws=db.get_game_with_state_by_appid(conn,appid)
            valid={name for name,_ in valid_actions_for_status(gws.state.status)} | {'skip','pin'}
            if action not in valid:
                raise HTTPException(400,"This action is not available for the current game")
            token=None if action == 'skip' else actions.perform(conn,appid,action)
        key={'confirm_finished':'finished','already_completed':'finished','mark_in_progress':'in_progress',
             'pick':'in_progress','skip':'skipped','pin':'pinned'}.get(action,action)
        session.history.append((session.index,dict(session.counts),token))
        session.counts[key]=session.counts.get(key,0)+1
        session.index += 1
    return templates.TemplateResponse(request,"partials/session_view.html",_session_context(session))


@router.post("/backlog/session/{session_id}/undo",response_class=HTMLResponse)
async def session_undo(request:Request,session_id:str,index:int=Form(...)):
    session=_session(session_id)
    if session.history and index == session.index:
        previous,counts,token=session.history[-1]
        if token:
            with db.get_db() as conn:
                actions.undo(conn,token)
        session.history.pop()
        session.index,session.counts=previous,counts
    return templates.TemplateResponse(request,"partials/session_view.html",_session_context(session))
