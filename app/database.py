import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Generator, Optional

from app.config import DB_PATH
from app.models import Game, GameState, GameStatus, GameWithState, RefreshLog


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS games (
                appid                   INTEGER PRIMARY KEY,
                name                    TEXT NOT NULL,
                playtime_minutes        INTEGER NOT NULL DEFAULT 0,
                last_played_steam       TEXT,
                installed               INTEGER,
                hltb_main_hours         REAL,
                hltb_main_extra_hours   REAL,
                hltb_completionist_hours REAL,
                genres                  TEXT NOT NULL DEFAULT '',
                tags                    TEXT NOT NULL DEFAULT '',
                developer               TEXT,
                publisher               TEXT,
                metacritic_score        INTEGER,
                opencritic_score        INTEGER,
                steam_review_pct        INTEGER,
                steam_review_count      INTEGER,
                last_refreshed          TEXT NOT NULL,
                is_active               INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS game_state (
                appid               INTEGER PRIMARY KEY REFERENCES games(appid),
                status              TEXT NOT NULL DEFAULT 'never_played',
                hours_played_manual REAL,
                notes               TEXT,
                updated_at          TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS refresh_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at      TEXT NOT NULL,
                completed_at    TEXT,
                games_added     INTEGER NOT NULL DEFAULT 0,
                games_updated   INTEGER NOT NULL DEFAULT 0,
                errors          TEXT
            );
        """)

        # --- Migrations ---
        # TODO: for v3+, replace try/except ALTER TABLE with a proper migration
        #       system using a schema_version table that tracks applied migrations.
        try:
            conn.execute(
                "ALTER TABLE game_state ADD COLUMN manually_set BOOLEAN NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # column already exists

        # Backfill: fix rows stuck at never_played despite having Steam playtime.
        # Only touches manually_set=0 rows — user-set statuses are preserved.
        _backfill_inferred_statuses(conn)


# ---------------------------------------------------------------------------
# Status inference
# ---------------------------------------------------------------------------

def infer_status(playtime_minutes: int, hltb_main_hours: Optional[float]) -> "GameStatus":
    """
    Derive a status from raw Steam playtime and HLTB data.

    Rules (applied in order):
      0 minutes               → never_played
      playtime >= hltb * 60m  → played   (completed or exceeded main story)
      playtime > 0, hltb set  → in_progress
      playtime > 0, no hltb   → played   (can't determine progress; honest default)
    """
    from app.models import GameStatus
    if playtime_minutes == 0:
        return GameStatus.never_played
    if hltb_main_hours is not None:
        if playtime_minutes >= hltb_main_hours * 60:
            return GameStatus.played
        return GameStatus.in_progress
    return GameStatus.played


def _backfill_inferred_statuses(conn: sqlite3.Connection) -> None:
    """
    Correct never_played rows that have non-zero Steam playtime.
    Runs on every init_db() call; the WHERE clause makes it a no-op once
    there are no more qualifying rows.
    """
    rows = conn.execute("""
        SELECT gs.appid, g.playtime_minutes, g.hltb_main_hours
        FROM game_state gs
        JOIN games g ON gs.appid = g.appid
        WHERE gs.manually_set = 0
          AND gs.status = 'never_played'
          AND g.playtime_minutes > 0
    """).fetchall()

    if not rows:
        return

    now = datetime.utcnow().isoformat()
    for row in rows:
        new_status = infer_status(row["playtime_minutes"], row["hltb_main_hours"])
        conn.execute(
            "UPDATE game_state SET status = ?, updated_at = ? WHERE appid = ? AND manually_set = 0",
            (new_status.value, now, row["appid"]),
        )


# ---------------------------------------------------------------------------
# Row conversion helpers
# ---------------------------------------------------------------------------

def _row_to_game(row: sqlite3.Row) -> Game:
    return Game(
        appid=row["appid"],
        name=row["name"],
        playtime_minutes=row["playtime_minutes"],
        last_played_steam=_parse_dt(row["last_played_steam"]),
        installed=_parse_bool(row["installed"]),
        hltb_main_hours=row["hltb_main_hours"],
        hltb_main_extra_hours=row["hltb_main_extra_hours"],
        hltb_completionist_hours=row["hltb_completionist_hours"],
        genres=row["genres"] or "",
        tags=row["tags"] or "",
        developer=row["developer"],
        publisher=row["publisher"],
        metacritic_score=row["metacritic_score"],
        opencritic_score=row["opencritic_score"],
        steam_review_pct=row["steam_review_pct"],
        steam_review_count=row["steam_review_count"],
        last_refreshed=_parse_dt(row["last_refreshed"]) or datetime.utcnow(),
        is_active=bool(row["is_active"]),
    )


def _row_to_state(row: sqlite3.Row) -> GameState:
    return GameState(
        appid=row["appid"],
        status=GameStatus(row["status"]),
        hours_played_manual=row["hours_played_manual"],
        notes=row["notes"],
        updated_at=_parse_dt(row["updated_at"]) or datetime.utcnow(),
        manually_set=bool(row["manually_set"]) if row["manually_set"] is not None else False,
    )


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_bool(value) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------

def upsert_game_steam_fields(
    conn: sqlite3.Connection,
    appid: int,
    name: str,
    playtime_minutes: int,
    last_played_steam: Optional[datetime],
    is_active: bool,
) -> None:
    """
    Insert or update only the fields sourced from Steam's owned-games list.
    Never overwrites enrichment columns (HLTB, genres, scores, reviews).
    For new rows all enrichment columns start as NULL/empty defaults.
    last_refreshed is set on INSERT but intentionally excluded from the
    UPDATE clause — it only advances when _phase_enrich writes enrichment data.
    """
    conn.execute("""
        INSERT INTO games (appid, name, playtime_minutes, last_played_steam, is_active, last_refreshed)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(appid) DO UPDATE SET
            name              = excluded.name,
            playtime_minutes  = excluded.playtime_minutes,
            last_played_steam = excluded.last_played_steam,
            is_active         = excluded.is_active
    """, (
        appid,
        name,
        playtime_minutes,
        last_played_steam.isoformat() if last_played_steam else None,
        1 if is_active else 0,
        datetime.utcnow().isoformat(),
    ))


def upsert_game(conn: sqlite3.Connection, game: Game) -> None:
    conn.execute("""
        INSERT INTO games (
            appid, name, playtime_minutes, last_played_steam, installed,
            hltb_main_hours, hltb_main_extra_hours, hltb_completionist_hours,
            genres, tags, developer, publisher,
            metacritic_score, opencritic_score,
            steam_review_pct, steam_review_count,
            last_refreshed, is_active
        ) VALUES (
            :appid, :name, :playtime_minutes, :last_played_steam, :installed,
            :hltb_main_hours, :hltb_main_extra_hours, :hltb_completionist_hours,
            :genres, :tags, :developer, :publisher,
            :metacritic_score, :opencritic_score,
            :steam_review_pct, :steam_review_count,
            :last_refreshed, :is_active
        )
        ON CONFLICT(appid) DO UPDATE SET
            name                    = excluded.name,
            playtime_minutes        = excluded.playtime_minutes,
            last_played_steam       = excluded.last_played_steam,
            hltb_main_hours         = excluded.hltb_main_hours,
            hltb_main_extra_hours   = excluded.hltb_main_extra_hours,
            hltb_completionist_hours = excluded.hltb_completionist_hours,
            genres                  = excluded.genres,
            tags                    = excluded.tags,
            developer               = excluded.developer,
            publisher               = excluded.publisher,
            metacritic_score        = excluded.metacritic_score,
            opencritic_score        = excluded.opencritic_score,
            steam_review_pct        = excluded.steam_review_pct,
            steam_review_count      = excluded.steam_review_count,
            last_refreshed          = excluded.last_refreshed,
            is_active               = excluded.is_active
    """, {
        "appid": game.appid,
        "name": game.name,
        "playtime_minutes": game.playtime_minutes,
        "last_played_steam": game.last_played_steam.isoformat() if game.last_played_steam else None,
        "installed": None,  # not populated in v1
        "hltb_main_hours": game.hltb_main_hours,
        "hltb_main_extra_hours": game.hltb_main_extra_hours,
        "hltb_completionist_hours": game.hltb_completionist_hours,
        "genres": game.genres,
        "tags": game.tags,
        "developer": game.developer,
        "publisher": game.publisher,
        "metacritic_score": game.metacritic_score,
        "opencritic_score": game.opencritic_score,
        "steam_review_pct": game.steam_review_pct,
        "steam_review_count": game.steam_review_count,
        "last_refreshed": game.last_refreshed.isoformat(),
        "is_active": 1 if game.is_active else 0,
    })


def ensure_game_state(conn: sqlite3.Connection, appid: int, playtime_minutes: int = 0) -> None:
    """
    Insert a game_state row for a new game if one doesn't already exist.
    Initial status is inferred from playtime (HLTB not available yet at this
    point; _phase_enrich will refine to in_progress once HLTB is fetched).
    manually_set is False — this is an auto-inferred row.
    """
    status = infer_status(playtime_minutes, hltb_main_hours=None)
    conn.execute("""
        INSERT OR IGNORE INTO game_state (appid, status, manually_set, updated_at)
        VALUES (?, ?, 0, ?)
    """, (appid, status.value, datetime.utcnow().isoformat()))


def mark_inactive(conn: sqlite3.Connection, appids: list[int]) -> None:
    if not appids:
        return
    placeholders = ",".join("?" * len(appids))
    conn.execute(
        f"UPDATE games SET is_active = 0 WHERE appid IN ({placeholders})",
        appids,
    )


def get_all_active_appids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT appid FROM games WHERE is_active = 1").fetchall()
    return {row["appid"] for row in rows}


def get_games_with_state(
    conn: sqlite3.Connection,
    active_only: bool = True,
) -> list[GameWithState]:
    where = "WHERE g.is_active = 1" if active_only else ""
    rows = conn.execute(f"""
        SELECT
            g.appid, g.name, g.playtime_minutes, g.last_played_steam, g.installed,
            g.hltb_main_hours, g.hltb_main_extra_hours, g.hltb_completionist_hours,
            g.genres, g.tags, g.developer, g.publisher,
            g.metacritic_score, g.opencritic_score,
            g.steam_review_pct, g.steam_review_count,
            g.last_refreshed, g.is_active,
            gs.status, gs.hours_played_manual, gs.notes,
            gs.updated_at AS state_updated_at,
            gs.manually_set
        FROM games g
        LEFT JOIN game_state gs ON g.appid = gs.appid
        {where}
        ORDER BY g.name
    """).fetchall()

    result = []
    for row in rows:
        game = _row_to_game(row)
        state = GameState(
            appid=row["appid"],
            status=GameStatus(row["status"]) if row["status"] else GameStatus.never_played,
            hours_played_manual=row["hours_played_manual"],
            notes=row["notes"],
            updated_at=_parse_dt(row["state_updated_at"]) or datetime.utcnow(),
            manually_set=bool(row["manually_set"]) if row["manually_set"] is not None else False,
        )
        result.append(GameWithState(game=game, state=state))
    return result


# ---------------------------------------------------------------------------
# Game state updates
# ---------------------------------------------------------------------------

def update_game_state(
    conn: sqlite3.Connection,
    appid: int,
    status: Optional[GameStatus] = None,
    hours_played_manual: Optional[float] = None,
    notes: Optional[str] = None,
    manually_set: Optional[bool] = None,
) -> None:
    existing = conn.execute(
        "SELECT appid FROM game_state WHERE appid = ?", (appid,)
    ).fetchone()

    now = datetime.utcnow().isoformat()
    if not existing:
        ms = 1 if manually_set else 0
        conn.execute("""
            INSERT INTO game_state (appid, status, hours_played_manual, notes, manually_set, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (appid, (status or GameStatus.never_played).value, hours_played_manual, notes, ms, now))
    else:
        updates = ["updated_at = ?"]
        params: list = [now]
        if status is not None:
            updates.append("status = ?")
            params.append(status.value)
        if hours_played_manual is not None:
            updates.append("hours_played_manual = ?")
            params.append(hours_played_manual)
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)
        if manually_set is not None:
            updates.append("manually_set = ?")
            params.append(1 if manually_set else 0)
        params.append(appid)
        conn.execute(
            f"UPDATE game_state SET {', '.join(updates)} WHERE appid = ?",
            params,
        )


def maybe_refine_inferred_status(
    conn: sqlite3.Connection,
    appid: int,
    playtime_minutes: int,
    hltb_main_hours: Optional[float],
) -> None:
    """
    Re-run status inference for a game after HLTB data has been fetched.
    Only touches rows where manually_set = 0.

    This refines the initial coarse guess (playtime > 0 → played) into the
    accurate in_progress / played split once HLTB hours are known.
    Also advances a never_played row if Steam now shows playtime (handles
    playtime changes between refreshes).
    """
    row = conn.execute(
        "SELECT status, manually_set FROM game_state WHERE appid = ?", (appid,)
    ).fetchone()

    if not row or row["manually_set"]:
        return

    new_status = infer_status(playtime_minutes, hltb_main_hours)
    if new_status.value != row["status"]:
        conn.execute(
            "UPDATE game_state SET status = ?, updated_at = ? WHERE appid = ? AND manually_set = 0",
            (new_status.value, datetime.utcnow().isoformat(), appid),
        )


# ---------------------------------------------------------------------------
# Refresh log
# ---------------------------------------------------------------------------

def start_refresh_log(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "INSERT INTO refresh_log (started_at, games_added, games_updated) VALUES (?, 0, 0)",
        (datetime.utcnow().isoformat(),),
    )
    return cursor.lastrowid


def finish_refresh_log(
    conn: sqlite3.Connection,
    log_id: int,
    games_added: int,
    games_updated: int,
    errors: Optional[str],
) -> None:
    conn.execute("""
        UPDATE refresh_log
        SET completed_at = ?, games_added = ?, games_updated = ?, errors = ?
        WHERE id = ?
    """, (datetime.utcnow().isoformat(), games_added, games_updated, errors, log_id))
