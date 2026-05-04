import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Generator, Optional

from app.config import DB_PATH
from app.models import Game, GameState, GameStatus, GameWithState, PickHistory, RecentPick, RefreshLog


_RECENT_ACTIVITY_DAYS = 30


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
            "ALTER TABLE game_state ADD COLUMN blacklisted BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE game_state ADD COLUMN dropped_strength TEXT",
            "ALTER TABLE games ADD COLUMN release_date TEXT",
            "ALTER TABLE games ADD COLUMN description TEXT",
            "ALTER TABLE game_state ADD COLUMN pinned_for_shortlist BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE game_state ADD COLUMN pinned_at TEXT",
            # Eligibility-at-pick-time capture for the Dashboard's
            # picks-per-week calculation. Both nullable; historical rows
            # NULL on both columns are treated as "include" (charitable to
            # existing data, no backfill).
            "ALTER TABLE pick_history ADD COLUMN status_at_pick TEXT",
            "ALTER TABLE pick_history ADD COLUMN was_forever_at_pick BOOLEAN",
            # User-set 1-5 personal rating, surfaced via the Game Detail page.
            # The existing `notes` column is reused for personal notes (was
            # added in v1 but never wired to UI) — no separate column added.
            "ALTER TABLE game_state ADD COLUMN personal_rating INTEGER",
            # v3 hook-point Phase 1a: per-game engagement signals.
            # All nullable — Phase 1a populates what's available; Phase 1b
            # turns these into a categorical "stickiness" badge after
            # threshold tuning against the real distribution.
            "ALTER TABLE games ADD COLUMN completion_rate REAL",
            "ALTER TABLE games ADD COLUMN cliff_metric REAL",
            "ALTER TABLE games ADD COLUMN review_playtime_median INTEGER",
            "ALTER TABLE games ADD COLUMN stickiness_ratio REAL",
            "ALTER TABLE games ADD COLUMN playtime_median_avg_ratio REAL",
            # Confidence label for completion_rate: 'high' / 'low' / NULL.
            # Lets Phase 1b weight strong-pattern matches differently from
            # fallback / weak-pattern picks.
            "ALTER TABLE games ADD COLUMN completion_rate_confidence TEXT",
            # v3 game-type classification: cached classification + manual
            # override flag + raw Steam app type ("game" / "dlc" / etc.)
            # used by the classifier.
            "ALTER TABLE games ADD COLUMN game_type TEXT",
            "ALTER TABLE games ADD COLUMN game_type_manual BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE games ADD COLUMN app_type TEXT",
        ]:
            try:
                conn.execute(ddl)
            except Exception:
                pass  # column already exists

        # Convert any legacy 'played' rows to 'played_unclassified'. The old enum
        # used 'played' as a catch-all for "Steam shows hours, user hasn't
        # categorised" — same meaning as the new played_unclassified state.
        conn.execute(
            "UPDATE game_state SET status = 'played_unclassified' WHERE status = 'played'"
        )

        # Backfill: re-run inference for every auto-inferred row so the new rules
        # (played_unclassified split + recent-activity in_progress check) take effect.
        # Manually-set statuses are never touched.
        _backfill_inferred_statuses(conn)


# ---------------------------------------------------------------------------
# Status inference
# ---------------------------------------------------------------------------

