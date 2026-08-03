"""Migration-framework tests.

Run with: uv run python tests/test_migrations.py

Covers the PRAGMA user_version runner, the neutralized rating-scale
migration, and finished_at lifecycle.

No pytest dependency — pure assertions.

DATABASE SAFETY
---------------
Every test here writes to SQLite. A prior session destroyed data in the
real user database because a GAMEPILE_DATA_DIR env override silently did
nothing (app/config.py had no such override at the time) and the write
landed on the production path.

These tests therefore do NOT rely on config resolution at all. They patch
app.database.DB_PATH directly to a tempfile, and _isolated_db() asserts
the resolved path is inside the temp directory before yielding. If that
assertion ever fails the test aborts before opening a connection.
"""

import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import config
from app import database as db
from app.models import GameStatus


_TMP_ROOT = Path(tempfile.gettempdir()).resolve()


@contextmanager
def _isolated_db():
    """Yield a throwaway DB path with app.database.DB_PATH pointed at it.

    Guards against the production-write incident: the path is asserted to
    live under the system temp dir before anything opens a connection.
    """
    with tempfile.TemporaryDirectory(prefix="gamepile-test-") as tmp:
        path = Path(tmp).resolve() / "test.db"

        # Hard gate. Never proceed unless we are demonstrably in /tmp.
        assert _TMP_ROOT in path.parents, (
            f"REFUSING TO RUN: test DB path {path} is not under {_TMP_ROOT}"
        )
        real = Path(config.DB_PATH).resolve()
        assert path != real, f"REFUSING TO RUN: test path equals production DB {real}"

        with patch.object(db, "DB_PATH", path):
            assert Path(db.DB_PATH).resolve() == path
            yield path


def _seed_game(conn, appid=1, name="Test Game", playtime=0):
    conn.execute(
        "INSERT OR REPLACE INTO games (appid, name, playtime_minutes, last_refreshed) "
        "VALUES (?, ?, ?, ?)",
        (appid, name, playtime, datetime.utcnow().isoformat()),
    )


def _rating(conn, appid=1):
    return conn.execute(
        "SELECT personal_rating FROM game_state WHERE appid = ?", (appid,)
    ).fetchone()[0]


def _user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


# ---------------------------------------------------------------------------
# Database safety
# ---------------------------------------------------------------------------

def test_isolation_actually_redirects_db_path():
    """The harness must move DB_PATH off the production path. If this
    fails, every other test in this file is writing somewhere it should
    not be."""
    production = Path(config.DB_PATH).resolve()
    with _isolated_db() as path:
        assert Path(db.DB_PATH).resolve() == path
        assert Path(db.DB_PATH).resolve() != production
    # Restored after the context exits.
    assert Path(db.DB_PATH).resolve() == production


def test_data_dir_env_override_is_honored():
    """config._resolve_data_dir() must honor GAMEPILE_DATA_DIR.

    Exercised through the pure resolver rather than by reloading
    app.config, so this test cannot leak an overridden DATA_DIR into
    sibling suites sharing the process (run_tests.py imports them all
    into one interpreter).
    """
    with patch.dict("os.environ", {"GAMEPILE_DATA_DIR": "/tmp/gamepile-override-probe"}):
        assert config._resolve_data_dir() == Path("/tmp/gamepile-override-probe")
    # Unset → falls back to the platform location.
    with patch.dict("os.environ", {}, clear=True):
        assert config._resolve_data_dir().name == "gamepile"


# ---------------------------------------------------------------------------
# Rating scale — the regression that shipped in v0.9.9
# ---------------------------------------------------------------------------

def test_double_init_leaves_low_rating_unchanged():
    """THE regression test.

    v0.9.9-v0.9.11 doubled every personal_rating <= 5 on every startup.
    A user-set 1.0* (stored 2) became 4 after one restart and 8 after two.
    Running init_db() twice must leave a stored 2 at exactly 2.
    """
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            _seed_game(conn)
            db.set_personal_rating(conn, 1, 2)   # user sets 1.0 stars
        db.init_db()                              # simulated app restart
        with db.get_db() as conn:
            assert _rating(conn) == 2, f"rating drifted to {_rating(conn)}"


def test_many_restarts_leave_every_low_rating_unchanged():
    """Sweep the whole corrupted range (1-5 == 0.5*-2.5*) across several
    restarts. Pre-fix, every one of these doubled until it passed 5."""
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            for appid in range(1, 6):
                _seed_game(conn, appid=appid, name=f"Game {appid}")
                db.set_personal_rating(conn, appid, appid)  # 1..5
        for _ in range(5):
            db.init_db()
        with db.get_db() as conn:
            for appid in range(1, 6):
                assert _rating(conn, appid) == appid, (
                    f"appid {appid} drifted to {_rating(conn, appid)}"
                )


