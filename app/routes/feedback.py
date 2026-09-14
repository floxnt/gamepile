"""Resumable feedback. Each pick owns one replaceable taste contribution."""
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from app import database as db, prompt_state, taste
from app.affinity import apply_affinity_update, apply_did_not_play_affinity
from app.models import GameStatus
from app.templates_config import templates

router=APIRouter()
_CLOSE_PROMPT='<div id="feedback-prompt"></div>'
_DONE_PROMPT='<div id="feedback-prompt" class="feedback-prompt feedback-prompt--done">Thanks — your feedback is saved.</div>'


def _retarget_to_prompt(html):
    response=HTMLResponse(html)
    response.headers['HX-Retarget']='#feedback-prompt'
    response.headers['HX-Reswap']='outerHTML'
    return response


def _pick(conn,pick_id):
    pick=db.get_pick_history_by_id(conn,pick_id)
    if pick is None:
        raise HTTPException(404,'Pick not found')
    return pick


def _complete(conn,pick_id):
    conn.execute('UPDATE pick_history SET feedback_completed_at=? WHERE id=?',(datetime.utcnow().isoformat(),pick_id))


def _game(conn,appid):
    if appid is None:
        return None
    game=db.get_game_by_appid(conn,appid)
    if game is None:
        raise HTTPException(400,'Selected game is not in this library')
    return game


def _save_played(conn,pick_id):
    pick=_pick(conn,pick_id)
    game=_game(conn,pick.appid)
    apply_affinity_update(conn,pick,game,pick.rating,pick.genre_match_rating,
        _game(conn,pick.would_have_picked_other_appid))


def _save_alternative(conn,pick_id,other_id=None):
    pick=_pick(conn,pick_id)
    apply_did_not_play_affinity(conn,_game(conn,pick.appid),pick.did_not_play_reason or 'picked_another_game',
        _game(conn,other_id),source=f'pick:{pick_id}')


def _candidates(conn,pick):
    ids=_parse_candidate_appids(pick.candidates_at_pick)
    ids=[appid for appid in ids if appid != pick.appid]
    games={g.game.appid:g for g in db.get_games_with_state(conn)}
    return [games[appid] for appid in ids if appid in games and games[appid].state.status != GameStatus.not_interested]


def resume_context(pick):
    # Partial steps are saved immediately; the completion marker stays unset
    # until the last step. Skipped answers may be asked again after leaving.
    if pick.outcome == 'did_not_play' and pick.did_not_play_reason in ('changed_mood','picked_another_game'):
        return 'partials/feedback_step1_6.html'
    if pick.outcome and pick.outcome.startswith('played_'):
        return 'partials/feedback_step3.html'
    return 'partials/feedback_step1.html'


@router.get('/feedback/{pick_id}',response_class=HTMLResponse)
@router.get('/feedback/{pick_id}/step1',response_class=HTMLResponse)
async def feedback_page(request:Request,pick_id:int):
    with db.get_db() as conn:
        pick=_pick(conn,pick_id)
    return templates.TemplateResponse(request,'feedback.html',{'pending_pick':pick,'pick':pick,
        'feedback_template':'partials/feedback_step1.html'})


@router.post('/feedback/{pick_id}/dismiss')
async def feedback_dismiss(pick_id:int):
    prompt_state.dismiss_for_session(pick_id)
    return _retarget_to_prompt(_CLOSE_PROMPT)


@router.post('/feedback/{pick_id}/skip-permanently')
async def feedback_skip_permanently(pick_id:int):
    with db.get_db() as conn:
        taste.write_lock(conn)
        _pick(conn,pick_id)
        db.update_pick_outcome(conn,pick_id,outcome='skipped_feedback')
        _complete(conn,pick_id)
    return _retarget_to_prompt(_CLOSE_PROMPT)


