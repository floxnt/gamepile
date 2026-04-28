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


def ensure_game_state(conn: sqlite3.Connection, appid: int) -> None:
    """Insert a default game_state row if one doesn't exist yet."""
    conn.execute("""
        INSERT OR IGNORE INTO game_state (appid, status, updated_at)
        VALUES (?, 'never_played', ?)
    """, (appid, datetime.utcnow().isoformat()))


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
            gs.status, gs.hours_played_manual, gs.notes, gs.updated_at AS state_updated_at
        FROM games g
        LEFT JOIN game_state gs ON g.appid = gs.appid
        {where}
        ORDER BY g.name
    """).fetchall()

    result = []
    for row in rows:
        game = _row_to_game(row)
        # Build a synthetic state row for games with no state record yet
        state = GameState(
            appid=row["appid"],
            status=GameStatus(row["status"]) if row["status"] else GameStatus.never_played,
            hours_played_manual=row["hours_played_manual"],
            notes=row["notes"],
            updated_at=_parse_dt(row["state_updated_at"]) or datetime.utcnow(),
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
) -> None:
    existing = conn.execute(
        "SELECT appid FROM game_state WHERE appid = ?", (appid,)
    ).fetchone()

    now = datetime.utcnow().isoformat()
    if not existing:
        conn.execute("""
            INSERT INTO game_state (appid, status, hours_played_manual, notes, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (appid, (status or GameStatus.never_played).value, hours_played_manual, notes, now))
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
        params.append(appid)
        conn.execute(
            f"UPDATE game_state SET {', '.join(updates)} WHERE appid = ?",
            params,
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
