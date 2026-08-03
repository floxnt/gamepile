import os
import sys
from pathlib import Path
from typing import Optional

import platformdirs
from dotenv import load_dotenv


# platformdirs.user_data_dir resolves per-platform:
#   Linux:   $XDG_DATA_HOME/gamepile  (defaults to ~/.local/share/gamepile)
#   Windows: %LOCALAPPDATA%\gamepile
#   macOS:   ~/Library/Application Support/gamepile
# appauthor=False suppresses the extra company-name directory on Windows.
_DATA_DIR_NAME = "gamepile"
_DB_FILENAME = "gamepile.db"
_LEGACY_DIR_NAMES = ("tonights-pick", "game-roulette")
_LEGACY_DB_FILENAMES = ("tonights-pick.db", "game-roulette.db")

# Escape hatch for pointing the app at a throwaway data directory.
# docs/DESIGN_CONTRACT.md ("Testing against live data") instructs
# developers to redirect the app at a temp DB copy "via env override" —
# but no such override existed, so every script that thought it was
# isolated silently wrote to the real user database. Added v0.9.12.
_DATA_DIR_ENV_VAR = "GAMEPILE_DATA_DIR"


def data_dir_override() -> Optional[Path]:
    """The GAMEPILE_DATA_DIR override, or None when unset/blank."""
    raw = os.environ.get(_DATA_DIR_ENV_VAR, "").strip()
    return Path(raw).expanduser() if raw else None


def _resolve_data_dir() -> Path:
    override = data_dir_override()
    if override is not None:
        return override
    return Path(platformdirs.user_data_dir(_DATA_DIR_NAME, appauthor=False))


def _migrate_legacy_data(target_dir: Path) -> None:
    """
    One-shot migration: if the new gamepile data dir doesn't exist but a legacy
    one does, rename the directory and its DB file. Refuses to start if any
    rename fails — a clear error beats silently losing user data.

    Linux-only: tonights-pick and game-roulette only ever existed under
    ~/.local/share/. Skip on other platforms where these dirs can't exist.
    """
    if sys.platform != "linux":
        return
    if target_dir.exists():
        return
    # Never rehome legacy data into an explicitly-overridden directory —
    # the override means "use this throwaway location", not "adopt the
    # user's real data".
    if data_dir_override() is not None:
        return

    parent = target_dir.parent
    for legacy_name in _LEGACY_DIR_NAMES:
        legacy = parent / legacy_name
        if not legacy.exists() or not legacy.is_dir():
            continue
        try:
            legacy.rename(target_dir)
        except OSError as exc:
            raise RuntimeError(
                f"GamePile: failed to migrate legacy data from {legacy} to {target_dir}: {exc}\n"
                f"Move the directory manually and restart."
            ) from exc

        new_db = target_dir / _DB_FILENAME
        if not new_db.exists():
            for legacy_db_name in _LEGACY_DB_FILENAMES:
                legacy_db = target_dir / legacy_db_name
                if not legacy_db.exists():
                    continue
                try:
                    legacy_db.rename(new_db)
                    for suffix in ("-wal", "-shm"):
                        side = target_dir / f"{legacy_db_name}{suffix}"
                        if side.exists():
                            side.rename(target_dir / f"{_DB_FILENAME}{suffix}")
                except OSError as exc:
                    raise RuntimeError(
                        f"GamePile: failed to rename legacy DB file {legacy_db}: {exc}"
                    ) from exc
                break
        return


def _find_and_load_env() -> None:
    # Development: .env next to pyproject.toml (project root)
    project_root = Path(__file__).parent.parent / ".env"
    if project_root.exists():
        load_dotenv(project_root)
        return

    # Bundled binary: per-platform data dir (Linux XDG, Windows AppData, macOS Application Support)
    data_dir = _resolve_data_dir()
    data_env = data_dir / ".env"
    if data_env.exists():
        load_dotenv(data_env)
        return

    # Fall back to legacy locations so existing installs keep working until
    # _migrate_legacy_data moves them into place on the next startup.
    # Linux-only: legacy dirs never existed elsewhere.
    if sys.platform == "linux":
        for legacy_name in _LEGACY_DIR_NAMES:
            legacy_env = data_dir.parent / legacy_name / ".env"
            if legacy_env.exists():
                load_dotenv(legacy_env)
                return


_find_and_load_env()


# v4: STEAM_API_KEY / STEAM_ID are no longer eagerly required at import
# time. Missing credentials trigger the first-run wizard via the
# /setup middleware. Callers that need a credential value should import
# from app.credentials (get_steam_api_key / get_steam_id) — those
# accessors honor the keyring + .env precedence rules.
PORT: int = int(os.environ.get("PORT", "8765"))

# Per-platform data directory — used for the SQLite database.
DATA_DIR: Path = _resolve_data_dir()
_migrate_legacy_data(DATA_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH: Path = DATA_DIR / _DB_FILENAME