@router.post('/feedback/{pick_id}/step1')
async def feedback_step1(request:Request,pick_id:int,answer:str=Form(...)):
    if answer == 'skip':
        return await feedback_dismiss(pick_id)
    if answer not in ('yes','no'):
        raise HTTPException(400,'Unknown answer')
    with db.get_db() as conn:
        taste.write_lock(conn)
        pick=_pick(conn,pick_id)
        conn.execute('UPDATE pick_history SET feedback_completed_at=NULL WHERE id=?',(pick_id,))
        if answer == 'yes' and pick.outcome == 'did_not_play':
            conn.execute('UPDATE pick_history SET did_not_play_reason=NULL, actually_played_appid=NULL, would_have_picked_other_appid=NULL WHERE id=?',(pick_id,))
    return templates.TemplateResponse(request,'partials/feedback_step1_5.html' if answer == 'no' else 'partials/feedback_step2.html',{'pick':pick})


@router.post('/feedback/{pick_id}/step1_5')
async def feedback_step1_5(request:Request,pick_id:int,reason:str=Form(...)):
    if reason == 'skip':
        return await feedback_dismiss(pick_id)
    if reason not in ('no_time','changed_mood','picked_another_game','technical_issue'):
        raise HTTPException(400,'Unknown reason')
    with db.get_db() as conn:
        taste.write_lock(conn)
        pick=_pick(conn,pick_id)
        conn.execute('UPDATE pick_history SET rating=NULL,genre_match_rating=NULL,actually_played_appid=NULL,would_have_picked_other_appid=NULL,feedback_completed_at=NULL WHERE id=?',(pick_id,))
        db.update_pick_outcome(conn,pick_id,outcome='did_not_play',did_not_play_reason=reason)
        _save_alternative(conn,pick_id)
        if reason == 'technical_issue':
            db.update_game_state(conn,pick.appid,has_technical_issue=True)
        if reason in ('technical_issue','no_time'):
            _complete(conn,pick_id)
            return _retarget_to_prompt(_DONE_PROMPT)
        pick=_pick(conn,pick_id)
    return templates.TemplateResponse(request,'partials/feedback_step1_6.html',{'pick':pick})


@router.post('/feedback/{pick_id}/step1_6')
async def feedback_step1_6(request:Request,pick_id:int,sub_option:str=Form(...)):
    if sub_option not in ('from_candidates','from_library','not_in_app'):
        raise HTTPException(400,'Unknown answer')
    with db.get_db() as conn:
        taste.write_lock(conn)
        pick=_pick(conn,pick_id)
        if sub_option == 'not_in_app':
            _save_alternative(conn,pick_id)
            _complete(conn,pick_id)
            return _retarget_to_prompt(_DONE_PROMPT)
        candidates=_candidates(conn,pick)
    name='partials/feedback_step1_6_candidates.html' if sub_option == 'from_candidates' else 'partials/feedback_step1_6_library.html'
    return templates.TemplateResponse(request,name,{'pick':pick,'other_candidates':candidates})


@router.post('/feedback/{pick_id}/step1_6_candidates')
async def feedback_step1_6_candidates(pick_id:int,selected_appid:Optional[int]=Form(None)):
    with db.get_db() as conn:
        taste.write_lock(conn)
        pick=_pick(conn,pick_id)
        if selected_appid and (selected_appid == pick.appid or selected_appid not in _parse_candidate_appids(pick.candidates_at_pick)):
            raise HTTPException(400,'Game was not one of these recommendations')
        _game(conn,selected_appid)
        conn.execute('UPDATE pick_history SET would_have_picked_other_appid=? WHERE id=?',(selected_appid,pick_id))
        _save_alternative(conn,pick_id,selected_appid)
        _complete(conn,pick_id)
    return _retarget_to_prompt(_DONE_PROMPT)


@router.get('/feedback/{pick_id}/game_search')
async def feedback_game_search(request:Request,pick_id:int,q:str=''):
    with db.get_db() as conn:
        _pick(conn,pick_id)
        results=db.search_games_by_name(conn,q)
    return templates.TemplateResponse(request,'partials/feedback_game_search_results.html',{'results':results,'pick_id':pick_id})


