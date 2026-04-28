import os
from pathlib import Path
from dotenv import load_dotenv


def _find_and_load_env() -> None:
    # Development: .env next to pyproject.toml (project root)
    project_root = Path(__file__).parent.parent / ".env"
    if project_root.exists():
        load_dotenv(project_root)
        return

    # Bundled binary: XDG data dir
    xdg_data = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    data_env = Path(xdg_data) / "tonights-pick" / ".env"
    if data_env.exists():
        load_dotenv(data_env)


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
_xdg_data = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
DATA_DIR: Path = Path(_xdg_data) / "tonights-pick"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH: Path = DATA_DIR / "tonights-pick.db"
