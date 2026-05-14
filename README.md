# GamePile

A local desktop app for managing your Steam backlog and deciding what to play.
The **Shortlist** feature ("suggest 5 games") is the headline interactive
mode; the broader purpose is backlog management and progress tracking.

Native window, no browser tab, no server to manage. Single-user, no cloud.

---

This is the **developer README**. For end-user install / first-run / troubleshooting,
see the README bundled with each release archive (or `README.bundled.md` in this repo).

## Stack

- Python 3.12+, FastAPI backend
- HTMX + Jinja2, vanilla CSS, no JS frameworks
- SQLite via `sqlite3` stdlib (no ORM)
- pywebview for the native window (GTK on Linux, EdgeChromium/WebView2 on Windows)
- [uv](https://github.com/astral-sh/uv) for dependency management
- PyInstaller (--onedir) for binary distribution

## Runtime requirements

### Linux (development + binary)

Install GTK + WebKit2 system libraries before `uv sync`:

| Distro | Command |
|---|---|
| Arch | `sudo pacman -S webkit2gtk-4.1 python-gobject gtk3` |
| Ubuntu/Debian | `sudo apt install python3-gi gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0` |

These cannot be bundled portably — PyInstaller can't ship system libraries.

### Windows (binary only)

Microsoft Edge **WebView2 Runtime** is required. Preinstalled on Windows 11 standard
SKUs; missing on Windows 11 LTSC and some Windows 10 builds. GamePile detects this
at launch and auto-opens the installer page.

## Dev setup

```bash
# Clone, install deps
uv sync

# First-run wizard handles credential setup, OR drop a .env at project root:
cat > .env <<EOF
STEAM_API_KEY=your_key_from_steamcommunity.com/dev/apikey
STEAM_ID=your_steamid64_or_vanity
EOF

# Run
uv run gamepile
```

The first launch (with no creds + no .env) walks through the setup wizard at
`/setup/welcome`. Credentials persist in the OS keychain via the
[`keyring`](https://pypi.org/project/keyring/) library (Windows Credential
Manager / macOS Keychain / Linux Secret Service).

## Data location

| Platform | Path |
|---|---|
| Linux | `~/.local/share/gamepile/` (respects `$XDG_DATA_HOME`) |
| Windows | `%LOCALAPPDATA%\gamepile\` |
| macOS | `~/Library/Application Support/gamepile/` |

Resolved via the [`platformdirs`](https://pypi.org/project/platformdirs/) library.

## Running tests

```bash
uv run python tests/run_tests.py
```

Aggregates every `tests/test_*.py` suite. No pytest dependency — each suite
runs as a script with its own `main()`.

## Building binaries locally

```bash
uv pip install pyinstaller
uv run pyinstaller gamepile.spec --noconfirm
```

Output: `dist/gamepile/gamepile` (Linux) or `dist\gamepile\gamepile.exe` (Windows).
Smoke-test with the `--healthz-only` flag:

```bash
dist/gamepile/gamepile --healthz-only   # expect "ok" on stdout, exit 0
```

## Building binaries via CI

Push a tag matching `v*` (e.g. `v0.5.0`):

```bash
git tag v0.5.0
git push --tags
```

`.github/workflows/release.yml` runs on `ubuntu-latest` + `windows-latest`,
builds both bundles, uploads them as a **draft** GitHub Release. Promote
manually once you've verified the artifacts.

Manual dry-runs without tagging: trigger the workflow via the Actions tab
("Run workflow"). Artifacts land on the run page only, not in Releases.

## Architecture

See `docs/PROJECT_STATE.md` for the full version-by-version status and
`docs/DESIGN_CONTRACT.md` for design rules every contributor must follow.

Per-feature design docs:
- `SPEC.md` / `SPEC_V2.md` / `SPEC_V3_*.md` — versioned feature specs
- `SPEC_V4_SETUP.md` — setup wizard + keyring credential storage
- `SPEC_V5_DISTRIBUTION.md` — binary distribution pipeline

## Contributing

This is a personal project. Bug reports and PRs from friends running the
binary are welcome at <https://github.com/floxnt/gamepile/issues>.