def test_high_ratings_also_unchanged():
    """Ratings above 5 were never in the doubling window, but verify the
    runner doesn't touch them either."""
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            _seed_game(conn)
            db.set_personal_rating(conn, 1, 10)
        db.init_db()
        with db.get_db() as conn:
            assert _rating(conn) == 10


def test_legacy_db_ratings_are_left_alone():
    """A database sitting at user_version 0 with a rating of 2 must come
    out of init_db() still at 2 — v1 is deliberately a no-op, because a
    pre-v0.9.9 database is indistinguishable from an already-migrated one.
    """
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            _seed_game(conn)
            db.set_personal_rating(conn, 1, 2)
            conn.execute("PRAGMA user_version = 0")   # pretend we never migrated
        db.init_db()
        with db.get_db() as conn:
            assert _rating(conn) == 2
            assert _user_version(conn) == db._SCHEMA_VERSION


# ---------------------------------------------------------------------------
# user_version bookkeeping
# ---------------------------------------------------------------------------

def test_user_version_set_on_fresh_db():
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            assert _user_version(conn) == db._SCHEMA_VERSION


def test_user_version_stable_across_repeated_init():
    with _isolated_db():
        db.init_db()
        db.init_db()
        db.init_db()
        with db.get_db() as conn:
            assert _user_version(conn) == db._SCHEMA_VERSION


def test_migrations_skipped_when_already_current():
    """At the current version the runner must short-circuit — no step runs."""
    with _isolated_db():
        db.init_db()
        called = []
        with patch.object(db, "_migrate_v2_backfill_finished_at",
                          lambda conn: called.append("v2")):
            db.init_db()
        assert called == [], f"migration re-ran at current version: {called}"


# ---------------------------------------------------------------------------
# finished_at lifecycle
# ---------------------------------------------------------------------------

def test_finished_at_set_on_transition_into_finished():
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            _seed_game(conn)
            db.update_game_state(conn, 1, status=GameStatus.finished, manually_set=True)
            gws = db.get_game_with_state_by_appid(conn, 1)
            assert gws.state.finished_at is not None


def test_finished_at_set_when_first_write_is_finished():
    """A game with no prior state row that goes straight to finished still
    gets a timestamp (INSERT branch, not UPDATE)."""
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            _seed_game(conn, appid=42, name="Straight To Done")
            assert conn.execute(
                "SELECT COUNT(*) FROM game_state WHERE appid = 42"
            ).fetchone()[0] == 0
            db.update_game_state(conn, 42, status=GameStatus.finished, manually_set=True)
            gws = db.get_game_with_state_by_appid(conn, 42)
            assert gws.state.finished_at is not None


def test_finished_at_not_moved_by_notes_edit():
    """The whole point of the column: editing a note must not re-date the
    completion. updated_at moves, finished_at does not."""
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            _seed_game(conn)
            db.update_game_state(conn, 1, status=GameStatus.finished, manually_set=True)
            original = db.get_game_with_state_by_appid(conn, 1).state.finished_at

        with db.get_db() as conn:
            db.set_notes(conn, 1, "Great ending.")
            after = db.get_game_with_state_by_appid(conn, 1).state
            assert after.finished_at == original, (
                f"finished_at moved: {original} -> {after.finished_at}"
            )
            assert after.updated_at >= original, "updated_at should still advance"


def test_finished_at_not_moved_by_rating_edit():
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            _seed_game(conn)
            db.update_game_state(conn, 1, status=GameStatus.finished, manually_set=True)
            original = db.get_game_with_state_by_appid(conn, 1).state.finished_at
        with db.get_db() as conn:
            db.set_personal_rating(conn, 1, 9)
            assert db.get_game_with_state_by_appid(conn, 1).state.finished_at == original


def test_finished_at_preserved_when_refinished():
    """Re-asserting 'finished' on an already-finished row keeps the
    original timestamp rather than stamping a new one."""
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            _seed_game(conn)
            db.update_game_state(conn, 1, status=GameStatus.finished, manually_set=True)
            original = db.get_game_with_state_by_appid(conn, 1).state.finished_at
        with db.get_db() as conn:
            db.update_game_state(conn, 1, status=GameStatus.finished, manually_set=True)
            assert db.get_game_with_state_by_appid(conn, 1).state.finished_at == original


def test_finished_at_cleared_when_leaving_finished():
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            _seed_game(conn)
            db.update_game_state(conn, 1, status=GameStatus.finished, manually_set=True)
            assert db.get_game_with_state_by_appid(conn, 1).state.finished_at is not None
            db.update_game_state(conn, 1, status=GameStatus.in_progress, manually_set=True)
            assert db.get_game_with_state_by_appid(conn, 1).state.finished_at is None


