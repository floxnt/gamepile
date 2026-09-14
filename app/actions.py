"""Shared state transitions and conflict-aware Undo for every action surface."""
import json
import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException
from app import database as db, taste
from app.models import GameStatus

MESSAGES = {
    'finished':'Marked finished', 'already_completed':'Marked finished',
    'confirm_finished':'Marked finished', 'mark_in_progress':'Marked in progress',
    'pick':'Marked in progress', 'bounced':'Marked bounced', 'not_my_thing':'Marked not my thing',
    'never_recommend':'Excluded from recommendations', 'restore':'Restored to recommendations',
    'pin':'Pinned to Shortlist', 'unpin':'Unpinned', 'clear_technical_issue':'Technical issue cleared',
}


def _snapshot(conn, appid, pick_id=None):
    db._ensure_state_row(conn, appid)
    state=dict(conn.execute('SELECT * FROM game_state WHERE appid=?',(appid,)).fetchone())
    row=conn.execute('SELECT * FROM taste_signals WHERE source=?',(f'quick:{appid}',)).fetchone()
    pick=conn.execute('SELECT * FROM pick_history WHERE id=?',(pick_id,)).fetchone() if pick_id else None
    return {'state':state,'signal':dict(row) if row else None,'pick_id':pick_id,'pick':dict(pick) if pick else None}


def record_undo(conn, appid, before, after):
    token=secrets.token_urlsafe(24)
    conn.execute('DELETE FROM action_undo WHERE created_at < ?',((datetime.utcnow()-timedelta(days=1)).isoformat(),))
    conn.execute('INSERT INTO action_undo VALUES (?,?,?,?,?)',
        (token,appid,json.dumps(before),json.dumps(after),datetime.utcnow().isoformat()))
    return token


def perform(conn, appid, action, pick_id=None):
    if action not in MESSAGES:
        raise HTTPException(400,'Unknown action')
    taste.write_lock(conn)
    game=db.get_game_by_appid(conn,appid)
    if game is None:
        raise HTTPException(404,'Game not found')
    if pick_id:
        pick=db.get_pick_history_by_id(conn,pick_id)
        if pick is None or pick.appid != appid:
            raise HTTPException(400,'Pick does not belong to this game')
    db._ensure_state_row(conn,appid)
    before=_snapshot(conn,appid,pick_id)
    if action in ('finished','already_completed','confirm_finished'):
        db.update_game_state(conn,appid,status=GameStatus.finished,manually_set=True)
        db.clear_pin(conn,appid)
        if action == 'confirm_finished':
            taste.set_signal(conn,f'quick:{appid}',appid,taste.labels(game,0.5))
        if pick_id:
            db.update_pick_outcome(conn,pick_id,outcome='played_and_finished')
    elif action in ('mark_in_progress','pick'):
        db.update_game_state(conn,appid,status=GameStatus.in_progress,manually_set=True)
        db.clear_pin(conn,appid)
        taste.set_signal(conn,f'quick:{appid}',appid,[])
    elif action in ('bounced','not_my_thing'):
        strong=action == 'not_my_thing'
        db.update_game_state(conn,appid,status=GameStatus.dropped,dropped_strength='strong' if strong else 'soft',manually_set=True)
        db.clear_pin(conn,appid)
        taste.set_signal(conn,f'quick:{appid}',appid,taste.labels(game,-1 if strong else -0.5))
        if pick_id:
            db.update_pick_outcome(conn,pick_id,outcome='played_and_dropped')
    elif action == 'never_recommend':
        db.update_game_state(conn,appid,blacklisted=True,manually_set=True)
        db.clear_pin(conn,appid)
    elif action == 'restore':
        db.update_game_state(conn,appid,blacklisted=False)
        state=db.get_game_with_state_by_appid(conn,appid).state
        if state.status == GameStatus.not_interested or (state.status == GameStatus.dropped and state.dropped_strength == 'strong'):
            db.reset_status_to_inferred(conn,appid)
            taste.set_signal(conn,f'quick:{appid}',appid,[])
    elif action == 'pin':
        db.set_pin(conn,appid)
    elif action == 'unpin':
        db.clear_pin(conn,appid)
    elif action == 'clear_technical_issue':
        db.update_game_state(conn,appid,has_technical_issue=False)
    after=_snapshot(conn,appid,pick_id)
    return record_undo(conn,appid,before,after)


def undo(conn, token):
    taste.write_lock(conn)
    row=conn.execute('SELECT * FROM action_undo WHERE token=?',(token,)).fetchone()
    if not row:
        raise HTTPException(409,'This action has already been undone or expired.')
    before,after=json.loads(row['before_json']),json.loads(row['after_json'])
    appid=row['appid']
    current=_snapshot(conn,appid,after['pick_id'])
    if current != after:
        raise HTTPException(409,'This game has changed since that action. Open its details to edit it.')
    state=before['state']
    columns=[k for k in state if k != 'appid']
    conn.execute('UPDATE game_state SET '+','.join(k+'=?' for k in columns)+' WHERE appid=?',
        [state[k] for k in columns]+[appid])
    signal=before['signal']
    taste.set_signal(conn,f'quick:{appid}',appid,json.loads(signal['contributions']) if signal else [])
    if signal:
        conn.execute('UPDATE taste_signals SET updated_at=? WHERE source=?',
            (signal['updated_at'], signal['source']))
    if before['pick_id']:
        if before['pick']:
            pick=before['pick']; columns=[k for k in pick if k != 'id']
            conn.execute('UPDATE pick_history SET '+','.join(k+'=?' for k in columns)+' WHERE id=?',
                [pick[k] for k in columns]+[pick['id']])
        else:
            conn.execute('DELETE FROM taste_signals WHERE source=?',(f"pick:{before['pick_id']}",))
            conn.execute('DELETE FROM pick_history WHERE id=?',(before['pick_id'],))
            taste.rebuild(conn)
    conn.execute('DELETE FROM action_undo WHERE token=?',(token,))
    return appid