def infer_status(
    playtime_minutes: int,
    hltb_main_hours: Optional[float],
    last_played_steam: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> "GameStatus":
    """
    Derive a status from raw Steam playtime and HLTB data per docs/STATE_MACHINE.md.

    Rules (applied in order):
      playtime == 0                                                          → never_played
      playtime < 30                                                          → played_unclassified
      playtime >= 30 AND playtime < hltb_main * 60 AND recent activity (≤30d) → in_progress
      playtime >= 30 (any other case)                                        → played_unclassified
    """
    from app.models import GameStatus
    if playtime_minutes == 0:
        return GameStatus.never_played
    if playtime_minutes < 30:
        return GameStatus.played_unclassified

    if hltb_main_hours is not None and last_played_steam is not None:
        cutoff = (now or datetime.utcnow()) - timedelta(days=_RECENT_ACTIVITY_DAYS)
        recent = last_played_steam >= cutoff
        if recent and playtime_minutes < hltb_main_hours * 60:
            return GameStatus.in_progress

    return GameStatus.played_unclassified


def _backfill_inferred_statuses(conn: sqlite3.Connection) -> None:
    """
    Re-run inference for every auto-inferred row (manually_set = 0).
    Idempotent: applying the same rules again produces the same status.
    """
    rows = conn.execute("""
        SELECT gs.appid, gs.status, g.playtime_minutes, g.hltb_main_hours, g.last_played_steam
        FROM game_state gs
        JOIN games g ON gs.appid = g.appid
        WHERE gs.manually_set = 0
    """).fetchall()

    if not rows:
        return

    now = datetime.utcnow()
    now_iso = now.isoformat()
    for row in rows:
        last_played = _parse_dt(row["last_played_steam"])
        new_status = infer_status(
            row["playtime_minutes"],
            row["hltb_main_hours"],
            last_played,
            now,
        )
        if new_status.value == row["status"]:
            continue
        conn.execute(
            "UPDATE game_state SET status = ?, updated_at = ? WHERE appid = ? AND manually_set = 0",
            (new_status.value, now_iso, row["appid"]),
        )


# ---------------------------------------------------------------------------
# Row conversion helpers
# ---------------------------------------------------------------------------

def _row_to_game(row: sqlite3.Row) -> Game:
    keys = row.keys()
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
        release_date=_parse_dt(row["release_date"]) if "release_date" in keys else None,
        description=row["description"] if "description" in keys else None,
        completion_rate=row["completion_rate"] if "completion_rate" in keys else None,
        completion_rate_confidence=row["completion_rate_confidence"] if "completion_rate_confidence" in keys else None,
        cliff_metric=row["cliff_metric"] if "cliff_metric" in keys else None,
        review_playtime_median=row["review_playtime_median"] if "review_playtime_median" in keys else None,
        stickiness_ratio=row["stickiness_ratio"] if "stickiness_ratio" in keys else None,
        playtime_median_avg_ratio=row["playtime_median_avg_ratio"] if "playtime_median_avg_ratio" in keys else None,
        game_type=row["game_type"] if "game_type" in keys else None,
        game_type_manual=bool(row["game_type_manual"]) if "game_type_manual" in keys and row["game_type_manual"] is not None else False,
        app_type=row["app_type"] if "app_type" in keys else None,
    )


def _row_to_state(row: sqlite3.Row) -> GameState:
    keys = row.keys()
    return GameState(
        appid=row["appid"],
        status=GameStatus(row["status"]),
        hours_played_manual=row["hours_played_manual"],
        notes=row["notes"],
        updated_at=_parse_dt(row["updated_at"]) or datetime.utcnow(),
        manually_set=bool(row["manually_set"]) if row["manually_set"] is not None else False,
        has_technical_issue=bool(row["has_technical_issue"]) if row["has_technical_issue"] is not None else False,
        blacklisted=bool(row["blacklisted"]) if row["blacklisted"] is not None else False,
        dropped_strength=row["dropped_strength"] if "dropped_strength" in keys else None,
        pinned_for_shortlist=bool(row["pinned_for_shortlist"]) if "pinned_for_shortlist" in keys and row["pinned_for_shortlist"] is not None else False,
        pinned_at=_parse_dt(row["pinned_at"]) if "pinned_at" in keys else None,
        personal_rating=row["personal_rating"] if "personal_rating" in keys else None,
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
            last_refreshed, is_active, release_date, description,
            completion_rate, completion_rate_confidence, cliff_metric,
            review_playtime_median, stickiness_ratio, playtime_median_avg_ratio,
            game_type, game_type_manual, app_type
        ) VALUES (
            :appid, :name, :playtime_minutes, :last_played_steam, :installed,
            :hltb_main_hours, :hltb_main_extra_hours, :hltb_completionist_hours,
            :genres, :tags, :user_tags, :developer, :publisher,
            :metacritic_score, :opencritic_score,
            :steam_review_pct, :steam_review_count,
            :last_refreshed, :is_active, :release_date, :description,
            :completion_rate, :completion_rate_confidence, :cliff_metric,
            :review_playtime_median, :stickiness_ratio, :playtime_median_avg_ratio,
            :game_type, :game_type_manual, :app_type
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
            is_active               = excluded.is_active,
            release_date            = COALESCE(excluded.release_date, games.release_date),
            description             = COALESCE(excluded.description, games.description),
            -- Hook-point Phase 1a metrics: COALESCE so cache-skip writes
            -- (where the field comes through as None on the new Game value)
            -- preserve previously-stored data instead of nulling it.
            completion_rate            = COALESCE(excluded.completion_rate, games.completion_rate),
            completion_rate_confidence = COALESCE(excluded.completion_rate_confidence, games.completion_rate_confidence),
            cliff_metric               = COALESCE(excluded.cliff_metric, games.cliff_metric),
            review_playtime_median     = COALESCE(excluded.review_playtime_median, games.review_playtime_median),
            stickiness_ratio           = COALESCE(excluded.stickiness_ratio, games.stickiness_ratio),
            playtime_median_avg_ratio  = COALESCE(excluded.playtime_median_avg_ratio, games.playtime_median_avg_ratio),
            -- Game-type classification: COALESCE on game_type so cache-skipped
            -- iterations don't nullify, and so callers that omit game_type
            -- (sync.py when game_type_manual=1) preserve the user's override.
            -- Caller is the primary safety; this COALESCE is the secondary.
            game_type                  = COALESCE(excluded.game_type, games.game_type),
            game_type_manual           = excluded.game_type_manual,
            app_type                   = COALESCE(excluded.app_type, games.app_type)
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
        "release_date": game.release_date.isoformat() if game.release_date else None,
        "description": game.description,
        "completion_rate": game.completion_rate,
        "completion_rate_confidence": game.completion_rate_confidence,
        "cliff_metric": game.cliff_metric,
        "review_playtime_median": game.review_playtime_median,
        "stickiness_ratio": game.stickiness_ratio,
        "playtime_median_avg_ratio": game.playtime_median_avg_ratio,
        "game_type": game.game_type,
        "game_type_manual": 1 if game.game_type_manual else 0,
        "app_type": game.app_type,
    })