def test_finished_at_cleared_by_reset_to_inferred():
    """Reset re-runs inference, which never yields 'finished' — so the
    completion timestamp must go with it."""
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            _seed_game(conn, playtime=0)
            db.update_game_state(conn, 1, status=GameStatus.finished, manually_set=True)
            assert db.get_game_with_state_by_appid(conn, 1).state.finished_at is not None
            db.reset_status_to_inferred(conn, 1)
            assert db.get_game_with_state_by_appid(conn, 1).state.finished_at is None


def test_backfill_seeds_finished_at_from_updated_at():
    """Games already finished before the column existed get an approximate
    timestamp from updated_at."""
    with _isolated_db():
        db.init_db()
        stamp = (datetime.utcnow() - timedelta(days=90)).isoformat()
        with db.get_db() as conn:
            _seed_game(conn)
            # Simulate a pre-v2 row: finished, with no finished_at.
            conn.execute(
                "INSERT OR REPLACE INTO game_state (appid, status, manually_set, updated_at, finished_at) "
                "VALUES (1, 'finished', 1, ?, NULL)", (stamp,),
            )
            conn.execute("PRAGMA user_version = 1")   # pre-finished_at version
        db.init_db()
        with db.get_db() as conn:
            got = db.get_game_with_state_by_appid(conn, 1).state.finished_at
            assert got is not None, "backfill did not populate finished_at"
            assert abs((got - datetime.fromisoformat(stamp)).total_seconds()) < 1


def test_backfill_does_not_touch_unfinished_rows():
    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            _seed_game(conn)
            db.update_game_state(conn, 1, status=GameStatus.in_progress, manually_set=True)
            conn.execute("PRAGMA user_version = 1")
        db.init_db()
        with db.get_db() as conn:
            assert db.get_game_with_state_by_appid(conn, 1).state.finished_at is None


# ---------------------------------------------------------------------------
# Dashboard consumer
# ---------------------------------------------------------------------------

def test_finished_this_month_uses_finished_at_not_updated_at():
    """A game finished three months ago whose notes were edited today must
    NOT count toward this month. This was the bug finished_at fixes."""
    from app.dashboard import compute_finished_this_month

    with _isolated_db():
        db.init_db()
        old = (datetime.utcnow() - timedelta(days=90)).isoformat()
        with db.get_db() as conn:
            _seed_game(conn)
            conn.execute(
                "INSERT OR REPLACE INTO game_state "
                "(appid, status, manually_set, updated_at, finished_at) "
                "VALUES (1, 'finished', 1, ?, ?)", (old, old),
            )
        with db.get_db() as conn:
            db.set_notes(conn, 1, "late note")   # bumps updated_at to now
            games = db.get_games_with_state(conn)
        count = compute_finished_this_month(games, datetime.utcnow())
        assert count == 0, f"stale completion counted this month (got {count})"


def test_finished_this_month_counts_recent_completion():
    from app.dashboard import compute_finished_this_month

    with _isolated_db():
        db.init_db()
        with db.get_db() as conn:
            _seed_game(conn)
            db.update_game_state(conn, 1, status=GameStatus.finished, manually_set=True)
            games = db.get_games_with_state(conn)
        assert compute_finished_this_month(games, datetime.utcnow()) == 1


TESTS = [
    test_isolation_actually_redirects_db_path,
    test_data_dir_env_override_is_honored,
    test_double_init_leaves_low_rating_unchanged,
    test_many_restarts_leave_every_low_rating_unchanged,
    test_high_ratings_also_unchanged,
    test_legacy_db_ratings_are_left_alone,
    test_user_version_set_on_fresh_db,
    test_user_version_stable_across_repeated_init,
    test_migrations_skipped_when_already_current,
    test_finished_at_set_on_transition_into_finished,
    test_finished_at_set_when_first_write_is_finished,
    test_finished_at_not_moved_by_notes_edit,
    test_finished_at_not_moved_by_rating_edit,
    test_finished_at_preserved_when_refinished,
    test_finished_at_cleared_when_leaving_finished,
    test_finished_at_cleared_by_reset_to_inferred,
    test_backfill_seeds_finished_at_from_updated_at,
    test_backfill_does_not_touch_unfinished_rows,
    test_finished_this_month_uses_finished_at_not_updated_at,
    test_finished_this_month_counts_recent_completion,
]


def main() -> int:
    print(f"Running {len(TESTS)} test(s)…")
    failures = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  ✗ {fn.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ✗ {fn.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\n{failures}/{len(TESTS)} failed")
        return 1
    print(f"\nAll {len(TESTS)} tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
