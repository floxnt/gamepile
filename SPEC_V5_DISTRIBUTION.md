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
`import clr` at module load. PyInstaller's binary auto-walk finds
`Python.Runtime.dll` itself but misses its non-DLL companions in
`pythonnet/runtime/`: `Python.Runtime.dll.config`,
`Python.Runtime.runtimeconfig.json`, `Python.Runtime.deps.json`. It
can also miss `clr_loader/ffi/dlls/clr.dll` — the native CLR-host
shim. `collect_all` closes that gap. Gated to `sys.platform == "win32"`
— pythonnet isn't load-bearing on the Linux GTK backend (no
`import clr`) and is dead weight there.

### Self-contained .NET runtime (Windows)

**Why bundled, not host-resolved.** pythonnet's default runtime
selection on Windows is the netfx (.NET Framework) loader. That worked
on the GitHub Actions `windows-latest` runner (a developer image with
.NET Framework 4.8.1 Developer Pack, multiple .NET Core SDKs, populated
GAC, registered AssemblyFoldersEx) and reliably failed on clean
consumer Windows 11 (in-box .NET Framework 4.8 only). The failure mode
was always the same: `Python.Runtime.dll` loaded successfully, but
`GetType("Python.Runtime.Loader")` returned null because some facade
assembly in its dependency chain didn't resolve — manifesting as
`RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from
…\Python.Runtime.dll` before any window opened. This shipped broken in
v0.5.3 (caught manually), v0.5.4 (caught manually after the empty-
releases-page distraction), and v0.5.5 (caught manually after a
CI-only smoke test produced a false negative — the runner satisfied
the facade resolution that the consumer machine could not).

The .NET Framework facade-resolution path under `Reflection.LoadFile`
is not formally specified, varies between 4.8.0 and 4.8.1, varies
between consumer and developer SKUs, and cannot be reliably simulated
on GitHub-hosted runners. Fixing netfx properly would mean shipping
manual facade-assembly DLLs alongside `Python.Runtime.dll` AND hoping
the host's .NET Framework cooperates AND accepting that CI can never
honestly test the failure mode against a clean consumer environment.
The simpler-and-strictly-better path is to stop depending on the host
.NET entirely.

**Architecture.** The release workflow downloads a pinned .NET 8 LTS
runtime (Microsoft.NETCore.App + Microsoft.WindowsDesktop.App,
8.0.27, SHA512-verified against the official Microsoft release
metadata), extracts it into `dotnet/runtime/`, and the spec bundles
it into the app at `_internal/dotnet/`. Both SKUs are needed:
NETCore.App is the base CLR + BCL; WindowsDesktop.App contains
System.Windows.Forms, which pywebview's edgechromium backend uses to
host the WebView2 control. NETCore.App alone is insufficient.

`app/main.py` runs a top-of-module block (Windows + frozen only) that:
1. Sets `DOTNET_ROOT` env var to `sys._MEIPASS / "dotnet"`.
2. Calls `pythonnet.set_runtime(clr_loader.get_coreclr(runtime_config=…))`
   pointing at the version-controlled `dotnet/Python.Runtime.runtimeconfig.json`
   (targets `net8.0` / `Microsoft.WindowsDesktop.App` 8.0.0).
3. Returns control to the import chain. Subsequent `import webview`
   triggers `import clr` which now uses the bundled coreclr runtime,
   not the host's netfx loader.

The set_runtime call is load-bearing on import order: it must run
BEFORE any module that pulls pywebview. The block carries a code
comment naming that constraint explicitly so a future refactor cannot
silently reintroduce the host-dependency crash by reordering imports.

**Bundle size impact.** Combined download for both runtime SKUs is
~70 MB compressed (NETCore.App 33.2 MB + WindowsDesktop.App 36.8 MB);
extracted in-bundle footprint pushes the published Windows zip from
~22 MB (v0.5.5) to ~80–100 MB (v0.5.6+). One-time download cost, not
per-launch. Acceptable for friend-distribution; would be the first
optimization target if size becomes a complaint.

**.NET runtime version pin.** Bundled runtime is .NET 8.0.27
(release 2026-05-12, LTS, support phase "maintenance", EOL
2026-11-10). Pinned by URL + SHA512 in both runtime download steps in
`.github/workflows/release.yml`. To bump for a security patch: update
version + both URLs + both SHA512s in the workflow's "Download and
verify bundled .NET 8 runtime" step (values come from the
`builds.dotnet.microsoft.com/dotnet/release-metadata/8.0/releases.json`
metadata, fields `releases[0].runtime` and `releases[0].windowsdesktop`).
Tracked in deferred housekeeping.

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