def ensure_game_state(
    conn: sqlite3.Connection,
    appid: int,
    playtime_minutes: int = 0,
    last_played_steam: Optional[datetime] = None,
) -> None:
    """
    Insert a game_state row for a new game if one doesn't already exist.
    Initial status is inferred from playtime + last-played (HLTB not available
    yet; _phase_enrich will refine to in_progress once HLTB is fetched).
    manually_set is False — this is an auto-inferred row.
    """
    status = infer_status(
        playtime_minutes,
        hltb_main_hours=None,
        last_played_steam=last_played_steam,
    )
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
            g.last_refreshed, g.is_active, g.release_date, g.description,
            g.completion_rate, g.completion_rate_confidence, g.cliff_metric,
            g.review_playtime_median, g.stickiness_ratio, g.playtime_median_avg_ratio,
            g.game_type, g.game_type_manual, g.app_type,
            gs.status, gs.hours_played_manual, gs.notes,
            gs.updated_at AS state_updated_at,
            gs.manually_set,
            gs.has_technical_issue,
            gs.blacklisted,
            gs.dropped_strength,
            gs.pinned_for_shortlist,
            gs.pinned_at,
            gs.personal_rating
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
            blacklisted=bool(row["blacklisted"]) if row["blacklisted"] is not None else False,
            dropped_strength=row["dropped_strength"],
            pinned_for_shortlist=bool(row["pinned_for_shortlist"]) if row["pinned_for_shortlist"] is not None else False,
            pinned_at=_parse_dt(row["pinned_at"]),
            personal_rating=row["personal_rating"],
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
    blacklisted: Optional[bool] = None,
    dropped_strength: Optional[str] = None,
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
            # If status is being moved away from dropped, clear the orphan
            # dropped_strength so it doesn't dangle. Caller-supplied
            # dropped_strength below overrides this if provided alongside.
            if status != GameStatus.dropped and dropped_strength is None:
                updates.append("dropped_strength = NULL")
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
        if blacklisted is not None:
            updates.append("blacklisted = ?")
            params.append(1 if blacklisted else 0)
        if dropped_strength is not None:
            updates.append("dropped_strength = ?")
            params.append(dropped_strength)
        params.append(appid)
        conn.execute(
            f"UPDATE game_state SET {', '.join(updates)} WHERE appid = ?",
            params,
        )


# ---------------------------------------------------------------------------
# Game Detail page helpers
# ---------------------------------------------------------------------------

def _ensure_state_row(conn: sqlite3.Connection, appid: int) -> None:
    """Insert a default state row if one doesn't exist. Used by the
    set_* helpers below so editing a never-touched game just works."""
    conn.execute("""
        INSERT OR IGNORE INTO game_state (appid, status, manually_set, updated_at)
        VALUES (?, 'never_played', 0, ?)
    """, (appid, datetime.utcnow().isoformat()))


def set_notes(conn: sqlite3.Connection, appid: int, notes: Optional[str]) -> None:
    """Save personal notes. Empty string and None both clear the field
    (an empty notes textarea is functionally 'no notes')."""
    _ensure_state_row(conn, appid)
    value = notes.strip() if notes else None
    if not value:
        value = None
    conn.execute(
        "UPDATE game_state SET notes = ?, updated_at = ? WHERE appid = ?",
        (value, datetime.utcnow().isoformat(), appid),
    )


