import os
from pathlib import Path
from dotenv import load_dotenv


_xdg_data = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
_DATA_DIR_NAME = "gamepile"
_DB_FILENAME = "gamepile.db"
_LEGACY_DIR_NAMES = ("tonights-pick", "game-roulette")
_LEGACY_DB_FILENAMES = ("tonights-pick.db", "game-roulette.db")


def _migrate_legacy_data(target_dir: Path) -> None:
    """
    One-shot migration: if the new gamepile data dir doesn't exist but a legacy
    one does, rename the directory and its DB file. Refuses to start if any
    rename fails — a clear error beats silently losing user data.
    """
    if target_dir.exists():
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

    # Bundled binary: XDG data dir
    data_env = Path(_xdg_data) / _DATA_DIR_NAME / ".env"
    if data_env.exists():
        load_dotenv(data_env)
        return

    # Fall back to legacy locations so existing installs keep working until
    # _migrate_legacy_data moves them into place on the next startup.
    for legacy_name in _LEGACY_DIR_NAMES:
        legacy_env = Path(_xdg_data) / legacy_name / ".env"
        if legacy_env.exists():
            load_dotenv(legacy_env)
            return


_find_and_load_env()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required config: {name}\n"
            f"Add it to .env (see .env.example)"
        )
    return value


STEAM_API_KEY: str = _require("STEAM_API_KEY")
STEAM_ID: str = _require("STEAM_ID")
PORT: int = int(os.environ.get("PORT", "8765"))

# XDG data directory — used for the SQLite database
DATA_DIR: Path = Path(_xdg_data) / _DATA_DIR_NAME
_migrate_legacy_data(DATA_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH: Path = DATA_DIR / _DB_FILENAME
