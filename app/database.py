import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Generator, Optional

from app.config import DB_PATH
from app.models import Game, GameState, GameStatus, GameWithState, PickHistory, RefreshLog


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

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pick_history (
                id                              INTEGER PRIMARY KEY AUTOINCREMENT,
                appid                           INTEGER NOT NULL REFERENCES games(appid),
                game_name                       TEXT NOT NULL,
                picked_at                       TEXT NOT NULL,
                time_window_minutes             INTEGER,
                mode                            TEXT NOT NULL,
                candidates_at_pick              TEXT NOT NULL,
                outcome                         TEXT,
                outcome_recorded_at             TEXT,
                rating                          INTEGER,
                genre_match_rating              INTEGER,
                would_have_picked_other_appid   INTEGER REFERENCES games(appid)
            );

            CREATE TABLE IF NOT EXISTS affinity (
                kind        TEXT NOT NULL,
                value       TEXT NOT NULL,
                weight      REAL NOT NULL DEFAULT 0.0,
                pick_count  INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (kind, value)
            );
        """)

        # --- Migrations ---
        # TODO: for v3+, replace try/except ALTER TABLE with a proper migration
        #       system using a schema_version table that tracks applied migrations.
        for ddl in [
            "ALTER TABLE game_state ADD COLUMN manually_set BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE games ADD COLUMN user_tags TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE game_state ADD COLUMN has_technical_issue BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE pick_history ADD COLUMN did_not_play_reason TEXT",
            "ALTER TABLE pick_history ADD COLUMN actually_played_appid INTEGER REFERENCES games(appid)",
        ]:
            try:
                conn.execute(ddl)
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
        user_tags=row["user_tags"] or "",
    )


def _row_to_state(row: sqlite3.Row) -> GameState:
    return GameState(
        appid=row["appid"],
        status=GameStatus(row["status"]),
        hours_played_manual=row["hours_played_manual"],
        notes=row["notes"],
        updated_at=_parse_dt(row["updated_at"]) or datetime.utcnow(),
        manually_set=bool(row["manually_set"]) if row["manually_set"] is not None else False,
        has_technical_issue=bool(row["has_technical_issue"]) if row["has_technical_issue"] is not None else False,
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
            genres, tags, user_tags, developer, publisher,
            metacritic_score, opencritic_score,
            steam_review_pct, steam_review_count,
            last_refreshed, is_active
        ) VALUES (
            :appid, :name, :playtime_minutes, :last_played_steam, :installed,
            :hltb_main_hours, :hltb_main_extra_hours, :hltb_completionist_hours,
            :genres, :tags, :user_tags, :developer, :publisher,
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
            user_tags               = excluded.user_tags,
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
        "user_tags": game.user_tags,
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
            g.genres, g.tags, g.user_tags, g.developer, g.publisher,
            g.metacritic_score, g.opencritic_score,
            g.steam_review_pct, g.steam_review_count,
            g.last_refreshed, g.is_active,
            gs.status, gs.hours_played_manual, gs.notes,
            gs.updated_at AS state_updated_at,
            gs.manually_set,
            gs.has_technical_issue
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
            has_technical_issue=bool(row["has_technical_issue"]) if row["has_technical_issue"] is not None else False,
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
    has_technical_issue: Optional[bool] = None,
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
        if has_technical_issue is not None:
            updates.append("has_technical_issue = ?")
            params.append(1 if has_technical_issue else 0)
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


# ---------------------------------------------------------------------------
# Pick history
# ---------------------------------------------------------------------------

def insert_pick_history(
    conn: sqlite3.Connection,
    appid: int,
    game_name: str,
    mode: str,
    time_window_minutes: Optional[int],
    candidates_at_pick: list[int],
) -> int:
    cursor = conn.execute("""
        INSERT INTO pick_history
            (appid, game_name, picked_at, time_window_minutes, mode, candidates_at_pick, outcome)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
    """, (
        appid,
        game_name,
        datetime.utcnow().isoformat(),
        time_window_minutes,
        mode,
        json.dumps(candidates_at_pick),
    ))
    return cursor.lastrowid


def get_oldest_pending_pick(conn: sqlite3.Connection) -> Optional[PickHistory]:
    """Return the oldest pick_history row with outcome IS NULL, or None."""
    row = conn.execute("""
        SELECT * FROM pick_history
        WHERE outcome IS NULL
        ORDER BY picked_at ASC
        LIMIT 1
    """).fetchone()
    if not row:
        return None
    return _row_to_pick_history(row)


def get_pick_history_by_id(conn: sqlite3.Connection, pick_id: int) -> Optional[PickHistory]:
    row = conn.execute("SELECT * FROM pick_history WHERE id = ?", (pick_id,)).fetchone()
    return _row_to_pick_history(row) if row else None


def update_pick_outcome(
    conn: sqlite3.Connection,
    pick_id: int,
    outcome: Optional[str] = None,
    rating: Optional[int] = None,
    genre_match_rating: Optional[int] = None,
    would_have_picked_other_appid: Optional[int] = None,
    did_not_play_reason: Optional[str] = None,
    actually_played_appid: Optional[int] = None,
) -> None:
    """Update pick_history fields. outcome is optional so sub-steps can save
    extra columns (e.g. actually_played_appid) without overwriting the outcome."""
    updates: list = []
    params: list = []

    if outcome is not None:
        updates += ["outcome = ?", "outcome_recorded_at = ?"]
        params += [outcome, datetime.utcnow().isoformat()]
    if rating is not None:
        updates.append("rating = ?")
        params.append(rating)
    if genre_match_rating is not None:
        updates.append("genre_match_rating = ?")
        params.append(genre_match_rating)
    if would_have_picked_other_appid is not None:
        updates.append("would_have_picked_other_appid = ?")
        params.append(would_have_picked_other_appid)
    if did_not_play_reason is not None:
        updates.append("did_not_play_reason = ?")
        params.append(did_not_play_reason)
    if actually_played_appid is not None:
        updates.append("actually_played_appid = ?")
        params.append(actually_played_appid)

    if not updates:
        return

    params.append(pick_id)
    conn.execute(
        f"UPDATE pick_history SET {', '.join(updates)} WHERE id = ?",
        params,
    )


def _row_to_pick_history(row: sqlite3.Row) -> PickHistory:
    keys = row.keys()
    return PickHistory(
        id=row["id"],
        appid=row["appid"],
        game_name=row["game_name"],
        picked_at=_parse_dt(row["picked_at"]) or datetime.utcnow(),
        time_window_minutes=row["time_window_minutes"],
        mode=row["mode"],
        candidates_at_pick=row["candidates_at_pick"],
        outcome=row["outcome"],
        outcome_recorded_at=_parse_dt(row["outcome_recorded_at"]),
        rating=row["rating"],
        genre_match_rating=row["genre_match_rating"],
        would_have_picked_other_appid=row["would_have_picked_other_appid"],
        did_not_play_reason=row["did_not_play_reason"] if "did_not_play_reason" in keys else None,
        actually_played_appid=row["actually_played_appid"] if "actually_played_appid" in keys else None,
    )


def search_games_by_name(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[Game]:
    """Case-insensitive substring search against active games."""
    query = query.strip()
    if not query:
        return []
    rows = conn.execute(
        "SELECT * FROM games WHERE name LIKE ? AND is_active = 1 ORDER BY name LIMIT ?",
        (f"%{query}%", limit),
    ).fetchall()
    return [_row_to_game(r) for r in rows]


# ---------------------------------------------------------------------------
# Affinity
# ---------------------------------------------------------------------------

def get_all_affinities(conn: sqlite3.Connection) -> dict:
    """Return {(kind, value_lower): (weight, pick_count)} for all rows."""
    rows = conn.execute("SELECT kind, value, weight, pick_count FROM affinity").fetchall()
    return {(r["kind"], r["value"].lower()): (r["weight"], r["pick_count"]) for r in rows}


def get_game_by_appid(conn: sqlite3.Connection, appid: int) -> Optional[Game]:
    row = conn.execute("SELECT * FROM games WHERE appid = ?", (appid,)).fetchone()
    return _row_to_game(row) if row else None


def upsert_affinity_delta(
    conn: sqlite3.Connection,
    kind: str,
    value: str,
    delta: float,
    increment_pick_count: bool,
) -> None:
    """Apply delta to an affinity weight, clamped to [-10, +10]."""
    now = datetime.utcnow().isoformat()
    existing = conn.execute(
        "SELECT weight, pick_count FROM affinity WHERE kind = ? AND value = ?",
        (kind, value),
    ).fetchone()

    if existing:
        new_weight = max(-10.0, min(10.0, existing["weight"] + delta))
        new_count = existing["pick_count"] + (1 if increment_pick_count else 0)
        conn.execute(
            "UPDATE affinity SET weight = ?, pick_count = ?, updated_at = ? WHERE kind = ? AND value = ?",
            (new_weight, new_count, now, kind, value),
        )
    else:
        initial = max(-10.0, min(10.0, delta))
        conn.execute(
            "INSERT INTO affinity (kind, value, weight, pick_count, updated_at) VALUES (?, ?, ?, ?, ?)",
            (kind, value, initial, 1 if increment_pick_count else 0, now),
        )