def set_personal_rating(conn: sqlite3.Connection, appid: int, rating: Optional[int]) -> None:
    """Save 1-5 personal rating. None clears the field. Out-of-range values
    are rejected silently — caller validates beforehand."""
    if rating is not None and not (1 <= rating <= 5):
        return
    _ensure_state_row(conn, appid)
    conn.execute(
        "UPDATE game_state SET personal_rating = ?, updated_at = ? WHERE appid = ?",
        (rating, datetime.utcnow().isoformat(), appid),
    )


def set_hours_played_manual(conn: sqlite3.Connection, appid: int, hours: Optional[float]) -> None:
    """Save manual-override hours played. None clears the field."""
    _ensure_state_row(conn, appid)
    conn.execute(
        "UPDATE game_state SET hours_played_manual = ?, updated_at = ? WHERE appid = ?",
        (hours, datetime.utcnow().isoformat(), appid),
    )


def reset_status_to_inferred(conn: sqlite3.Connection, appid: int) -> Optional[GameStatus]:
    """Clear manually_set and re-run infer_status against current playtime
    + HLTB + last_played. Returns the new status, or None if the game
    doesn't exist. Does not refuse to demote from 'finished' — the user
    explicitly chose Reset, and infer_status only ever returns
    never_played / played_unclassified / in_progress anyway.
    """
    row = conn.execute("""
        SELECT g.playtime_minutes, g.hltb_main_hours, g.last_played_steam
        FROM games g WHERE g.appid = ?
    """, (appid,)).fetchone()
    if not row:
        return None

    last_played = _parse_dt(row["last_played_steam"])
    new_status = infer_status(row["playtime_minutes"], row["hltb_main_hours"], last_played)
    now = datetime.utcnow().isoformat()
    _ensure_state_row(conn, appid)
    # Also clear dropped_strength: if we're re-inferring to anything that
    # isn't `dropped` (and infer_status never returns dropped), the
    # dropped_strength field becomes meaningless and would otherwise dangle.
    conn.execute(
        "UPDATE game_state SET status = ?, manually_set = 0, dropped_strength = NULL, updated_at = ? WHERE appid = ?",
        (new_status.value, now, appid),
    )
    return new_status


def get_picks_for_appid(conn: sqlite3.Connection, appid: int) -> list[PickHistory]:
    """All pick_history rows for one game, newest first."""
    rows = conn.execute(
        "SELECT * FROM pick_history WHERE appid = ? ORDER BY picked_at DESC",
        (appid,),
    ).fetchall()
    return [_row_to_pick_history(r) for r in rows]


# ---------------------------------------------------------------------------
# Backlog → Shortlist pin
# ---------------------------------------------------------------------------

def set_pin(conn: sqlite3.Connection, appid: int) -> None:
    """Pin a game for the next Shortlist run; idempotent.

    Inserts a game_state row if one doesn't exist (status defaults to never_played
    for the rare race where a game is pinned before its state row is created).
    """
    now = datetime.utcnow().isoformat()
    existing = conn.execute(
        "SELECT appid FROM game_state WHERE appid = ?", (appid,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE game_state SET pinned_for_shortlist = 1, pinned_at = ?, updated_at = ? WHERE appid = ?",
            (now, now, appid),
        )
    else:
        conn.execute("""
            INSERT INTO game_state (appid, status, pinned_for_shortlist, pinned_at, updated_at)
            VALUES (?, 'never_played', 1, ?, ?)
        """, (appid, now, now))


def clear_pin(conn: sqlite3.Connection, appid: int) -> None:
    """Clear a game's pin; idempotent (no-op if the game is not pinned)."""
    conn.execute(
        "UPDATE game_state SET pinned_for_shortlist = 0, pinned_at = NULL, updated_at = ? WHERE appid = ?",
        (datetime.utcnow().isoformat(), appid),
    )


def expire_pins(conn: sqlite3.Connection, max_age_days: int = 14) -> int:
    """Clear pins older than max_age_days. Returns count cleared.

    Called at Backlog page load AND at the start of every Shortlist generation —
    expired pins must never get one final boost before being swept.
    """
    cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
    cursor = conn.execute(
        "UPDATE game_state SET pinned_for_shortlist = 0, pinned_at = NULL "
        "WHERE pinned_for_shortlist = 1 AND pinned_at IS NOT NULL AND pinned_at < ?",
        (cutoff,),
    )
    return cursor.rowcount


def maybe_refine_inferred_status(
    conn: sqlite3.Connection,
    appid: int,
    playtime_minutes: int,
    hltb_main_hours: Optional[float],
    last_played_steam: Optional[datetime] = None,
) -> None:
    """
    Re-run status inference for a game after HLTB data has been fetched.
    Only touches rows where manually_set = 0.

    Refines the initial coarse guess once HLTB hours and last_played_steam are
    known (e.g. promoting played_unclassified → in_progress for actively-played
    games still under the HLTB main story estimate).
    """
    row = conn.execute(
        "SELECT status, manually_set FROM game_state WHERE appid = ?", (appid,)
    ).fetchone()

    if not row or row["manually_set"]:
        return

    new_status = infer_status(
        playtime_minutes,
        hltb_main_hours,
        last_played_steam,
    )
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
    status_at_pick: Optional[str] = None,
    was_forever_at_pick: Optional[bool] = None,
) -> int:
    """Insert a pick_history row.

    status_at_pick / was_forever_at_pick capture eligibility at the moment of
    the pick — populated by the caller from pre-pick game state. Both are
    optional so legacy callers / tests don't need to provide them; rows
    inserted without them are treated as "include" by the Dashboard
    picks-per-week filter.
    """
    cursor = conn.execute("""
        INSERT INTO pick_history (
            appid, game_name, picked_at, time_window_minutes, mode,
            candidates_at_pick, outcome, status_at_pick, was_forever_at_pick
        )
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
    """, (
        appid,
        game_name,
        datetime.utcnow().isoformat(),
        time_window_minutes,
        mode,
        json.dumps(candidates_at_pick),
        status_at_pick,
        1 if was_forever_at_pick else (0 if was_forever_at_pick is False else None),
    ))
    return cursor.lastrowid


