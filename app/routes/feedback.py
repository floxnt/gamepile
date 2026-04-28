"""
Feedback flow — v2.

Four HTMX-driven steps rendered inside a dismissible card on /.
Each step POST replaces #feedback-step-container (hx-swap="innerHTML").
Terminal responses (close card) override the target via HX-Retarget to
#feedback-prompt and swap outerHTML.

Step 1: Did you play it?
Step 2: How was it? (1-5)
Step 3: Was the genre/style a good match? (1-5)
Step 4: Would you have picked a different one?

Skip permanently: sets outcome=skipped_feedback, available at every step.
Dismiss for session: hides prompt until next app launch, no DB change.
"""

import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from typing import Optional

from app import database as db
from app import prompt_state
from app.affinity import apply_affinity_update
from app.models import GameStatus
from app.templates_config import templates

router = APIRouter()
log = logging.getLogger(__name__)

# Sentinel HTML used whenever we want to close the feedback prompt card entirely.
_CLOSE_PROMPT = '<div id="feedback-prompt"></div>'


def _retarget_to_prompt(html: str) -> HTMLResponse:
    """Return a response that replaces #feedback-prompt instead of the step container."""
    resp = HTMLResponse(html)
    resp.headers["HX-Retarget"] = "#feedback-prompt"
    resp.headers["HX-Reswap"] = "outerHTML"
    return resp


@router.post("/feedback/{pick_id}/dismiss")
async def feedback_dismiss(pick_id: int):
    """Hide the prompt for this session without changing the outcome."""
    prompt_state.dismiss_for_session(pick_id)
    return _retarget_to_prompt(_CLOSE_PROMPT)


@router.post("/feedback/{pick_id}/skip-permanently")
async def feedback_skip_permanently(pick_id: int):
    """Set outcome=skipped_feedback; never re-prompt for this pick."""
    with db.get_db() as conn:
        db.update_pick_outcome(conn, pick_id, outcome="skipped_feedback")
    return _retarget_to_prompt(_CLOSE_PROMPT)


@router.post("/feedback/{pick_id}/step1")
async def feedback_step1(
    request: Request,
    pick_id: int,
    answer: str = Form(...),
):
    """
    answer: "yes" | "no" | "skip"
    yes  → show step 2
    no   → set did_not_play, close prompt
    skip → dismiss for session, close prompt
    """
    if answer == "skip":
        prompt_state.dismiss_for_session(pick_id)
        return _retarget_to_prompt(_CLOSE_PROMPT)

    if answer == "no":
        with db.get_db() as conn:
            db.update_pick_outcome(conn, pick_id, outcome="did_not_play")
        return _retarget_to_prompt(_CLOSE_PROMPT)

    # "yes" → step 2
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
    if not pick:
        return _retarget_to_prompt(_CLOSE_PROMPT)

    return templates.TemplateResponse(request, "partials/feedback_step2.html", {
        "pick": pick,
    })


@router.post("/feedback/{pick_id}/step2")
async def feedback_step2(
    request: Request,
    pick_id: int,
    rating: Optional[int] = Form(None),
    skip: bool = Form(False),
):
    """
    Save overall rating (1-5) and derive preliminary outcome.
    skip → outcome = played_still_going, no rating saved.
    """
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
        if not pick:
            return _retarget_to_prompt(_CLOSE_PROMPT)

        if not skip and rating:
            # Check game_state to refine the outcome for 4-5 ratings.
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
    """Save genre/style match rating (1-5), then show step 4."""
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
        if not pick:
            return _retarget_to_prompt(_CLOSE_PROMPT)

        if not skip and genre_match:
            db.update_pick_outcome(conn, pick_id, genre_match_rating=genre_match)

    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
        # Load the other 4 candidates (skip not_interested games; use current state).
        candidate_appids = _parse_candidate_appids(pick.candidates_at_pick)
        other_appids = [a for a in candidate_appids if a != pick.appid]
        all_games = db.get_games_with_state(conn)
        other_candidates = [
            g for g in all_games
            if g.game.appid in other_appids
            and g.state.status != GameStatus.not_interested
        ]
        # Preserve the original shown order.
        other_candidates.sort(key=lambda g: other_appids.index(g.game.appid))

    return templates.TemplateResponse(request, "partials/feedback_step4.html", {
        "pick": pick,
        "other_candidates": other_candidates,
    })


@router.post("/feedback/{pick_id}/step4")
async def feedback_step4(
    request: Request,
    pick_id: int,
    answer: str = Form(...),
    retroactive_appid: Optional[int] = Form(None),
):
    """
    answer: "no" | "yes" | "skip"
    If yes and retroactive_appid provided: save and apply -0.3 to played game,
    +0.3 to preferred game.
    Then apply the full affinity update and close the prompt.
    """
    with db.get_db() as conn:
        pick = db.get_pick_history_by_id(conn, pick_id)
        if not pick:
            return _retarget_to_prompt(_CLOSE_PROMPT)

        other_appid = None
        if answer == "yes" and retroactive_appid:
            other_appid = retroactive_appid
            db.update_pick_outcome(conn, pick_id, would_have_picked_other_appid=other_appid)

        # Load games for affinity update.
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

    # Return empty prompt div — the spec confirmation message is brief enough
    # to put inline rather than in a separate template.
    return _retarget_to_prompt(
        '<div id="feedback-prompt" class="feedback-prompt feedback-prompt--done">'
        "Thanks — recommendations will adjust based on your feedback."
        "</div>"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rating_to_outcome(rating: int, game_status: Optional[GameStatus]) -> str:
    if rating <= 2:
        return "played_and_dropped"
    if rating == 3:
        return "played_still_going"
    # 4 or 5
    if game_status == GameStatus.finished:
        return "played_and_finished"
    return "played_still_going"


def _parse_candidate_appids(candidates_json: str) -> list[int]:
    try:
        return [int(x) for x in json.loads(candidates_json)]
    except Exception:
        return []
