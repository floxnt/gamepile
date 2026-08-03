"""JSON export of the user-authored layer.

Everything GamePile knows falls into one of two buckets: data it fetched
(Steam metadata, HLTB times, computed signals) and data the user made.
The first bucket is reproducible — delete it and a refresh rebuilds it.
The second is not. This module serialises the second bucket so a
reinstall, a disk failure, or a botched migration isn't total loss.

The affinity table is the reason this exists. It's the learned taste
model, accumulated one pick at a time over months, and nothing can
regenerate it.

Export only for now; import lands in a later round. That asymmetry is
deliberate — the envelope is version-stamped from day one so the future
importer has something to branch on rather than having to guess at the
shape of files already in the wild.
"""

import json
import sqlite3
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Bump when the envelope shape changes in a way an importer must notice.
SCHEMA_VERSION = 1

# personal_rating is stored 0–10 (half-stars displayed as value/2). An
# importer MUST refuse or convert on mismatch: a 0–5 file loaded as 0–10
# silently halves every rating, and the reverse silently doubles them.
# That exact doubling bug shipped once already via a re-runnable data
# migration; recording the scale is what makes it detectable next time.
RATING_SCALE = "0-10"

# game_state columns — the whole user-authored status layer.
_GAME_STATE_COLUMNS = [
    "appid",
    "status",
    "hours_played_manual",
    "notes",
    "manually_set",
    "has_technical_issue",
    "blacklisted",
    "dropped_strength",
    "pinned_for_shortlist",
    "pinned_at",
    "personal_rating",
    "finished_at",
    "updated_at",
]

# Manual overrides living on the games table. game_type_manual is only a
# boolean flag — the value the user actually chose sits in game_type, so
# the two have to travel together or the choice is lost. Rows where the
# user overrode nothing are skipped; this is a sparse table by nature.
_GAME_OVERRIDE_COLUMNS = [
    "appid",
    "game_type",
    "game_type_manual",
    "hltb_id_manual",
]

_AFFINITY_COLUMNS = ["kind", "value", "weight", "pick_count"]

_PICK_COLUMNS = [
    "appid",
    "game_name",
    "picked_at",
    "time_window_minutes",
    "mode",
    "outcome",
    "outcome_recorded_at",
    "rating",
    "genre_match_rating",
    "would_have_picked_other_appid",
    "did_not_play_reason",
    "actually_played_appid",
    "status_at_pick",
    "was_forever_at_pick",
]


def app_version() -> str:
    """Installed package version, or "unknown" if metadata isn't present
    (a frozen build may not carry dist-info)."""
    try:
        return _pkg_version("gamepile")
    except PackageNotFoundError:
        return "unknown"


def _rows(conn: sqlite3.Connection, sql: str) -> list[dict]:
    cur = conn.execute(sql)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def build_backup(conn: sqlite3.Connection) -> dict:
    """Assemble the export envelope.

    Timestamps are passed through exactly as stored. Stamping export time
    onto updated_at would destroy the history it records and make a
    restored backup look like every game was touched at once.
    """
    game_state = _rows(
        conn,
        f"SELECT {', '.join(_GAME_STATE_COLUMNS)} FROM game_state ORDER BY appid",
    )

    # Sparse by design: only rows carrying an actual override. A NULL
    # hltb_id_manual with game_type_manual=0 says nothing worth keeping.
    overrides = _rows(
        conn,
        f"SELECT {', '.join(_GAME_OVERRIDE_COLUMNS)} FROM games "
        "WHERE game_type_manual = 1 OR hltb_id_manual IS NOT NULL "
        "ORDER BY appid",
    )

    affinity = _rows(
        conn,
        f"SELECT {', '.join(_AFFINITY_COLUMNS)} FROM affinity ORDER BY kind, value",
    )

    # picked_at ordering, not id — ids are local autoincrement and carry
    # no meaning across installs, so they're left out of the export.
    picks = _rows(
        conn,
        f"SELECT {', '.join(_PICK_COLUMNS)} FROM pick_history ORDER BY picked_at",
    )

    return {
        "schema": SCHEMA_VERSION,
        "rating_scale": RATING_SCALE,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "app_version": app_version(),
        "game_state": game_state,
        "game_overrides": overrides,
        "affinity": affinity,
        "picks": picks,
    }


def serialize(backup: dict) -> str:
    """Pretty-printed so the file is greppable and diffable — a backup
    nobody can read by hand is a backup nobody can sanity-check."""
    return json.dumps(backup, indent=2, ensure_ascii=False)


def filename(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y-%m-%d")
    return f"gamepile-backup-{stamp}.json"
