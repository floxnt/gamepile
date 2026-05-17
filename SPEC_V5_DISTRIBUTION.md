# v5 Cross-Platform Binary Distribution

## Purpose

Required to take GamePile from "dev on the author's Linux box" to "friend
downloads a ZIP and runs it on their Windows machine". v5 ships the build
pipeline. Cross-platform validation (running the bundle on real Windows
machines, gathering feedback, iterating on issues) is the *next* phase —
landing the pipeline is what unlocks that work.

Scope: Windows + Linux x86_64. macOS skipped — friends aren't on macOS,
the .NET-Framework-class indirection isn't worth the CI minutes when the
runtime story is untested. Re-include later if someone asks.

## Path-resolution audit (precondition)

Before any PyInstaller work, every site using `os.environ.get("XDG_DATA_HOME")`
or `Path.home() / ".local"` was identified and migrated to
`platformdirs.user_data_dir("gamepile", appauthor=False)`. Resolves
per-platform:

| Platform | Path |
|---|---|
| Linux | `$XDG_DATA_HOME/gamepile` (defaults to `~/.local/share/gamepile`) |
| Windows | `%LOCALAPPDATA%\gamepile` |
| macOS | `~/Library/Application Support/gamepile` |

`appauthor=False` suppresses the extra company-name nesting Windows adds
by default.

Legacy data migration (`tonights-pick` / `game-roulette` → `gamepile`)
guarded by `sys.platform == "linux"` since those legacy directories
never existed elsewhere.

`tests/test_paths.py` (9 tests) covers DATA_DIR resolution, writability,
the Linux-only migration guard on both linux and non-linux platforms,
and a regression-canary grep that fails if anyone reintroduces a
hardcoded XDG pattern in production code.

## Build pipeline

### PyInstaller spec (`gamepile.spec`)

**Mode: `--onedir`.** Not `--onefile`. Two reasons:

1. Windows Defender flags `--onefile` bundles as suspicious far more
   often than `--onedir`. SmartScreen friction is already worse on
   `--onefile`.
2. `--onefile` startup extracts to a temp dir on every launch — slower
   and unnecessary for a desktop app.

**Per-platform backend selection** in the spec via `sys.platform`:

- Windows: `webview.platforms.edgechromium` + `winforms` as hidden
  imports; GTK / cocoa excluded
- Linux: `webview.platforms.gtk` + `gi.repository.{WebKit2,Gtk,GLib}` as
  hidden imports; edgechromium / winforms / cocoa excluded

**Keyring backends per-platform.** Linux Secret Service depends on
`secretstorage` which isn't installed on Windows wheels — blanket-hiding
all backends would break the Windows build. The spec scopes:

- Windows: `keyring.backends.Windows`
- Linux: `keyring.backends.SecretService`, `keyring.backends.kwallet`,
  `secretstorage`
- macOS: `keyring.backends.macOS`

Plus the always-needed `keyring.backends.fail` and `keyring.backends.chainer`
on every platform.

**`pyproject.toml` PEP 508 markers.** `PyGObject>=3.42; sys_platform == 'linux'`
keeps `uv sync` from trying to install PyGObject on the Windows CI runner
(no wheel exists).

