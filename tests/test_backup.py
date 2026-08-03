"""Backup-export tests.

Run with: uv run python tests/test_backup.py

Covers the envelope shape, the exact column set per section, the sparse
game_overrides predicate, and timestamp passthrough.

No pytest dependency — pure assertions.

DATABASE SAFETY
---------------
Same discipline as tests/test_migrations.py: app.database.DB_PATH is
patched to a tempfile and the resolved path is asserted to live under
the system temp dir before any connection opens. A prior session
destroyed real user data by trusting an env override that silently did
nothing, so config resolution is never relied on here.
"""

import json
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import backup
from app import config
from app import database as db


_TMP_ROOT = Path(tempfile.gettempdir()).resolve()


@contextmanager
def _isolated_db():
    """Initialised throwaway DB with DB_PATH pointed at it."""
    with tempfile.TemporaryDirectory(prefix="gamepile-backup-test-") as tmp:
        path = Path(tmp).resolve() / "test.db"

        # Hard gate. Never proceed unless we are demonstrably in /tmp.
        assert _TMP_ROOT in path.parents, (
            f"REFUSING TO RUN: test DB path {path} is not under {_TMP_ROOT}"
        )
        real = Path(config.DB_PATH).resolve()
        assert path != real, f"REFUSING TO RUN: test path equals production DB {real}"

        with patch.object(db, "DB_PATH", path):
            assert Path(db.DB_PATH).resolve() == path
            db.init_db()
            yield path


def _seed_game(conn: sqlite3.Connection, appid: int, name: str) -> None:
    conn.execute(
        "INSERT INTO games (appid, name, last_refreshed) VALUES (?, ?, ?)",
        (appid, name, "2026-01-01T00:00:00"),
    )


def _seed_state(conn: sqlite3.Connection, appid: int, **kw) -> None:
    cols = {"appid": appid, "status": "never_played",
            "updated_at": "2026-01-01T00:00:00"}
    cols.update(kw)
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO game_state ({names}) VALUES ({marks})",
                 tuple(cols.values()))


# --- envelope ---------------------------------------------------------

def test_envelope_has_all_required_keys():
    with _isolated_db():
        with db.get_db() as conn:
            out = backup.build_backup(conn)
    expected = {"schema", "rating_scale", "exported_at", "app_version",
                "game_state", "game_overrides", "affinity", "picks"}
    assert set(out) == expected, f"envelope keys were {sorted(out)}"


def test_rating_scale_is_stamped():
    """Without this an importer silently halves or doubles every rating."""
    with _isolated_db():
        with db.get_db() as conn:
            out = backup.build_backup(conn)
    assert out["rating_scale"] == "0-10", out["rating_scale"]


def test_schema_version_is_an_int():
    with _isolated_db():
        with db.get_db() as conn:
            out = backup.build_backup(conn)
    assert isinstance(out["schema"], int), type(out["schema"])
    assert out["schema"] >= 1


def test_empty_db_produces_empty_sections_not_errors():
    with _isolated_db():
        with db.get_db() as conn:
            out = backup.build_backup(conn)
    for key in ("game_state", "game_overrides", "affinity", "picks"):
        assert out[key] == [], f"{key} was {out[key]!r}"


def test_output_is_json_serializable():
    with _isolated_db():
        with db.get_db() as conn:
            _seed_game(conn, 10, "Zed")
            _seed_state(conn, 10, notes="unicode: ✓ ümlaut", personal_rating=7)
            conn.commit()
            out = backup.build_backup(conn)
    reloaded = json.loads(backup.serialize(out))
    assert reloaded["game_state"][0]["notes"] == "unicode: ✓ ümlaut"


# --- game_state -------------------------------------------------------

def test_game_state_exports_exact_column_set():
    with _isolated_db():
        with db.get_db() as conn:
            _seed_game(conn, 10, "Zed")
            _seed_state(conn, 10)
            conn.commit()
            out = backup.build_backup(conn)
    expected = {"appid", "status", "hours_played_manual", "notes",
                "manually_set", "has_technical_issue", "blacklisted",
                "dropped_strength", "pinned_for_shortlist", "pinned_at",
                "personal_rating", "finished_at", "updated_at"}
    assert set(out["game_state"][0]) == expected, sorted(out["game_state"][0])


def test_game_state_values_round_trip():
    with _isolated_db():
        with db.get_db() as conn:
            _seed_game(conn, 10, "Zed")
            _seed_state(conn, 10, status="finished", personal_rating=9,
                        notes="great", blacklisted=1, hours_played_manual=12.5,
                        finished_at="2026-02-02T00:00:00")
            conn.commit()
            out = backup.build_backup(conn)
    row = out["game_state"][0]
    assert row["status"] == "finished", row["status"]
    assert row["personal_rating"] == 9, row["personal_rating"]
    assert row["notes"] == "great", row["notes"]
    assert row["blacklisted"] == 1, row["blacklisted"]
    assert row["hours_played_manual"] == 12.5, row["hours_played_manual"]
    assert row["finished_at"] == "2026-02-02T00:00:00", row["finished_at"]


