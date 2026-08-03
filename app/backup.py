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
from pathlib import Path

from app import config

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


# ---------------------------------------------------------------------------
# Writing to disk
#
# The export is written server-side rather than served as a browser
# download. GamePile ships inside a webview, and neither embedded engine
# will complete a Content-Disposition download: pywebview gates both on a
# settings['ALLOW_DOWNLOADS'] flag that defaults to False, so Qt never
# connects its downloadRequested handler and the Windows WebView2 backend
# sets args.Cancel = True outright. The click produced no file and no
# error. Enabling the flag wouldn't be enough either — pywebview's Qt
# handler calls download.setPath(), removed from
# QWebEngineDownloadRequest back in Qt 6.2.
#
# Writing the file ourselves needs no webview-specific code and behaves
# identically on both platforms. The trade-off is no native save dialog,
# which is why every caller must surface the resolved path.
# ---------------------------------------------------------------------------

def default_target_dir() -> Path:
    """Where backups land: the user's Downloads folder when it exists,
    otherwise the app's own data directory.

    Downloads is the first place someone looks for a file an app just
    saved. The data dir is the fallback rather than the default because
    it's buried under platformdirs and nobody browses there by habit —
    but it's guaranteed to exist and be writable, which Downloads isn't.
    """
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        return downloads
    return config.DATA_DIR


def _unique_path(directory: Path, name: str) -> Path:
    """First free path for `name` in `directory`, suffixing -2, -3, … on
    collision.

    Exporting twice in one day must not overwrite the first file. This is
    a backup feature; silently replacing an existing backup with a newer
    one is the specific failure it exists to prevent.
    """
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    n = 2
    while True:
        candidate = directory / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def write_backup(
    conn: sqlite3.Connection,
    target_dir: Path | None = None,
) -> Path:
    """Build the export and write it to disk. Returns the resolved path.

    Raises OSError if the target directory can't be written to — the
    caller is expected to surface that rather than swallow it, since a
    backup that silently didn't happen is worse than no backup button.
    """
    directory = Path(target_dir) if target_dir is not None else default_target_dir()
    directory.mkdir(parents=True, exist_ok=True)

    payload = build_backup(conn)
    path = _unique_path(directory, filename())

    # Write via a temp file in the same directory, then rename. A crash
    # partway through must not leave a truncated file sitting there
    # looking like a valid backup.
    tmp = path.with_name(path.name + ".partial")
    try:
        tmp.write_text(serialize(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return path.resolve()
