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
from app import (
    database as db,
    credentials,
    sync,
    actions,
    taste,
    prompt_state,
    decision_sessions,
    backup,
    backup_import,
)
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
        self.creds_patch = patch.object(
            credentials, "has_complete_credentials", return_value=True
        )
        self.creds_patch.start()
        db.init_db()
        self.client = TestClient(
            app, base_url="http://127.0.0.1", headers={"X-GamePile-Token": CSRF_TOKEN}
        )
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
        values = dict(
            appid=appid,
            name=f"Fixture {appid}",
            playtime_minutes=120,
            last_played_steam=datetime.utcnow(),
            installed=None,
            hltb_main_hours=10,
            hltb_main_extra_hours=15,
            hltb_completionist_hours=20,
            genres="Action",
            tags="Single-player",
            user_tags="Action",
            developer="Fixture Studio",
            publisher=None,
            metacritic_score=80,
            opencritic_score=None,
            steam_review_pct=90,
            steam_review_count=100,
            last_refreshed=datetime.utcnow(),
            release_date=datetime.utcnow() - timedelta(days=45),
            game_type="linear",
            median_achievement_unlock_pct=50,
            user_achievement_pct=10,
        )
        values.update(changes)
        with db.get_db() as conn:
            db.upsert_game(conn, Game(**values))
            db.ensure_game_state(conn, appid, playtime_minutes=120)

    def state(self, appid=1):
        with db.get_db() as conn:
            return db.get_game_with_state_by_appid(conn, appid)

    def test_actual_htmx_pick_form_preserves_context(self):
        self.seed(2)
        response = self.client.post(
            "/games/1/pick",
            data={
                "mode": "comfort_pick",
                "minutes": "180",
                "candidates_at_pick": ["1", "2"],
            },
        )
        self.assertEqual(response.status_code, 200)
        with db.get_db() as conn:
            pick = db.get_most_recent_pick(conn)
        self.assertEqual(pick.mode, "comfort_pick")
        self.assertIsNone(pick.time_window_minutes)
        self.assertEqual(json.loads(pick.candidates_at_pick), [1, 2])

    def test_edit_forms_render_saved_values_and_work_again(self):
        for url, data, expected in [
            ("/notes", {"notes": "Remember this"}, "Remember this"),
            ("/hours_played_manual", {"hours": "7.5"}, "7.5"),
        ]:
            for _ in range(2):
                response = self.client.post("/games/1" + url, data=data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected, response.text)
                self.assertIn("/games/1" + url, response.text)
        self.assertEqual(self.state().state.notes, "Remember this")
        self.assertEqual(self.state().state.hours_played_manual, 7.5)

    def test_api_rejects_untrusted_host_origin_and_missing_token(self):
        for headers, expected in [
            ({"Host": "attacker.example"}, 400),
            ({"Origin": "https://attacker.example"}, 403),
            ({"X-GamePile-Token": ""}, 403),
        ]:
            response = self.client.post(
                "/games/1/notes", data={"notes": "bad"}, headers=headers
            )
            self.assertEqual(response.status_code, expected)
        self.assertIsNone(self.state().state.notes)
        native = self.client.post(
            "/games/1/notes",
            data={"notes": "native", "csrf_token": CSRF_TOKEN},
            headers={"X-GamePile-Token": ""},
        )
        self.assertEqual(native.status_code, 200)

    def test_enrichment_protects_intervening_manual_changes(self):
        snapshot = self.state().game
        with db.get_db() as conn:
            db.set_game_type(conn, 1, "sandbox", manual=True)
            db.set_hltb_id_manual(conn, 1, 42, 20, 30, 40)
        with db.get_db() as conn:
            db.apply_enrichment(
                conn,
                snapshot,
                {"game_type": "linear", "hltb_main_hours": 5, "steam_review_pct": 95},
            )
        game = self.state().game
        self.assertEqual((game.game_type, game.game_type_manual), ("sandbox", True))
        self.assertEqual((game.hltb_id_manual, game.hltb_main_hours), (42, 20))
        self.assertEqual(game.steam_review_pct, 95)

    def test_source_clock_does_not_advance_when_source_skipped(self):
        old = datetime.utcnow() - timedelta(days=35)
        with db.get_db() as conn:
            conn.execute(
                "UPDATE games SET hltb_fetched_at=? WHERE appid=1", (old.isoformat(),)
            )
        snapshot = self.state().game
        with db.get_db() as conn:
            db.apply_enrichment(conn, snapshot, {"steam_review_pct": 95})
        game = self.state().game
        self.assertEqual(game.hltb_fetched_at, old)
        self.assertTrue(sync._source_stale(game, game.hltb_fetched_at))

    def test_ambiguous_steam_response_keeps_owned_games(self):
        async def run():
            transport = httpx.MockTransport(
                lambda req: httpx.Response(200, json={"response": {}}, request=req)
            )
            with (
                patch.object(
                    sync.steam_fetcher, "get_steam_api_key", return_value="fixture"
                ),
                patch.object(
                    sync.steam_fetcher, "get_steam_id", return_value="fixture"
                ),
            ):
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
        response = self.client.post(
            "/games/1/pick",
            data={"mode": "comfort_pick", "candidates_at_pick": ["1", "2"]},
        )
        self.assertEqual(response.status_code, 200)
        with db.get_db() as conn:
            return db.get_most_recent_pick(conn).id

    def test_personal_rating_edits_replace_and_clear_their_signal(self):
        for value, expected in [(10, 1), (10, 1), (2, -1), (8, 0.5)]:
            response = self.client.post("/games/1/rating", data={"rating": value})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.affinity()[("tag", "action")], (expected, 1))
        response = self.client.post("/games/1/rating", data={"clear": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.affinity(), {})
        self.assertIsNone(self.state().state.personal_rating)

    def test_quick_feedback_has_confidence_and_explicit_rating_supersedes_it(self):
        for _ in range(2):
            self.assertEqual(
                self.client.post(
                    "/games/1/quick-action", data={"action": "bounced"}
                ).status_code,
                200,
            )
        self.assertEqual(self.affinity()[("tag", "action")], (-0.5, 1))
        self.client.post("/games/1/rating", data={"rating": 10})
        self.assertEqual(self.affinity()[("tag", "action")], (1, 1))
        self.client.post("/games/1/rating", data={"clear": "1"})
        self.assertEqual(self.affinity()[("tag", "action")], (-0.5, 1))

    def test_feedback_saves_partial_resumes_and_retries_without_double_learning(self):
        pick_id = self.pick()
        self.assertEqual(self.client.get(f"/feedback/{pick_id}/step1").status_code, 200)
        self.assertEqual(
            self.client.post(
                f"/feedback/{pick_id}/step2", data={"rating": 5}
            ).status_code,
            200,
        )
        self.assertEqual(self.affinity()[("tag", "action")], (1, 1))
        with db.get_db() as conn:
            self.assertEqual(db.get_oldest_pending_pick(conn).id, pick_id)
        self.assertIn(f"/feedback/{pick_id}/step3", self.client.get("/").text)
        for _ in range(2):
            self.assertEqual(
                self.client.post(
                    f"/feedback/{pick_id}/step3", data={"genre_match": 4}
                ).status_code,
                200,
            )
            self.assertEqual(
                self.client.post(
                    f"/feedback/{pick_id}/step4", data={"answer": "no"}
                ).status_code,
                200,
            )
            self.assertEqual(self.affinity()[("tag", "action")], (1.25, 1))
        with db.get_db() as conn:
            self.assertIsNone(db.get_oldest_pending_pick(conn))
        self.client.post(f"/feedback/{pick_id}/step2", data={"rating": 1})
        self.assertEqual(self.affinity()[("tag", "action")], (-0.75, 1))

    def test_undo_restores_exact_state_taste_and_rejects_newer_edits(self):
        original = self.state().state
        with db.get_db() as conn:
            first = actions.perform(conn, 1, "bounced")
            second = actions.perform(conn, 1, "not_my_thing")
            actions.undo(conn, second)
            actions.undo(conn, first)
        self.assertEqual(self.state().state, original)
        self.assertEqual(self.affinity(), {})
        with db.get_db() as conn:
            token = actions.perform(conn, 1, "never_recommend")
            db.set_notes(conn, 1, "A newer edit")
        response = self.client.post("/actions/undo", data={"token": token})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.state().state.notes, "A newer edit")
        self.assertTrue(self.state().state.blacklisted)
        self.assertEqual(self.client.post("/games/1/restore").status_code, 200)
        self.assertFalse(self.state().state.blacklisted)

    def test_not_feeling_it_is_session_only_and_undoable(self):
        original = self.state().state
        response = self.client.post("/games/1/state", data={"status": "not_interested"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.state().state, original)
        self.assertIn(1, prompt_state.skipped_appids)
        token = next(iter(prompt_state.skip_undo))
        self.assertEqual(
            self.client.post("/actions/undo", data={"token": token}).status_code, 200
        )
        self.assertNotIn(1, prompt_state.skipped_appids)

    def test_decision_session_survives_details_undo_and_request_retry(self):
        from app.backlog import SECTION_TITLES

        self.seed(2)
        response = self.client.post(
            "/backlog/session/start",
            data={"section_key": next(iter(SECTION_TITLES)), "appids_json": "[1,2]"},
        )
        self.assertEqual(response.status_code, 200)
        path = response.headers["HX-Redirect"]
        session_id = path.rsplit("/", 1)[1]
        self.assertIn(f"/games/1?session={session_id}", self.client.get(path).text)
        self.assertIn(path, self.client.get(f"/games/1?session={session_id}").text)
        data = {"session_id": session_id, "index": 0, "appid": 1, "action": "skip"}
        for _ in range(2):
            self.assertEqual(
                self.client.post("/backlog/session/action", data=data).status_code, 200
            )
            self.assertEqual(decision_sessions.sessions[session_id].index, 1)
        self.assertIn(f"/games/2?session={session_id}", self.client.get(path).text)
        self.assertEqual(
            self.client.post(path + "/undo", data={"index": 1}).status_code, 200
        )
        self.assertEqual(decision_sessions.sessions[session_id].index, 0)

    def test_existing_taste_migration_preserves_baseline_and_runs_once(self):
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO affinity VALUES ('tag','Action',2.5,4,'2025-01-01')"
            )
            db.set_personal_rating(conn, 1, 10)
            conn.execute("PRAGMA user_version=2")
        for _ in range(2):
            db.init_db()
            self.assertEqual(self.affinity()[("tag", "action")], (3.5, 5))
        self.client.post("/games/1/rating", data={"clear": "1"})
        self.assertEqual(self.affinity()[("tag", "action")], (2.5, 4))

    def test_backup_round_trip_fresh_database_and_repeated_import(self):
        pick_id = self.pick()
        self.client.post("/games/1/rating", data={"rating": 9})
        self.client.post("/games/1/notes", data={"notes": "Keep this ✓"})
        self.client.post(f"/feedback/{pick_id}/step2", data={"rating": 4})
        with db.get_db() as conn:
            db.set_game_type(conn, 1, "sandbox", manual=True)
            db.set_hltb_id_manual(conn, 1, 42, 12, 20, 30)
            original = backup.build_backup(conn)
        data = backup_import.validate(backup.serialize(original).encode())
        expected = self.affinity()
        fresh = Path(self.tmp.name) / "restored.db"
        with patch.object(db, "DB_PATH", fresh):
            db.init_db()
            for _ in range(2):
                with db.get_db() as conn:
                    backup_import.merge(conn, data, "backup")
                    restored = backup.build_backup(conn)
                    self.assertEqual(db.get_all_affinities(conn), expected)
                    self.assertEqual(
                        conn.execute("PRAGMA foreign_key_check").fetchall(), []
                    )
                for key in (
                    "game_state",
                    "game_overrides",
                    "picks",
                    "taste_signals",
                    "affinity_base",
                ):
                    self.assertEqual(restored[key], original[key], key)
            self.assertEqual(self.state().state.notes, "Keep this ✓")
            self.assertFalse(self.state().game.is_active)

    def test_import_remaps_pick_ids_and_respects_conflict_policy(self):
        pick_id = self.pick()
        self.client.post(f"/feedback/{pick_id}/step2", data={"rating": 5})
        self.client.post("/games/1/notes", data={"notes": "From backup"})
        with db.get_db() as conn:
            data = backup_import.validate(backup.serialize(backup.build_backup(conn)))
            conn.execute("DELETE FROM taste_signals")
            conn.execute("DELETE FROM pick_history")
            db.set_notes(conn, 1, "Keep local")
            # Force imported feedback onto a different local integer ID.
            conn.execute("UPDATE sqlite_sequence SET seq=99 WHERE name='pick_history'")
        with db.get_db() as conn:
            backup_import.merge(conn, data, "local")
            self.assertEqual(db.get_most_recent_pick(conn).id, 100)
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM taste_signals WHERE source='pick:100'"
                ).fetchone()
            )
        self.assertEqual(self.state().state.notes, "Keep local")
        with db.get_db() as conn:
            backup_import.merge(conn, data, "backup")
        self.assertEqual(self.state().state.notes, "From backup")
        self.assertEqual(self.affinity()[("tag", "action")], (1, 1))

    def test_import_preview_does_not_write_and_rejects_stale_confirmation(self):
        with db.get_db() as conn:
            exported = backup.serialize(backup.build_backup(conn)).encode()
            before = backup_import.fingerprint(conn)
        response = self.client.post(
            "/settings/import/preview",
            files={"backup_file": ("fixture.json", exported, "application/json")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Review this backup", response.text)
        token = re.search(r'name="token" value="([^"]+)"', response.text).group(1)
        with db.get_db() as conn:
            self.assertEqual(before, backup_import.fingerprint(conn))
            db.set_notes(conn, 1, "After preview")
        response = self.client.post(
            "/settings/import/apply", data={"token": token, "policy": "backup"}
        )
        self.assertIn("changed after this preview", response.text)
        self.assertEqual(self.state().state.notes, "After preview")

    def test_import_confirmation_creates_recovery_backup_and_is_consumed(self):
        with db.get_db() as conn:
            data = backup_import.validate(backup.serialize(backup.build_backup(conn)))
            data["game_state"][0]["notes"] = "Restored"
            token, _ = backup_import.prepare(conn, data)
        response = self.client.post(
            "/settings/import/apply", data={"token": token, "policy": "backup"}
        )
        self.assertIn("Backup imported", response.text)
        self.assertEqual(self.state().state.notes, "Restored")
        saved = list((Path(self.tmp.name) / "backups").glob("*.json"))
        self.assertEqual(len(saved), 1)
        self.assertIsNone(json.loads(saved[0].read_text())["game_state"][0]["notes"])
        self.assertIn(
            "expired",
            self.client.post(
                "/settings/import/apply", data={"token": token, "policy": "backup"}
            ).text,
        )

    def test_import_rejects_invalid_or_incompatible_data_without_changes(self):
        with db.get_db() as conn:
            original = backup.build_backup(conn)
            before = backup_import.fingerprint(conn)
        variants = []
        for path, value in [("schema", 999), ("rating_scale", "0-5")]:
            changed = json.loads(json.dumps(original))
            changed[path] = value
            variants.append(changed)
        for field, value in [
            ("personal_rating", 11),
            ("hours_played_manual", float("nan")),
            ("status", "bogus"),
        ]:
            changed = json.loads(json.dumps(original))
            changed["game_state"][0][field] = value
            variants.append(changed)
        changed = json.loads(json.dumps(original))
        changed["game_state"].append(changed["game_state"][0])
        variants.append(changed)
        for changed in variants:
            with self.assertRaises(backup_import.ImportError):
                backup_import.validate(json.dumps(changed))
        with self.assertRaises(backup_import.ImportError):
            backup_import.validate(b"x" * (backup_import.MAX_BYTES + 1))
        with db.get_db() as conn:
            self.assertEqual(before, backup_import.fingerprint(conn))

    def test_legacy_backup_preserves_taste_without_replaying_archived_feedback(self):
        pick_id = self.pick()
        with db.get_db() as conn:
            db.update_pick_outcome(
                conn, pick_id, outcome="played_still_going", rating=5
            )
            data = backup.build_backup(conn)
        data["schema"] = 1
        for key in ("catalog", "affinity_base", "taste_signals"):
            data.pop(key)
        for pick in data["picks"]:
            for key in (
                "key",
                "candidates_at_pick",
                "feedback_completed_at",
                "legacy_taste_recorded",
            ):
                pick.pop(key)
        data["affinity"] = [
            {"kind": "tag", "value": "Action", "weight": 2.0, "pick_count": 3}
        ]
        validated = backup_import.validate(backup.serialize(data))
        with db.get_db() as conn:
            backup_import.merge(conn, validated, "backup")
        self.assertEqual(self.affinity()[("tag", "action")], (2, 3))
        response = self.client.post(f"/feedback/{pick_id}/step4", data={"answer": "no"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.affinity()[("tag", "action")], (2, 3))

    def test_failed_import_rolls_back_all_changes(self):
        with db.get_db() as conn:
            data = backup_import.validate(backup.serialize(backup.build_backup(conn)))
            before = backup_import.fingerprint(conn)
        data["game_state"][0]["notes"] = "Should roll back"
        with patch.object(
            taste, "rebuild", side_effect=RuntimeError("fixture failure")
        ):
            with self.assertRaises(RuntimeError):
                with db.get_db() as conn:
                    backup_import.merge(conn, data, "backup")
        with db.get_db() as conn:
            self.assertEqual(before, backup_import.fingerprint(conn))

    def test_hltb_provenance_tracks_selected_record_and_clears_old_similarity(self):
        snapshot = self.state().game
        with db.get_db() as conn:
            db.apply_enrichment(
                conn,
                snapshot,
                {
                    "hltb_match_id": 123,
                    "hltb_match_name": "Correct match",
                    "hltb_match_similarity": 0.85,
                    "hltb_fetched_at": datetime.utcnow(),
                },
            )
        page = self.client.get("/games/1").text
        self.assertIn("howlongtobeat.com/game/123", page)
        self.assertIn("85% title similarity", page)
        with db.get_db() as conn:
            db.set_hltb_id_manual(conn, 1, 42, 10, 20, 30, matched_name="Manual match")
        game = self.state().game
        self.assertEqual(game.hltb_match_id, 42)
        self.assertIsNone(game.hltb_match_similarity)
        self.assertIn("Manual match", self.client.get("/games/1").text)

    def test_reset_clears_temporary_skips_and_pages_render_with_local_assets(self):
        self.client.post("/games/1/state")
        self.assertIn(1, prompt_state.skipped_appids)
        response = self.client.post("/picks/reset?mode=comfort_pick&minutes=90")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(prompt_state.skipped_appids, set())
        for path in (
            "/",
            "/backlog",
            "/library",
            "/dashboard",
            "/settings",
            "/games/1",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("/static/vendor/htmx-1.9.12.js", response.text)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(
            self.client.get("/static/vendor/htmx-1.9.12.js").status_code, 200
        )

    def test_invalid_game_edits_are_404_and_cannot_create_orphan_state(self):
        for path, data in [
            ("/notes", {"notes": "Missing"}),
            ("/hours_played_manual", {"hours": 10}),
        ]:
            response = self.client.post("/games/999" + path, data=data)
            self.assertEqual(response.status_code, 404)
        with db.get_db() as conn:
            self.assertIsNone(
                conn.execute("SELECT * FROM game_state WHERE appid=999").fetchone()
            )

    # --- Regressions found reviewing 1.1.0 in a real browser -------------

    def test_pages_do_not_force_null_origin_on_native_forms(self):
        # Under "no-referrer", Chromium webviews send `Origin: null` on native
        # form posts, which the origin check rejects (setup wizard broke).
        page = self.client.get("/settings")
        self.assertNotEqual(page.headers["Referrer-Policy"], "no-referrer")
        same_origin = self.client.post(
            "/games/1/notes",
            data={"notes": "native", "csrf_token": CSRF_TOKEN},
            headers={"X-GamePile-Token": "", "Origin": "http://127.0.0.1"},
        )
        self.assertEqual(same_origin.status_code, 200)
        self.assertEqual(self.state().state.notes, "native")

    def test_session_container_disables_buttons_with_a_matching_selector(self):
        # hx-disabled-elt is inherited by each button. A "find ..." selector
        # resolves inside the clicked button, matches nothing, and HTMX 1.9
        # throws before sending the request.
        self.seed(2)
        response = self.client.post(
            "/backlog/session/start",
            data={"section_key": "in_progress", "appids_json": "[1, 2]"},
        )
        page = self.client.get(response.headers["HX-Redirect"]).text
        container = re.search(r'<section[^>]*id="session-content"[^>]*>', page).group(0)
        disabled = re.search(r'hx-disabled-elt="([^"]*)"', container)
        if disabled:
            self.assertFalse(disabled.group(1).startswith("find "), container)

    def test_time_box_outside_field_limits_still_records_pick(self):
        self.seed(2)
        page = self.client.get(
            "/picks", params={"minutes": 500, "mode": "i_only_have_tonight"}
        ).text
        values = json.loads(re.search(r"hx-vals='(\{\"candidates_at_pick\"[^']+)'", page).group(1))
        self.assertEqual(values["minutes"], 480)
        response = self.client.post(
            f"/games/{values['candidates_at_pick'][0]}/pick",
            data={
                "mode": values["mode"],
                "minutes": str(values["minutes"]),
                "candidates_at_pick": [str(v) for v in values["candidates_at_pick"]],
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_real_refresh_loop_honors_source_clocks_and_override_revisions(self):
        # The refresh reads snapshots through get_games_with_state(); 1.1.0's
        # clock tests used the single-game loader and missed that the bulk
        # query didn't select the new columns.
        from app.fetchers.hltb import HltbResult

        calls = []

        async def nothing(*args, **kwargs):
            return None

        def counted(name, value):
            async def fetch(*args, **kwargs):
                calls.append(name)
                return value
            return fetch

        found = HltbResult(found=True, hltb_main_hours=33.0, hltb_main_extra_hours=44.0,
                           hltb_completionist_hours=55.0, matched_name="Fixture", matched_id=9)
        spy = type("Spy", (), {"user_tags": [("Action", 1)]})()
        with db.get_db() as conn:  # what importing a backup does to an HLTB override
            db.set_hltb_id_manual(conn, 1, 9, None, None, None)
            conn.execute("UPDATE games SET hltb_fetched_at=NULL WHERE appid=1")
        with (
            patch.object(sync.steam_fetcher, "fetch_app_details", nothing),
            patch.object(sync.steam_fetcher, "fetch_review_data", nothing),
            patch.object(sync.hltb_fetcher, "fetch_hltb", counted("hltb", found)),
            patch.object(sync.hltb_fetcher, "fetch_hltb_by_id", counted("hltb", found)),
            patch.object(sync.steamspy_fetcher, "fetch_steamspy_data", counted("tags", spy)),
            patch.object(sync.achievements_fetcher, "fetch_global_achievement_percentages",
                         counted("achievements", [{"percent": 30.0}])),
            patch.object(sync.achievements_fetcher, "fetch_player_achievement_pct",
                         counted("mine", 40.0)),
        ):
            asyncio.run(sync._phase_enrich(None))
            self.assertEqual(self.state().game.hltb_main_hours, 33.0)
            calls.clear()
            asyncio.run(sync._phase_enrich(None))
        self.assertEqual(calls, [], "cached sources were fetched again")

    def test_backup_with_case_variant_taste_labels_round_trips(self):
        # Steam developer strings vary in case across games; 1.0 kept each
        # spelling as its own affinity row, and 1.1 copied them into the base.
        with db.get_db() as conn:
            conn.executemany(
                "INSERT INTO affinity_base VALUES (?,?,?,?)",
                [("developer", "Square Enix", 0.2, 1), ("developer", "SQUARE ENIX", 1.0, 1),
                 ("tag", "RPG", 2.0, 3)],
            )
            taste.write_lock(conn)
            taste.rebuild(conn)
            original = backup.build_backup(conn)
        expected = self.affinity()
        legacy = json.loads(json.dumps(original))
        legacy["schema"] = 1
        legacy["affinity"] = legacy["affinity_base"]
        for key in ("catalog", "affinity_base", "taste_signals"):
            legacy.pop(key)
        for pick in legacy["picks"]:
            for key in ("key", "candidates_at_pick", "feedback_completed_at", "legacy_taste_recorded"):
                pick.pop(key)
        for label, payload in [("1.1 export", original), ("1.0 export", legacy)]:
            data = backup_import.validate(backup.serialize(payload))
            fresh = Path(self.tmp.name) / f"restored-{label[:3]}.db"
            with patch.object(db, "DB_PATH", fresh):
                db.init_db()
                for _ in range(2):  # re-import is idempotent
                    with db.get_db() as conn:
                        backup_import.merge(conn, data, "backup")
                        self.assertEqual(db.get_all_affinities(conn), expected, label)
                        self.assertEqual(
                            backup.build_backup(conn)["affinity_base"],
                            original["affinity_base"],
                            label,
                        )


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(WorkflowTests)
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
