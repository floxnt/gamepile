"""Exercise real form submissions, response templates, and database effects."""
import asyncio
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import httpx
from fastapi.testclient import TestClient
from app import database as db, credentials, sync, actions, taste, prompt_state, decision_sessions
from app.main import app
from app.models import Game, GameStatus
from app.security import CSRF_TOKEN


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gamepile-workflow-")
        path = Path(self.tmp.name) / "test.db"
        assert path.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
        self.db_patch = patch.object(db, "DB_PATH", path)
        self.db_patch.start()
        self.creds_patch = patch.object(credentials, "has_complete_credentials", return_value=True)
        self.creds_patch.start()
        db.init_db()
        self.client = TestClient(app, base_url="http://127.0.0.1", headers={"X-GamePile-Token": CSRF_TOKEN})
        self.seed(1)
        prompt_state.skipped_appids.clear()
        prompt_state.skip_undo.clear()
        prompt_state._dismissed.clear()
        decision_sessions.sessions.clear()

    def tearDown(self):
        self.client.close()
        self.creds_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def seed(self, appid, **changes):
        values = dict(appid=appid, name=f"Fixture {appid}", playtime_minutes=120,
            last_played_steam=datetime.utcnow(), installed=None, hltb_main_hours=10,
            hltb_main_extra_hours=15, hltb_completionist_hours=20, genres="Action",
            tags="Single-player", user_tags="Action", developer="Fixture Studio",
            publisher=None, metacritic_score=80, opencritic_score=None,
            steam_review_pct=90, steam_review_count=100, last_refreshed=datetime.utcnow(),
            release_date=datetime.utcnow()-timedelta(days=45), game_type="linear",
            median_achievement_unlock_pct=50, user_achievement_pct=10)
        values.update(changes)
        with db.get_db() as conn:
            db.upsert_game(conn, Game(**values))
            db.ensure_game_state(conn, appid, playtime_minutes=120)

    def state(self, appid=1):
        with db.get_db() as conn:
            return db.get_game_with_state_by_appid(conn, appid)

    def test_actual_htmx_pick_form_preserves_context(self):
        self.seed(2)
        response = self.client.post('/games/1/pick', data={
            'mode':'comfort_pick', 'minutes':'180', 'candidates_at_pick':['1','2']})
        self.assertEqual(response.status_code, 200)
        with db.get_db() as conn:
            pick = db.get_most_recent_pick(conn)
        self.assertEqual(pick.mode, 'comfort_pick')
        self.assertIsNone(pick.time_window_minutes)
        self.assertEqual(json.loads(pick.candidates_at_pick), [1,2])

    def test_edit_forms_render_saved_values_and_work_again(self):
        for url, data, expected in [('/notes', {'notes':'Remember this'}, 'Remember this'),
                ('/hours_played_manual', {'hours':'7.5'}, '7.5')]:
            for _ in range(2):
                response = self.client.post('/games/1'+url, data=data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected, response.text)
                self.assertIn('/games/1'+url, response.text)
        self.assertEqual(self.state().state.notes, 'Remember this')
        self.assertEqual(self.state().state.hours_played_manual, 7.5)

    def test_api_rejects_untrusted_host_origin_and_missing_token(self):
        for headers, expected in [({'Host':'attacker.example'},400),
                ({'Origin':'https://attacker.example'},403), ({'X-GamePile-Token':''},403)]:
            response = self.client.post('/games/1/notes',data={'notes':'bad'},headers=headers)
            self.assertEqual(response.status_code, expected)
        self.assertIsNone(self.state().state.notes)
        native = self.client.post('/games/1/notes', data={'notes':'native', 'csrf_token':CSRF_TOKEN}, headers={'X-GamePile-Token':''})
        self.assertEqual(native.status_code,200)

    def test_enrichment_protects_intervening_manual_changes(self):
        snapshot=self.state().game
        with db.get_db() as conn:
            db.set_game_type(conn,1,'sandbox',manual=True)
            db.set_hltb_id_manual(conn,1,42,20,30,40)
        with db.get_db() as conn:
            db.apply_enrichment(conn,snapshot,{'game_type':'linear','hltb_main_hours':5,'steam_review_pct':95})
        game=self.state().game
        self.assertEqual((game.game_type,game.game_type_manual),('sandbox',True))
        self.assertEqual((game.hltb_id_manual,game.hltb_main_hours),(42,20))
        self.assertEqual(game.steam_review_pct,95)

    def test_source_clock_does_not_advance_when_source_skipped(self):
        old=datetime.utcnow()-timedelta(days=35)
        with db.get_db() as conn:
            conn.execute('UPDATE games SET hltb_fetched_at=? WHERE appid=1',(old.isoformat(),))
        snapshot=self.state().game
        with db.get_db() as conn:
            db.apply_enrichment(conn,snapshot,{'steam_review_pct':95})
        game=self.state().game
        self.assertEqual(game.hltb_fetched_at,old)
        self.assertTrue(sync._source_stale(game,game.hltb_fetched_at))

    def test_ambiguous_steam_response_keeps_owned_games(self):
        async def run():
            transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'response':{}},request=req))
            with patch.object(sync.steam_fetcher,'get_steam_api_key',return_value='fixture'), patch.object(sync.steam_fetcher,'get_steam_id',return_value='fixture'):
                async with httpx.AsyncClient(transport=transport) as client:
                    with self.assertRaises(ValueError):
                        await sync._phase_steam(client)
        asyncio.run(run())
        self.assertTrue(self.state().game.is_active)


    def affinity(self):
        with db.get_db() as conn:
            return db.get_all_affinities(conn)

    def pick(self):
        self.seed(2)
        response = self.client.post('/games/1/pick', data={
            'mode':'comfort_pick', 'candidates_at_pick':['1','2']})
        self.assertEqual(response.status_code, 200)
        with db.get_db() as conn:
            return db.get_most_recent_pick(conn).id

    def test_personal_rating_edits_replace_and_clear_their_signal(self):
        for value, expected in [(10,1),(10,1),(2,-1),(8,0.5)]:
            response=self.client.post('/games/1/rating',data={'rating':value})
            self.assertEqual(response.status_code,200)
            self.assertEqual(self.affinity()[('tag','action')],(expected,1))
        response=self.client.post('/games/1/rating',data={'clear':'1'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.affinity(),{})
        self.assertIsNone(self.state().state.personal_rating)

    def test_quick_feedback_has_confidence_and_explicit_rating_supersedes_it(self):
        for _ in range(2):
            self.assertEqual(self.client.post('/games/1/quick-action',data={'action':'bounced'}).status_code,200)
        self.assertEqual(self.affinity()[('tag','action')],(-0.5,1))
        self.client.post('/games/1/rating',data={'rating':10})
        self.assertEqual(self.affinity()[('tag','action')],(1,1))
        self.client.post('/games/1/rating',data={'clear':'1'})
        self.assertEqual(self.affinity()[('tag','action')],(-0.5,1))

    def test_feedback_saves_partial_resumes_and_retries_without_double_learning(self):
        pick_id=self.pick()
        self.assertEqual(self.client.get(f'/feedback/{pick_id}/step1').status_code,200)
        self.assertEqual(self.client.post(f'/feedback/{pick_id}/step2',data={'rating':5}).status_code,200)
        self.assertEqual(self.affinity()[('tag','action')],(1,1))
        with db.get_db() as conn:
            self.assertEqual(db.get_oldest_pending_pick(conn).id,pick_id)
        self.assertIn(f'/feedback/{pick_id}/step3',self.client.get('/').text)
        for _ in range(2):
            self.assertEqual(self.client.post(f'/feedback/{pick_id}/step3',data={'genre_match':4}).status_code,200)
            self.assertEqual(self.client.post(f'/feedback/{pick_id}/step4',data={'answer':'no'}).status_code,200)
            self.assertEqual(self.affinity()[('tag','action')],(1.25,1))
        with db.get_db() as conn:
            self.assertIsNone(db.get_oldest_pending_pick(conn))
        self.client.post(f'/feedback/{pick_id}/step2',data={'rating':1})
        self.assertEqual(self.affinity()[('tag','action')],(-0.75,1))

    def test_undo_restores_exact_state_taste_and_rejects_newer_edits(self):
        original=self.state().state
        with db.get_db() as conn:
            first=actions.perform(conn,1,'bounced')
            second=actions.perform(conn,1,'not_my_thing')
            actions.undo(conn,second)
            actions.undo(conn,first)
        self.assertEqual(self.state().state,original)
        self.assertEqual(self.affinity(),{})
        with db.get_db() as conn:
            token=actions.perform(conn,1,'never_recommend')
            db.set_notes(conn,1,'A newer edit')
        response=self.client.post('/actions/undo',data={'token':token})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.state().state.notes,'A newer edit')
        self.assertTrue(self.state().state.blacklisted)
        self.assertEqual(self.client.post('/games/1/restore').status_code,200)
        self.assertFalse(self.state().state.blacklisted)

    def test_not_feeling_it_is_session_only_and_undoable(self):
        original=self.state().state
        response=self.client.post('/games/1/state',data={'status':'not_interested'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.state().state,original)
        self.assertIn(1,prompt_state.skipped_appids)
        token=next(iter(prompt_state.skip_undo))
        self.assertEqual(self.client.post('/actions/undo',data={'token':token}).status_code,200)
        self.assertNotIn(1,prompt_state.skipped_appids)

    def test_decision_session_survives_details_undo_and_request_retry(self):
        from app.backlog import SECTION_TITLES
        self.seed(2)
        response=self.client.post('/backlog/session/start',data={'section_key':next(iter(SECTION_TITLES)),'appids_json':'[1,2]'})
        self.assertEqual(response.status_code,200)
        path=response.headers['HX-Redirect']
        session_id=path.rsplit('/',1)[1]
        self.assertIn(f'/games/1?session={session_id}',self.client.get(path).text)
        self.assertIn(path,self.client.get(f'/games/1?session={session_id}').text)
        data={'session_id':session_id,'index':0,'appid':1,'action':'skip'}
        for _ in range(2):
            self.assertEqual(self.client.post('/backlog/session/action',data=data).status_code,200)
            self.assertEqual(decision_sessions.sessions[session_id].index,1)
        self.assertIn(f'/games/2?session={session_id}',self.client.get(path).text)
        self.assertEqual(self.client.post(path+'/undo',data={'index':1}).status_code,200)
        self.assertEqual(decision_sessions.sessions[session_id].index,0)

    def test_existing_taste_migration_preserves_baseline_and_runs_once(self):
        with db.get_db() as conn:
            conn.execute("INSERT INTO affinity VALUES ('tag','Action',2.5,4,'2025-01-01')")
            db.set_personal_rating(conn,1,10)
            conn.execute('PRAGMA user_version=2')
        for _ in range(2):
            db.init_db()
            self.assertEqual(self.affinity()[('tag','action')],(3.5,5))
        self.client.post('/games/1/rating',data={'clear':'1'})
        self.assertEqual(self.affinity()[('tag','action')],(2.5,4))


def main():
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(WorkflowTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