def test_updated_at_is_passed_through_not_restamped():
    """Stamping export time onto updated_at would destroy the history it
    records and make a restore look like everything was touched at once."""
    original = "2020-05-05T05:05:05"
    with _isolated_db():
        with db.get_db() as conn:
            _seed_game(conn, 10, "Zed")
            _seed_state(conn, 10, updated_at=original)
            conn.commit()
            out = backup.build_backup(conn)
    assert out["game_state"][0]["updated_at"] == original, \
        out["game_state"][0]["updated_at"]


# --- game_overrides (sparse) ------------------------------------------

def test_game_type_override_carries_the_value_not_just_the_flag():
    """game_type_manual is only a boolean; the user's actual choice lives
    in game_type. Exporting the flag alone loses the choice."""
    with _isolated_db():
        with db.get_db() as conn:
            _seed_game(conn, 10, "Zed")
            conn.execute("UPDATE games SET game_type='rpg', game_type_manual=1 "
                         "WHERE appid=10")
            conn.commit()
            out = backup.build_backup(conn)
    assert len(out["game_overrides"]) == 1, out["game_overrides"]
    row = out["game_overrides"][0]
    assert row["game_type"] == "rpg", row
    assert row["game_type_manual"] == 1, row


def test_hltb_id_override_is_exported():
    with _isolated_db():
        with db.get_db() as conn:
            _seed_game(conn, 10, "Zed")
            conn.execute("UPDATE games SET hltb_id_manual=4242 WHERE appid=10")
            conn.commit()
            out = backup.build_backup(conn)
    assert len(out["game_overrides"]) == 1, out["game_overrides"]
    assert out["game_overrides"][0]["hltb_id_manual"] == 4242


def test_games_without_overrides_are_omitted():
    """Sparse by design — a NULL override on 650 fetched games is noise."""
    with _isolated_db():
        with db.get_db() as conn:
            _seed_game(conn, 10, "Overridden")
            _seed_game(conn, 20, "Untouched")
            conn.execute("UPDATE games SET game_type_manual=1 WHERE appid=10")
            conn.commit()
            out = backup.build_backup(conn)
    ids = [r["appid"] for r in out["game_overrides"]]
    assert ids == [10], ids


def test_auto_classified_game_type_is_not_an_override():
    """game_type set with manual=0 is a computed signal, not user data."""
    with _isolated_db():
        with db.get_db() as conn:
            _seed_game(conn, 10, "Zed")
            conn.execute("UPDATE games SET game_type='rpg', game_type_manual=0 "
                         "WHERE appid=10")
            conn.commit()
            out = backup.build_backup(conn)
    assert out["game_overrides"] == [], out["game_overrides"]


# --- affinity & picks -------------------------------------------------

def test_affinity_exports_exact_column_set():
    with _isolated_db():
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO affinity (kind, value, weight, pick_count, updated_at) "
                "VALUES ('genre', 'RPG', 0.75, 3, '2026-01-01T00:00:00')")
            conn.commit()
            out = backup.build_backup(conn)
    assert set(out["affinity"][0]) == {"kind", "value", "weight", "pick_count"}, \
        sorted(out["affinity"][0])
    assert out["affinity"][0]["weight"] == 0.75


def test_picks_are_exported_without_local_autoincrement_id():
    """Row ids are local and meaningless across installs."""
    with _isolated_db():
        with db.get_db() as conn:
            _seed_game(conn, 10, "Zed")
            conn.execute(
                "INSERT INTO pick_history (appid, game_name, picked_at, mode, "
                "candidates_at_pick) VALUES (10, 'Zed', '2026-03-03T00:00:00', "
                "'quick', '[]')")
            conn.commit()
            out = backup.build_backup(conn)
    assert len(out["picks"]) == 1, out["picks"]
    assert "id" not in out["picks"][0], sorted(out["picks"][0])
    assert out["picks"][0]["picked_at"] == "2026-03-03T00:00:00"


# --- fetched data must NOT leak --------------------------------------

def test_fetched_columns_are_not_exported():
    """The export is the user-authored layer only. Fetched Steam/HLTB data
    is reproducible by a refresh and would bloat the file for nothing."""
    with _isolated_db():
        with db.get_db() as conn:
            _seed_game(conn, 10, "Zed")
            conn.execute("UPDATE games SET hltb_id_manual=1 WHERE appid=10")
            conn.commit()
            out = backup.build_backup(conn)
    row = out["game_overrides"][0]
    for leaked in ("name", "playtime_minutes", "hltb_main_hours", "genres",
                   "tags", "user_tags", "metacritic_score", "steam_review_pct",
                   "completion_rate", "median_achievement_unlock_pct"):
        assert leaked not in row, f"fetched column {leaked!r} leaked into export"


# --- filename ---------------------------------------------------------

def test_filename_uses_dated_pattern():
    from datetime import datetime
    name = backup.filename(datetime(2026, 8, 3))
    assert name == "gamepile-backup-2026-08-03.json", name