**`collect_all("pythonnet")` + `collect_all("clr_loader")` (Windows only).**
pywebview's edgechromium and winforms backend modules both do
`import clr` at module load. That fires the chain pythonnet →
clr_loader → netfx loader → reflection lookup of
`Python.Runtime.Loader.Initialize` inside `Python.Runtime.dll`.
PyInstaller's binary auto-walk finds `Python.Runtime.dll` itself but
misses its non-DLL companions in `pythonnet/runtime/`:
`Python.Runtime.dll.config` (binding redirects to the in-box
netstandard 2.0 facade), `Python.Runtime.runtimeconfig.json`,
`Python.Runtime.deps.json`. It can also miss `clr_loader/ffi/dlls/clr.dll`
— the native CLR-host shim. Without those, the netfx loader on
Windows 11 (which has .NET Framework 4.8 preinstalled and should "just
work") loads the assembly but can't resolve its entry-point type,
crashing with `RuntimeError: Failed to resolve
Python.Runtime.Loader.Initialize from …\\Python.Runtime.dll` before
any window opens.

This was discovered when v0.5.3 and v0.5.4 Windows bundles crashed at
end-user launch on clean Windows 11 machines with that exact error
despite `Python.Runtime.dll` being physically present in
`gamepile/_internal/pythonnet/runtime/`. CI never caught it because
`--healthz-only` runs uvicorn alone and bypasses every code path that
touches `import clr`. The `collect_all` calls are gated to
`sys.platform == "win32"` — pythonnet isn't load-bearing on the Linux
GTK backend (no `import clr`) and is dead weight there.

### Frozen-aware resource resolution

PyInstaller flattens `app/main.py` into the bundle's top level, which
breaks `Path(__file__).parent / "static"` and the Jinja templates
directory lookup in `app/templates_config.py`.

`app/_resources.py` exposes `app_resource_dir()` returning:

- Dev mode: `Path(__file__).parent` (the package dir)
- Frozen mode: `Path(sys._MEIPASS) / "app"` (where PyInstaller's spec
  `datas=[('app/templates', ...)]` preserves the layout)

Both `main.py` (StaticFiles) and `templates_config.py` (Jinja2Templates)
route through it.

### uvicorn import-string vs app object

`uvicorn.Config("app.main:app", ...)` fails inside the bundle because
PyInstaller flattens the script entry point and dynamic resolution of
"app.main" no longer works. Changed to pass the `app` FastAPI object
directly. Side effect: uvicorn's auto-reload feature is unavailable in
the bundle (not used in production anyway).

### WebView2 runtime detection (Windows-only)

`app/main.py:_check_webview2_runtime()` probes three registry locations
for the Microsoft Edge WebView2 Runtime version key:

- `HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}\pv`
- `HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}\pv`
- `HKCU\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}\pv`

On missing runtime (Windows 11 LTSC, fresh Windows 10 installs), the
WebView2 Evergreen Standalone Installer page opens in the user's default
browser and the app exits cleanly. Better UX than letting pywebview's
Windows backend crash opaquely.

No-op on non-Windows platforms.

### CI smoke-test modes

The Windows CI smoke-test runs two complementary modes against the
built `gamepile.exe`. Both exit 0/1 with no display, no window, no
interactivity required.

**`--healthz-only`** — uvicorn/FastAPI half.

1. Starts uvicorn in a daemon thread (same path as production)
2. Polls `/healthz` for up to 30 seconds
3. Prints `ok` on success / `fail: <reason>` on timeout
4. Exits 0 / 1 — no pywebview, no GUI subsystem touched

Catches missing-hidden-import and path-resolution regressions in the
server-side bundle. 30s timeout absorbs slow cold-start on
windows-latest runners.

**`--check-windows-runtime`** — pywebview/.NET loader half.

1. `import clr` — triggers pythonnet → clr_loader → netfx →
   reflection lookup of `Python.Runtime.Loader.Initialize` in the
   bundled `Python.Runtime.dll`
2. `import webview.platforms.edgechromium` — forces pywebview's
   Windows backend module to load its C# bindings
3. Prints `ok` / `fail: <ExcType>: <msg>`, exits 0 / 1
4. On non-Windows: no-op exit 0 (so the same step is portable across
   the matrix without a separate `if:` guard at the workflow level)

No window opens; `clr.dll` activates the CLR but creates no UI, and
the edgechromium import loads bindings without instantiating
`EdgeChrome`. This is the exact failure class that ate v0.5.3 and
v0.5.4 — they passed `--healthz-only` because that flag never imports
clr, so the broken bundle shipped to users. v0.5.5+ catches that class
at build time, not at end-user launch.

What this does NOT cover: the actual "window appears, UI renders"
end-to-end check. That stays manual — download the zip on a clean
Windows machine, double-click `gamepile.exe`, confirm a window opens
before the release is considered validated.

## GitHub Actions workflow

`.github/workflows/release.yml`:

```
Trigger: push tag v* OR workflow_dispatch
Matrix:  os = [ubuntu-latest, windows-latest]

Both:
  checkout → setup-python 3.12 → setup-uv → install pyinstaller

Linux only:
  apt install libwebkit2gtk-4.1-0 libgirepository1.0-dev gir1.2-* python3-gi
  uv sync
  uv run python tests/run_tests.py     # gates the build
  uv run pyinstaller gamepile.spec
  cp README.bundled.md dist/gamepile/README.md
  tar -czf gamepile-<tag>-linux-x64.tar.gz dist/gamepile

Windows only:
  uv sync                              # PyGObject skipped via PEP 508 marker
  uv run pyinstaller gamepile.spec
  dist\gamepile\gamepile.exe --healthz-only   # expect "ok"
  Copy-Item README.bundled.md dist\gamepile\README.md
  Compress-Archive dist\gamepile gamepile-<tag>-windows-x64.zip

Upload:
  tag push     → softprops/action-gh-release@v2 published Release
                  (draft: false — tag push lands a public release with
                  both bundles attached, no manual promote step)
  dispatch run → actions/upload-artifact@v4 (14-day retention)
```

`fail-fast: false` on the matrix so a Windows-only break doesn't cancel
the in-flight Linux build (useful when iterating on platform-specific
spec issues).

## Distribution flow

1. Author runs `git tag vX.Y.Z && git push origin main --tags`
2. Workflow builds both bundles and publishes a Release directly (no
   draft intermediate)
3. Author downloads and smoke-tests both artifacts on local machines
   (or sends to a friend with the target OS)
4. If broken: delete the tag + Release, fix, re-tag with a patch bump
5. Friends download from <https://github.com/floxnt/gamepile/releases>

### Why direct-publish instead of draft

The original v5 design used `draft: true` on the gh-release action, on
the theory that a manual "Publish release" click would keep the author
in control of when a release went public. In practice that step was
never taken — v0.5.0 through v0.5.3 all landed as drafts that were
invisible to anonymous viewers of the Releases page, so to friends the
project looked like it had no releases at all. "Author controls when
releases go public" collapsed to "releases never go public," which is
strictly worse for a friend-distribution project.

The fix is `draft: false`: tag pushes publish immediately. The recovery
path for a bad build is the same as for any other public release —
delete the tag and the Release, patch-bump, re-tag. Friends will not be
running every tag the moment it ships; the latency between tag and
download already gives a window for "oh that one's broken, grab the
next one instead."

Versioning: semver from `v0.5.0`. Patch releases for bugfixes
(`v0.5.1`, `v0.5.2`), minor bumps for new features (`v0.6.0`),
`v1.0.0` when friend-validation has shaken the bugs out and the
app feels stable.

## Known limitations

### No code signing

Friends-first scope, signing certs cost real money for marginal trust gain
when the distribution channel is "I trust you, send me a link". Windows
users see SmartScreen warning on first launch; the bundled README
documents the "More info → Run anyway" path.

If GamePile ever ships beyond friends, code signing becomes the right
next investment. EV cert + per-build signing → no SmartScreen prompt.

### Cross-platform validation pending

The CI pipeline builds Windows and Linux artifacts end-to-end. The
**Windows artifact has not been run on a real Windows machine yet** —
the smoke test in CI verifies it imports + responds on /healthz, but not
that WebView2 actually renders the UI without surprises, that the
keychain integration works against Windows Credential Manager, that the
WebView2-missing path opens the right URL, etc.

Surfacing those issues is the v5.x patch-release loop: ship a build,
friend reports a bug, fix, re-tag. This is expected — the pipeline
existing is what unlocks the friend-validation phase.

### macOS skipped

No friends are on macOS. Adding `macos-latest` to the matrix later is a
one-line change once the demand materializes.

### Bundle size

Linux bundle is ~620 MB (GTK + WebKit + Python stdlib + httpx + FastAPI
deps + pywebview). Compressed via UPX where possible. Windows bundle
will be smaller (no WebKit — uses the OS's WebView2 runtime) but still
significant. Acceptable for download-once friend-distribution; would
need shrinking before public distribution.

### WebView2 runtime not bundled

Microsoft's distribution license for WebView2 specifies the **Evergreen
Bootstrapper** as the supported installation path; redistributing the
runtime files in-bundle isn't allowed. GamePile detects missing runtime
and opens the bootstrapper install page — that's the documented path.

### No Linux AppImage

The brief originally called out AppImage as a v5 deliverable, but the
PyInstaller --onedir path + GTK/WebKit system deps (which can't be
bundled portably anyway) made AppImage redundant. If a friend runs into
distro-version compatibility issues, AppImage with bundled GTK becomes
the right escalation.

## Tests

- `tests/test_paths.py` — 9 tests covering platformdirs resolution,
  legacy migration guard, hardcoded-XDG regression canary
- `tests/test_credentials.py` — 18 tests (existing, updated to use
  direct `_data_dir_env` patching now that the helper goes through
  platformdirs)
- `tests/run_tests.py` — new aggregate runner discovering every
  `tests/test_*.py` file; called by the CI's Linux build step before
  PyInstaller

PyInstaller bundle verified locally on Linux:
- `gamepile --healthz-only` prints "ok" within 30s
- Running the bundle without the flag serves `/healthz`, `/static/style.css`,
  and `/setup/welcome` (Jinja-rendered) identically to `uv run gamepile`

## Out of scope (deferred)

- macOS build in CI matrix (one-line addition when needed)
- Code signing (EV cert + Windows signtool / macOS notarization)
- WebView2 Runtime auto-install (Microsoft's bootstrapper path is what
  we use; auto-running it requires admin elevation we don't want to ask for)
- Auto-update mechanism (Sparkle / Squirrel / NSIS updater) — friends
  re-download a ZIP for now; auto-update is a v6 concern at earliest
- ARM64 builds (Windows on ARM, Linux ARM); x86_64 only in v5
- Bundle size optimization (UPX is enabled; aggressive stripping +
  exclude pruning is a v5.x patch-release task if size becomes a problem)
- Per-Linux-distro packaging (.deb, .rpm, Flatpak, Snap) — the tar.gz
  + system-libs documentation is enough for friend-distribution
