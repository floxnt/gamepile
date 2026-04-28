# Tonight's Pick

A local desktop app that helps you decide what to play from your Steam library.
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
uv run tonights-pick
```

A native window opens. Hit **Refresh Library** to pull your Steam library and
enrich it with HLTB/OpenCritic data. First refresh takes a while (~2s per game
for HLTB lookups); progress is shown in the banner.

## How recommendations work

### Short-term mode

Recommends games that fit in your available time window (±50%).

1. Filters by your toggles (include unplayed, include in-progress)
2. Estimates remaining time:
   - In-progress + manual hours set → `max(hltb_main - hours_played, 0.5)`
   - In-progress, no manual hours → full HLTB estimate (card shows a hint)
   - Unplayed → full HLTB estimate
3. Scores each candidate:
   - +2 if in-progress
   - +1 if not played in 30+ days (or never)
   - +1 if Metacritic ≥ 85, OpenCritic ≥ 85, or Steam reviews ≥ 90% with ≥ 1000 reviews
4. **Iterative variety selection**: picks highest-scored game, then applies −1
   to remaining candidates sharing its primary genre, repeats until 5 picks.
   This actually enforces genre variety rather than just nudging scores.

### Long-term mode

Recommends games worth committing to across multiple sessions.

1. Requires HLTB main story ≥ 8 hours
2. Scores each candidate:
   - +3 if Metacritic ≥ 85 or OpenCritic ≥ 85
   - +2 if Steam reviews ≥ 90% with ≥ 1000 reviews
   - +1 if in-progress (continuation bias)
   - −2 if dropped (don't re-suggest games you bounced off)
3. Top 5 by score, no genre variety step

## Building the binary (optional)

Requires `pyinstaller`. Only do this after the app works via `uv run`.

```bash
uv pip install pyinstaller
pyinstaller tonights_pick.spec
```

Output: `dist/tonights-pick` — a single self-contained executable.

**Linux note:** The binary still requires `webkit2gtk-4.1` to be installed on
the target machine. PyInstaller cannot bundle GTK/WebKit system libraries.

## Data storage

All data lives in `~/.local/share/tonights-pick/` (or `$XDG_DATA_HOME/tonights-pick/`):

- `tonights-pick.db` — SQLite database
- `.env` — credentials (when running the binary)

During development, `.env` is read from the project root instead.