def get_picks_since(conn: sqlite3.Connection, threshold_dt: datetime) -> list[PickHistory]:
    """Return pick_history rows with picked_at >= threshold_dt, oldest first."""
    rows = conn.execute(
        "SELECT * FROM pick_history WHERE picked_at >= ? ORDER BY picked_at ASC",
        (threshold_dt.isoformat(),),
    ).fetchall()
    return [_row_to_pick_history(r) for r in rows]


def get_most_recent_pick(conn: sqlite3.Connection) -> Optional[PickHistory]:
    """Return the single most recent pick_history row, or None if empty."""
    row = conn.execute(
        "SELECT * FROM pick_history ORDER BY picked_at DESC LIMIT 1"
    ).fetchone()
    return _row_to_pick_history(row) if row else None


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
        status_at_pick=row["status_at_pick"] if "status_at_pick" in keys else None,
        was_forever_at_pick=(
            bool(row["was_forever_at_pick"]) if "was_forever_at_pick" in keys and row["was_forever_at_pick"] is not None
            else None
        ),
    )


def get_recent_picks(conn: sqlite3.Connection, limit: int = 8) -> list[RecentPick]:
    """
    Return the most recent pick per unique game, most recent first.
    If the same game was picked multiple times (e.g. returning to a dropped game),
    only the latest pick row appears.
    """
    rows = conn.execute("""
        SELECT p.* FROM pick_history p
        INNER JOIN (
            SELECT appid, MAX(picked_at) AS latest
            FROM pick_history
            GROUP BY appid
        ) latest_picks
        ON p.appid = latest_picks.appid AND p.picked_at = latest_picks.latest
        ORDER BY p.picked_at DESC
        LIMIT ?
    """, (limit,)).fetchall()

    result = []
    for row in rows:
        ph = _row_to_pick_history(row)
        game = get_game_by_appid(conn, ph.appid)
        if not game:
            continue
        gs_row = conn.execute(
            "SELECT * FROM game_state WHERE appid = ?", (ph.appid,)
        ).fetchone()
        state = (
            _row_to_state(gs_row) if gs_row
            else GameState(
                appid=ph.appid,
                status=GameStatus.never_played,
                hours_played_manual=None,
                notes=None,
                updated_at=datetime.utcnow(),
            )
        )
        result.append(RecentPick(pick=ph, game=game, state=state))
    return result


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


def get_game_with_state_by_appid(
    conn: sqlite3.Connection, appid: int
) -> Optional[GameWithState]:
    """Return one game's current data + state, or None if the game isn't known.

    Used by mark_picked to capture eligibility-at-pick-time without round-
    tripping through get_games_with_state's full library scan.
    """
    game = get_game_by_appid(conn, appid)
    if game is None:
        return None
    gs_row = conn.execute(
        "SELECT * FROM game_state WHERE appid = ?", (appid,)
    ).fetchone()
    state = (
        _row_to_state(gs_row) if gs_row
        else GameState(
            appid=appid,
            status=GameStatus.never_played,
            hours_played_manual=None,
            notes=None,
            updated_at=datetime.utcnow(),
        )
    )
    return GameWithState(game=game, state=state)


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