@router.post('/feedback/{pick_id}/step1_6_library')
async def feedback_step1_6_library(pick_id:int,actually_played_appid:Optional[int]=Form(None)):
    with db.get_db() as conn:
        taste.write_lock(conn)
        pick = _pick(conn,pick_id)
        if actually_played_appid == pick.appid:
            raise HTTPException(400,'Choose the other game you played')
        _game(conn,actually_played_appid)
        conn.execute('UPDATE pick_history SET actually_played_appid=? WHERE id=?',(actually_played_appid,pick_id))
        _save_alternative(conn,pick_id,actually_played_appid)
        _complete(conn,pick_id)
    return _retarget_to_prompt(_DONE_PROMPT)


@router.post('/feedback/{pick_id}/step2')
async def feedback_step2(request:Request,pick_id:int,rating:Optional[int]=Form(None),skip:bool=Form(False)):
    if not skip and (rating is None or not 1 <= rating <= 5):
        raise HTTPException(400,'Rating must be 1–5')
    with db.get_db() as conn:
        taste.write_lock(conn)
        pick=_pick(conn,pick_id)
        status=db.get_game_with_state_by_appid(conn,pick.appid).state.status
        conn.execute('UPDATE pick_history SET rating=?,feedback_completed_at=NULL WHERE id=?',(None if skip else rating,pick_id))
        db.update_pick_outcome(conn,pick_id,outcome=_rating_to_outcome(rating,status) if not skip else 'played_still_going')
        _save_played(conn,pick_id)
        pick=_pick(conn,pick_id)
    return templates.TemplateResponse(request,'partials/feedback_step3.html',{'pick':pick})


@router.post('/feedback/{pick_id}/step3')
async def feedback_step3(request:Request,pick_id:int,genre_match:Optional[int]=Form(None),skip:bool=Form(False)):
    if not skip and (genre_match is None or not 1 <= genre_match <= 5):
        raise HTTPException(400,'Genre match must be 1–5')
    with db.get_db() as conn:
        taste.write_lock(conn)
        _pick(conn,pick_id)
        conn.execute('UPDATE pick_history SET genre_match_rating=? WHERE id=?',(None if skip else genre_match,pick_id))
        _save_played(conn,pick_id)
        pick=_pick(conn,pick_id)
        candidates=_candidates(conn,pick)
    return templates.TemplateResponse(request,'partials/feedback_step4.html',{'pick':pick,'other_candidates':candidates})


@router.post('/feedback/{pick_id}/step4')
async def feedback_step4(pick_id:int,answer:str=Form(...),retroactive_appid:Optional[int]=Form(None)):
    if answer not in ('yes','no','skip'):
        raise HTTPException(400,'Unknown answer')
    with db.get_db() as conn:
        taste.write_lock(conn)
        pick=_pick(conn,pick_id)
        other_id=retroactive_appid if answer == 'yes' else None
        if answer == 'yes' and (not other_id or other_id == pick.appid or other_id not in _parse_candidate_appids(pick.candidates_at_pick)):
            raise HTTPException(400,'Choose one of the other recommendations')
        _game(conn,other_id)
        conn.execute('UPDATE pick_history SET would_have_picked_other_appid=? WHERE id=?',(other_id,pick_id))
        _save_played(conn,pick_id)
        _complete(conn,pick_id)
    return _retarget_to_prompt(_DONE_PROMPT)


def _rating_to_outcome(rating,game_status):
    if rating <= 2:
        return 'played_and_dropped'
    if rating == 3:
        return 'played_still_going'
    return 'played_and_finished' if game_status == GameStatus.finished else 'played_still_going'


def _parse_candidate_appids(value):
    try:
        return [int(x) for x in json.loads(value)]
    except (ValueError,TypeError):
        return []
