# GamePile

A local desktop app that helps you manage your Steam backlog and decide
what to play. The **Shortlist** feature ("suggest 5 games") is the
headline interactive mode; the broader purpose is backlog management
and progress tracking.

Opens as a native window. No browser, no server to manage.

## Requirements

### Runtime (Linux)

| Package | Why |
|---|---|
| `webkit2gtk-4.1` | WebKit rendering engine for the native window |
| `python-gobject` | GObject bindings used by pywebview's GTK backend |
| `gtk3` | GTK3 libraries |

Install on Arch: `sudo pacman -S webkit2gtk-4.1 python-gobject gtk3`

Install on Ubuntu/Debian: `sudo apt install python3-gi gir1.2-webkit2-4.1`

### Development

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)

## Setup

```bash
# Clone and install dependencies
uv sync

# Copy and fill in your credentials
cp .env.example .env
$EDITOR .env
```

Your `.env` needs:
- `STEAM_API_KEY` — get one at https://steamcommunity.com/dev/apikey
- `STEAM_ID` — your 64-bit SteamID (find it at https://steamid.io)

## Running

```bash
uv run gamepile
```

A native window opens. Hit **Refresh Library** to pull your Steam library and
enrich it with HLTB / SteamSpy data. First refresh takes a while (~2s per game
for HLTB lookups); progress is shown in the banner.

## Shortlist modes

The Shortlist tab offers five user-intent modes:

- **I only have tonight** — games that fit your time window (±50%)
- **Continue something** — surfaces in-progress games, especially those near completion
- **Comfort pick** — favors high-playtime games you've clearly enjoyed
- **Start something new** — never-played (and effectively-untouched) games worth committing to
- **Surprise me** — randomized with a quality bias

## Building the binary (optional)

Requires `pyinstaller`. Only do this after the app works via `uv run`.

```bash
uv pip install pyinstaller
pyinstaller gamepile.spec
```

Output: `dist/gamepile` — a single self-contained executable.

**Linux note:** The binary still requires `webkit2gtk-4.1` to be installed on
the target machine. PyInstaller cannot bundle GTK/WebKit system libraries.

## Data storage

All data lives in `~/.local/share/gamepile/` (or `$XDG_DATA_HOME/gamepile/`):

- `gamepile.db` — SQLite database
- `.env` — credentials (when running the binary)

If a legacy install exists at `~/.local/share/tonights-pick/` or
`~/.local/share/game-roulette/`, the app migrates it automatically on
first launch. If the migration fails, the app refuses to start rather
than risk losing data.

During development, `.env` is read from the project root instead.
