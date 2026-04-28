"""
Feedback flow — v2.

Four main HTMX-driven steps rendered inside a dismissible card on /.
The "did not play" branch adds steps 1.5 and 1.6 between steps 1 and 2.

Flow overview:
  Step 1:   Did you play it?
    → Yes:  Step 2 → Step 3 → Step 4 → close
    → No:   Step 1.5 (why didn't you?)
              no_time / technical_issue → close (affinity no-op)
              changed_mood              → Step 1.6 (optional follow-up) → close
              picked_another_game       → Step 1.6 (follow-up for signal) → close
    → Skip: dismiss for session, close
  Step 1.5: Why didn't you play it?
  Step 1.6: What did you play instead? (3 sub-options)
    1.6a: one of the 4 other recommendations (candidate card grid)
    1.6b: another game from your library (autocomplete search)
    1.6c: not in this app (no further data)
  Step 2:   How was it? (1-5)
  Step 3:   Was the genre/style a good match? (1-5)
  Step 4:   Would you have picked a different one?

Skip permanently: sets outcome=skipped_feedback. Available at every step.
Dismiss for session: hides prompt until next app launch, no DB change.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import database as db
from app import prompt_state
from app.affinity import apply_affinity_update, apply_did_not_play_affinity
from app.models import GameStatus
from app.templates_config import templates

router = APIRouter()
log = logging.getLogger(__name__)

_CLOSE_PROMPT = '<div id="feedback-prompt"></div>'
_DONE_PROMPT = (
    '<div id="feedback-prompt" class="feedback-prompt feedback-prompt--done">'
    "Thanks — recommendations will adjust based on your feedback."
    "</div>"
)


def _retarget_to_prompt(html: str) -> HTMLResponse:
    resp = HTMLResponse(html)
    resp.headers["HX-Retarget"] = "#feedback-prompt"
    resp.headers["HX-Reswap"] = "outerHTML"
    return resp


# ---------------------------------------------------------------------------
# Session / permanent dismissal
# ---------------------------------------------------------------------------

@router.post("/feedback/{pick_id}/dismiss")
async def feedback_dismiss(pick_id: int):
    prompt_state.dismiss_for_session(pick_id)
    return _retarget_to_prompt(_CLOSE_PROMPT)


@router.post("/feedback/{pick_id}/skip-permanently")
async def feedback_skip_permanently(pick_id: int):
    with db.get_db() as conn:
        db.update_pick_outcome(conn, pick_id, outcome="skipped_feedback")
    return _retarget_to_prompt(_CLOSE_PROMPT)


# ---------------------------------------------------------------------------
# Step 1 — Did you play it?
# ---------------------------------------------------------------------------

@router.post("/feedback/{pick_id}/step1")
async def feedback_step1(
    request: Request,
    pick_id: int,
    answer: str = Form(...),
):
    if answer == "skip":
        prompt_state.dismiss_for_session(pick_id)
        return _retarget_to_prompt(_CLOSE_PROMPT)

    if answer == "no":
        # Don't close yet — go to step 1.5 to capture why.
        with db.get_db() as conn:
            pick = db.get_pick_history_by_id(conn, pick_id)
        if not pick:
            return _retarget_to_prompt(_CLOSE_PROMPT)
        return templates.TemplateResponse(request, "partials/feedback_step1_5.html", {
            "pick": pick,
        })

    # "yes" → step 2
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
    if not pick:
        return _retarget_to_prompt(_CLOSE_PROMPT)
    return templates.TemplateResponse(request, "partials/feedback_step2.html", {
        "pick": pick,
    })


# ---------------------------------------------------------------------------
# Step 1.5 — Why didn't you play it?
# ---------------------------------------------------------------------------

@router.post("/feedback/{pick_id}/step1_5")
async def feedback_step1_5(
    request: Request,
    pick_id: int,
    reason: str = Form(...),
):
    """
    reason: no_time | changed_mood | picked_another_game | technical_issue | skip
    """
    if reason == "skip":
        prompt_state.dismiss_for_session(pick_id)
        return _retarget_to_prompt(_CLOSE_PROMPT)

    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
        if not pick:
            return _retarget_to_prompt(_CLOSE_PROMPT)

        # Always record the reason and outcome first.
        db.update_pick_outcome(
            conn, pick_id,
            outcome="did_not_play",
            did_not_play_reason=reason,
        )

        if reason == "technical_issue":
            db.update_game_state(conn, pick.appid, has_technical_issue=True)
            picked_game = db.get_game_by_appid(conn, pick.appid)
            if picked_game:
                apply_did_not_play_affinity(conn, picked_game, reason)
            return _retarget_to_prompt(_CLOSE_PROMPT)

        if reason == "no_time":
            picked_game = db.get_game_by_appid(conn, pick.appid)
            if picked_game:
                apply_did_not_play_affinity(conn, picked_game, reason)
            return _retarget_to_prompt(_CLOSE_PROMPT)

    # changed_mood or picked_another_game → go to step 1.6
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
    return templates.TemplateResponse(request, "partials/feedback_step1_6.html", {
        "pick": pick,
    })


# ---------------------------------------------------------------------------
# Step 1.6 — What did you play instead? (sub-option chooser)
# ---------------------------------------------------------------------------

@router.post("/feedback/{pick_id}/step1_6")
async def feedback_step1_6(
    request: Request,
    pick_id: int,
    sub_option: str = Form(...),
):
    """
    sub_option: from_candidates | from_library | not_in_app
    from_candidates → show the other 4 recommendation cards
    from_library    → show autocomplete search
    not_in_app      → record & apply affinity without a specific game, close
    """
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
        if not pick:
            return _retarget_to_prompt(_CLOSE_PROMPT)

        if sub_option == "not_in_app":
            picked_game = db.get_game_by_appid(conn, pick.appid)
            if picked_game:
                apply_did_not_play_affinity(
                    conn, picked_game, pick.did_not_play_reason or "picked_another_game"
                )
            return _retarget_to_prompt(_DONE_PROMPT)

        if sub_option == "from_candidates":
            candidate_appids = _parse_candidate_appids(pick.candidates_at_pick)
            other_appids = [a for a in candidate_appids if a != pick.appid]
            all_games = db.get_games_with_state(conn)
            other_candidates = [
                g for g in all_games
                if g.game.appid in other_appids
                and g.state.status != GameStatus.not_interested
            ]
            other_candidates.sort(key=lambda g: other_appids.index(g.game.appid))
            return templates.TemplateResponse(
                request, "partials/feedback_step1_6_candidates.html", {
                    "pick": pick,
                    "other_candidates": other_candidates,
                }
            )

    # from_library
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
    return templates.TemplateResponse(
        request, "partials/feedback_step1_6_library.html", {
            "pick": pick,
        }
    )


# ---------------------------------------------------------------------------
# Step 1.6a — From previous candidates
# ---------------------------------------------------------------------------

@router.post("/feedback/{pick_id}/step1_6_candidates")
async def feedback_step1_6_candidates(
    pick_id: int,
    selected_appid: Optional[int] = Form(None),
):
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
        if not pick:
            return _retarget_to_prompt(_CLOSE_PROMPT)

        if selected_appid:
            db.update_pick_outcome(conn, pick_id, would_have_picked_other_appid=selected_appid)

        picked_game = db.get_game_by_appid(conn, pick.appid)
        preferred_game = db.get_game_by_appid(conn, selected_appid) if selected_appid else None

        if picked_game:
            apply_did_not_play_affinity(
                conn, picked_game,
                pick.did_not_play_reason or "picked_another_game",
                preferred_game,
            )

    return _retarget_to_prompt(_DONE_PROMPT)


# ---------------------------------------------------------------------------
# Step 1.6b — From library (autocomplete search)
# ---------------------------------------------------------------------------

@router.get("/feedback/{pick_id}/game_search")
async def feedback_game_search(request: Request, pick_id: int, q: str = ""):
    """Autocomplete endpoint: returns HTML list of matching games."""
    with db.get_db() as conn:
        results = db.search_games_by_name(conn, q, limit=10)
    return templates.TemplateResponse(
        request, "partials/feedback_game_search_results.html", {
            "results": results,
            "pick_id": pick_id,
        }
    )


@router.post("/feedback/{pick_id}/step1_6_library")
async def feedback_step1_6_library(
    pick_id: int,
    actually_played_appid: Optional[int] = Form(None),
):
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
        if not pick:
            return _retarget_to_prompt(_CLOSE_PROMPT)

        if actually_played_appid:
            db.update_pick_outcome(conn, pick_id, actually_played_appid=actually_played_appid)

        picked_game = db.get_game_by_appid(conn, pick.appid)
        preferred_game = db.get_game_by_appid(conn, actually_played_appid) if actually_played_appid else None

        if picked_game:
            apply_did_not_play_affinity(
                conn, picked_game,
                pick.did_not_play_reason or "picked_another_game",
                preferred_game,
            )

    return _retarget_to_prompt(_DONE_PROMPT)


# ---------------------------------------------------------------------------
# Steps 2-4 (played path — unchanged logic from v2 initial)
# ---------------------------------------------------------------------------

@router.post("/feedback/{pick_id}/step2")
async def feedback_step2(
    request: Request,
    pick_id: int,
    rating: Optional[int] = Form(None),
    skip: bool = Form(False),
):
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
        if not pick:
            return _retarget_to_prompt(_CLOSE_PROMPT)

        if not skip and rating:
            games = db.get_games_with_state(conn)
            gws = next((g for g in games if g.game.appid == pick.appid), None)
            current_status = gws.state.status if gws else None
            outcome = _rating_to_outcome(rating, current_status)
            db.update_pick_outcome(conn, pick_id, outcome=outcome, rating=rating)
        else:
            db.update_pick_outcome(conn, pick_id, outcome="played_still_going")

    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
    return templates.TemplateResponse(request, "partials/feedback_step3.html", {
        "pick": pick,
    })


@router.post("/feedback/{pick_id}/step3")
async def feedback_step3(
    request: Request,
    pick_id: int,
    genre_match: Optional[int] = Form(None),
    skip: bool = Form(False),
):
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
        if not pick:
            return _retarget_to_prompt(_CLOSE_PROMPT)
        if not skip and genre_match:
            db.update_pick_outcome(conn, pick_id, genre_match_rating=genre_match)

    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
        candidate_appids = _parse_candidate_appids(pick.candidates_at_pick)
        other_appids = [a for a in candidate_appids if a != pick.appid]
        all_games = db.get_games_with_state(conn)
        other_candidates = [
            g for g in all_games
            if g.game.appid in other_appids
            and g.state.status != GameStatus.not_interested
        ]
        other_candidates.sort(key=lambda g: other_appids.index(g.game.appid))

    return templates.TemplateResponse(request, "partials/feedback_step4.html", {
        "pick": pick,
        "other_candidates": other_candidates,
    })


@router.post("/feedback/{pick_id}/step4")
async def feedback_step4(
    pick_id: int,
    answer: str = Form(...),
    retroactive_appid: Optional[int] = Form(None),
):
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
        if not pick:
            return _retarget_to_prompt(_CLOSE_PROMPT)

        other_appid = None
        if answer == "yes" and retroactive_appid:
            other_appid = retroactive_appid
            db.update_pick_outcome(conn, pick_id, would_have_picked_other_appid=other_appid)

        played_game = db.get_game_by_appid(conn, pick.appid)
        other_game = db.get_game_by_appid(conn, other_appid) if other_appid else None

        if played_game:
            apply_affinity_update(
                conn,
                pick=pick,
                played_game=played_game,
                rating=pick.rating,
                genre_match_rating=pick.genre_match_rating,
                would_have_other_game=other_game,
            )

    return _retarget_to_prompt(_DONE_PROMPT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rating_to_outcome(rating: int, game_status: Optional[GameStatus]) -> str:
    if rating <= 2:
        return "played_and_dropped"
    if rating == 3:
        return "played_still_going"
    if game_status == GameStatus.finished:
        return "played_and_finished"
    return "played_still_going"


def _parse_candidate_appids(candidates_json: str) -> list[int]:
    try:
        return [int(x) for x in json.loads(candidates_json)]
    except Exception:
        return []