# --- writing to disk --------------------------------------------------
#
# The export is written server-side because neither embedded webview
# completes a Content-Disposition download (see app/backup.py). These
# pin the behaviour the UI depends on.

def test_write_backup_creates_a_readable_file():
    with _isolated_db():
        with tempfile.TemporaryDirectory(prefix="gamepile-out-") as out:
            with db.get_db() as conn:
                _seed_game(conn, 10, "Zed")
                _seed_state(conn, 10, personal_rating=7)
                conn.commit()
                path = backup.write_backup(conn, target_dir=Path(out))
            assert path.exists(), f"no file at {path}"
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["game_state"][0]["personal_rating"] == 7


def test_write_backup_returns_an_absolute_path():
    """The UI has no save dialog — the returned path is the only way the
    user learns where the file went, so it must be fully resolved."""
    with _isolated_db():
        with tempfile.TemporaryDirectory(prefix="gamepile-out-") as out:
            with db.get_db() as conn:
                path = backup.write_backup(conn, target_dir=Path(out))
            assert path.is_absolute(), path


def test_second_export_same_day_does_not_overwrite_the_first():
    """Silently replacing an existing backup is the exact failure a
    backup feature exists to prevent."""
    with _isolated_db():
        with tempfile.TemporaryDirectory(prefix="gamepile-out-") as out:
            with db.get_db() as conn:
                first = backup.write_backup(conn, target_dir=Path(out))
                second = backup.write_backup(conn, target_dir=Path(out))
            assert first != second, f"both exports wrote to {first}"
            assert first.exists() and second.exists()
            assert second.name.endswith("-2.json"), second.name


def test_write_backup_creates_a_missing_directory():
    with _isolated_db():
        with tempfile.TemporaryDirectory(prefix="gamepile-out-") as out:
            nested = Path(out) / "does" / "not" / "exist"
            with db.get_db() as conn:
                path = backup.write_backup(conn, target_dir=nested)
            assert path.exists(), path


def test_write_backup_raises_on_unwritable_directory():
    """Must surface, not swallow — a backup that silently didn't happen
    is worse than no button at all."""
    with _isolated_db():
        with tempfile.TemporaryDirectory(prefix="gamepile-out-") as out:
            locked = Path(out) / "locked"
            locked.mkdir()
            locked.chmod(0o500)  # r-x: can traverse, cannot create
            try:
                with db.get_db() as conn:
                    try:
                        backup.write_backup(conn, target_dir=locked)
                    except OSError:
                        pass
                    else:
                        raise AssertionError("no OSError on unwritable dir")
            finally:
                locked.chmod(0o700)


def test_failed_write_leaves_no_partial_file():
    """A truncated file that looks like a valid backup is the worst
    possible outcome."""
    with _isolated_db():
        with tempfile.TemporaryDirectory(prefix="gamepile-out-") as out:
            target = Path(out)
            with db.get_db() as conn:
                with patch.object(backup, "serialize",
                                  side_effect=OSError("disk full")):
                    try:
                        backup.write_backup(conn, target_dir=target)
                    except OSError:
                        pass
            leftovers = list(target.iterdir())
            assert leftovers == [], f"left behind {leftovers}"


def test_default_target_dir_prefers_downloads():
    with tempfile.TemporaryDirectory(prefix="gamepile-home-") as home:
        downloads = Path(home) / "Downloads"
        downloads.mkdir()
        with patch.object(Path, "home", staticmethod(lambda: Path(home))):
            assert backup.default_target_dir() == downloads


def test_default_target_dir_falls_back_to_data_dir():
    """Downloads isn't guaranteed to exist; the data dir always is."""
    with tempfile.TemporaryDirectory(prefix="gamepile-home-") as home:
        with patch.object(Path, "home", staticmethod(lambda: Path(home))):
            assert backup.default_target_dir() == config.DATA_DIR


TESTS = [
    test_envelope_has_all_required_keys,
    test_rating_scale_is_stamped,
    test_schema_version_is_an_int,
    test_empty_db_produces_empty_sections_not_errors,
    test_output_is_json_serializable,
    test_game_state_exports_exact_column_set,
    test_game_state_values_round_trip,
    test_updated_at_is_passed_through_not_restamped,
    test_game_type_override_carries_the_value_not_just_the_flag,
    test_hltb_id_override_is_exported,
    test_games_without_overrides_are_omitted,
    test_auto_classified_game_type_is_not_an_override,
    test_affinity_exports_exact_column_set,
    test_picks_are_exported_without_local_autoincrement_id,
    test_fetched_columns_are_not_exported,
    test_filename_uses_dated_pattern,
    test_write_backup_creates_a_readable_file,
    test_write_backup_returns_an_absolute_path,
    test_second_export_same_day_does_not_overwrite_the_first,
    test_write_backup_creates_a_missing_directory,
    test_write_backup_raises_on_unwritable_directory,
    test_failed_write_leaves_no_partial_file,
    test_default_target_dir_prefers_downloads,
    test_default_target_dir_falls_back_to_data_dir,
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