1. `import clr` — triggers pythonnet → clr_loader → bundled coreclr
   bootstrap → reflection lookup of `Python.Runtime.Loader.Initialize`
   in the bundled `Python.Runtime.dll`
2. `import webview.platforms.edgechromium` — forces pywebview's
   Windows backend module to load its C# bindings
3. **Verify `pythonnet.get_runtime_info().kind == "CoreCLR"`** —
   asserts the bundled coreclr runtime is actually active, not netfx
   fallback. If `set_runtime()` in `app/main.py`'s top-of-module block
   silently failed to apply (missing bundled runtime files, malformed
   runtimeconfig, import-order regression by a future refactor),
   pythonnet falls back to netfx and this assertion fails, catching the
   v0.5.3..v0.5.5-class regression at build time rather than at
   end-user launch.
4. Prints `ok: runtime=CoreCLR version=…` / `fail: <reason>`,
   exits 0 / 1
5. On non-Windows: no-op exit 0 (portable across the matrix without a
   separate `if:` guard at the workflow level)

No window opens; `clr.dll` activates the CLR but creates no UI, and
the edgechromium import loads bindings without instantiating
`EdgeChrome`.

### What CI can and cannot catch

CI's role on Windows is bounded and worth stating plainly so the
detector doesn't manufacture false confidence — that was v0.5.5's
exact failure mode (CI green, real machine crash, three releases lost
to the same illusion).

**The detector catches:**
- Bundle missing the .NET runtime directory entirely
- Bundle missing the custom runtimeconfig.json
- pythonnet / clr_loader data files missing (the v0.5.4 collect_all gap)
- `set_runtime()` silently failing to apply (e.g., a future refactor
  reorders imports so a pywebview-pulling module loads before the
  runtime-selection block runs)
- Any future regression where the bundled coreclr runtime fails to
  bootstrap

**The detector does NOT catch:**
- "Window appears, UI renders" — the GUI subsystem cannot be exercised
  without a display, and GitHub-hosted runners do not provide an
  attached desktop session
- WebView2 Runtime missing on the host (handled by the existing
  `_check_webview2_runtime()` startup probe, separately)
- File-system permission anomalies on user machines
- AV / SmartScreen interference at end-user launch

**The manual gate is structural, not a temporary state.** The
windows-latest runner is a developer image by definition: it carries
multiple .NET SDKs, the Framework Developer Pack, a populated GAC,
and registry hints that a clean consumer Windows 11 machine does not
have. The pre-v0.5.6 architecture (netfx with host-resolved facade
assemblies) failed precisely because that environmental gap could not
be reliably bridged in CI. The post-v0.5.6 architecture (bundled
coreclr) closes the gap **for the .NET loader chain specifically** —
because the relevant runtime is now the bundled one, identical on
runner and end-user machine. But it does not close the gap for the
GUI rendering layer, and CI runners will never honestly simulate a
clean consumer desktop session.

Concretely: **the `--check-windows-runtime` step passing in CI is
necessary but not sufficient for release acceptance. The
release-acceptance criterion is, and will remain, the manual
download-and-double-click test on a real clean consumer Windows
machine.** Do not promote a release from "CI green" to "ship to
friends" without that manual check.

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
- **Revisit bundled .NET runtime pin before .NET 8 LTS EOL (2026-11-10).**
  The .NET 10 LTS lands Nov 2026; once it's proven (a few months of
  patch-release maturity), bump the bundled runtime. Don't preempt — pin
  is revisited on the EOL clock, not on every minor .NET update.
- **Update pinned .NET 8 runtime URL/SHA when a security patch warrants.**
  The pin in `.github/workflows/release.yml` is to 8.0.27 (2026-05-12).
  When a security advisory lands for a later 8.0.X, bump both URLs and
  both SHA512s from `builds.dotnet.microsoft.com/dotnet/release-metadata/
  8.0/releases.json`. Same pattern as the Node 20 / windows-2025
  deferred items: pinned URLs are expected to go stale; the discipline
  is to track that staleness explicitly rather than let it rot silently.
