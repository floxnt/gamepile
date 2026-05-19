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

### Load-bearing distribution property: install-location vs data-location separation

The platformdirs audit started as a portability fix — XDG patterns don't
resolve on Windows. As of v0.8.0 (Inno Setup installer), the same
separation became a **load-bearing distribution-layer property** that the
installer/uninstaller scope depends on:

- The Windows installer writes program files to `{userpf}\GamePile`
  (resolves to `%LocalAppData%\Programs\GamePile`) under
  `PrivilegesRequired=lowest`.
- The app reads/writes its database, logs, and any future per-user state
  to `%LocalAppData%\gamepile` via the platformdirs call. Note the
  distinct directory — `\Programs\GamePile` vs `\gamepile`.
- The uninstaller's scope is the installer's `{app}` directory only. The
  data directory is never written to by the installer or referenced in
  any `[UninstallDelete]` entry.

This is what makes the upgrade-preserves-DB and uninstall-preserves-DB
guarantees structurally true rather than convention. A regression that
moved the data directory to live *inside* the install directory would
silently invert both guarantees: upgrade would replace user data,
uninstall would delete it. The `tests/test_paths.py` regression canary
exists partly to catch that class of mistake.

**Do not move the data directory under the install directory** — not for
"convenience," not for "portable mode," not for anything short of an
explicit decision that revisits the installer scope. If portable mode is
ever wanted, it needs a separate code path that the installer build does
not exercise.

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

### WebView2 binding override (Windows)

**Why an override is needed.** pywebview 6.2.1 ships
`Microsoft.Web.WebView2.{Core,WinForms}.dll` under `webview/lib/`,
built for `.NETFramework v4.6.2`. The WinForms binding exposes a
`ContextMenu` property; that type was removed from
`System.Windows.Forms` in .NET Core 3.0+ and replaced by
`ContextMenuStrip`. Under the bundled .NET 8 coreclr runtime,
pythonnet's reflection over the WebView2 class hits the dead-type
reference and throws `System.TypeLoadException: Could not load type
'System.Windows.Forms.ContextMenu'` at module load — before any
window opens. v0.5.6 caught this in CI via the `--check-windows-runtime`
detector after the bundled-coreclr architecture went in; v0.5.6's
Windows artifact never published as a result.

**The fix.** Replace pywebview's bundled net462 binding DLLs with the
`netcoreapp3.0`-TFM variants from the same `Microsoft.Web.WebView2`
NuGet package version (1.0.3856.49 — matches the WebView2 binding
version pywebview itself ships, just the .NET Core target instead of
the .NET Framework target). The netcoreapp3.0 WinForms.dll only
references `ContextMenuStrip` (which exists in both .NET Framework
and .NET 8), not `ContextMenu`. Verified empirically via `strings`
against the extracted DLLs.

**Upstream context.** pywebview issue #1803 documents the exact
failure mode, recommends an equivalent fix (different specific version
but same approach), and references a fork branch with the upstream-
aware patch. The issue is open as of v0.5.7; pywebview has not yet
released a version that incorporates the fix. When pywebview does
ship a release with the binding update, the workflow's download step
and the spec's filter can both be removed and this section retired.

**Implementation — two layers.** The override applies in two places
because the spec-level layer turned out to be insufficient on its own.

*Layer 1 (intent, documentation, belt-and-braces — NOT load-bearing).*
`.github/workflows/release.yml` "Download and verify WebView2
netcoreapp3.0 binding override" step (Windows-only): fetches the
NuGet package by pinned URL, SHA512-verifies the bytes, extracts the
two `lib_manual/netcoreapp3.0/` DLLs into `webview2_override/`.
`gamepile.spec` filters `Microsoft.Web.WebView2.{Core,WinForms}.dll`
out of `collect_all("webview")` output (with a sanity-raise if
nothing matched). This layer removes the net462 DLLs from
PyInstaller's tracked collection, but **pywebview ships a PyInstaller
hook at `webview/__pyinstaller/hook-webview.py` that PyInstaller's
`Analysis()` invokes independently during its module-dependency
walk**. The hook calls `collect_data_files('webview', subdir='lib')`
and `collect_dynamic_libs('webview')`, which re-discover and re-add
the same net462 DLLs after the spec-level filter ran. v0.5.7 shipped
broken because this hook-rediscovery mechanism wasn't accounted for.

*Layer 2 (load-bearing — the actual override).* After `pyinstaller`
finishes writing `dist/gamepile/`, a Windows-only workflow step
`Copy-Item -Force`s the two netcoreapp3.0 DLLs over their final
bundle path at `dist/gamepile/_internal/webview/lib/`, then
SHA512-verifies the post-copy bytes against pinned hashes. This
layer is filesystem-level last-write-wins — deterministic,
unambiguous, immune to PyInstaller hook behavior. The post-copy SHA
check is non-optional: every silent-success assumption in this arc
has been wrong, so the bundle layer asserts that the bytes actually
landed.

pywebview's `interop_dll_path` (`webview/util.py:480-497`) resolves
to `<_MEIPASS>/webview/lib/<dll_name>` first when frozen, which in
the onedir bundle is `dist/gamepile/_internal/webview/lib/<dll_name>`
— exactly where the post-build copy targets. Verified by reading the
function directly, not theorized.

The two-layer structure costs ~30 lines of non-load-bearing spec
code that documents intent + provides belt-and-braces if a future
PyInstaller release changes the hook-rediscovery behavior. Removing
the spec filter would make the post-build `Copy-Item` look like an
unexplained hack.

### pywebview winforms.py patch (Windows)

**Why a patched winforms.py is needed.** Even with the bundled .NET 8
runtime and the netcoreapp3.0 WebView2 binding override, pywebview
6.2.1's `webview/platforms/winforms.py` crashes at module-load on
.NET 8 coreclr. The `OpenFolderDialog` class body (lines 668-693)
reflects over .NET-Framework-internal types
(`System.Windows.Forms.FileDialogNative+IFileDialog`,
`System.Windows.Forms.FileDialogNative+FOS`) that do not exist in
.NET 8's WinForms (`Microsoft.WindowsDesktop.App`). `GetType()`
returns `None` for them; the next line calls `.GetMethod(...)` on
the `None`; `AttributeError: 'NoneType' object has no attribute
'GetMethod'` propagates out of the module import, gets caught by
`webview/guilib.py:73-76`'s `except ImportError` (or propagates past
it depending on the Python version), and surfaces to the user as the
misleading `WebViewException: You must have pythonnet installed in
order to use pywebview.`

This is `OpenFolderDialog`'s class body evaluation — it runs at
import time regardless of whether the folder picker is ever
invoked. GamePile doesn't use the folder picker; that's irrelevant
to the crash. The whole module fails to load.

v0.5.7 / v0.5.8 didn't surface this because the prior layer (the
v0.5.6 WebView2 binding TypeLoadException) was crashing earlier in
the chain. With the binding override in place, the import got far
enough to evaluate `OpenFolderDialog`'s class body and the next .NET
8 incompatibility surfaced. v0.5.9's diagnostic instrumentation
made it visible in CI stderr; v0.6.0 fixes it.

**The fix — vendor a patched winforms.py.** pywebview issue #1803
tracks this exact problem; the smparkes:fix/dotnet8-coreclr fork
branch (commit `a2bf0df4a4728b170cb02f1f1f698387fbaf0379`) carries
the upstream-aware fix. The branch makes three changes to
winforms.py:

1. Adds `clr.AddReference('Microsoft.Win32.SystemEvents')` when
   PYTHONNET_RUNTIME is coreclr. On .NET 8 the SystemEvents type
   lives in a separate assembly; the AddReference is a no-op if
   already loaded transitively (which it appears to be when bundled
   `Microsoft.WindowsDesktop.App` is in play on Windows x64) but
   defensive for stripped runtimes / Windows ARM64.
2. Guards the `_is_chromium()` → `edge_build()` .NET Framework
   registry probe with `if not is_coreclr:` and fixes a latent
   UnboundLocalError on the `finally: winreg.CloseKey(net_key)`
   path. Defensive against environments where .NET Framework isn't
   registered (e.g. Windows ARM64).
3. Splits `OpenFolderDialog` into two implementations gated by
   `PYTHONNET_RUNTIME`. The netfx branch keeps the existing
   FileDialogNative reflection. The coreclr branch uses the public
   `WinForms.FolderBrowserDialog` (which exists in both .NET
   Framework and .NET 8) and accepts but ignores the multi-folder
   selection parameter (FolderBrowserDialog is single-select). This
   is the load-bearing fix for our crash.

(The branch also updates pywebview's bundled WebView2 binding DLLs.
Those overlap with our v0.5.8 post-build binding override — the
override wins post-build regardless, so smparkes' bundled DLLs are
irrelevant to us. We pick up only the Python source change.)

We apply all three sub-fixes, not just #3. Sub-fixes #1 and #2 are
defensive against .NET 8 environment variance that our
windows-latest CI runner doesn't exhibit (.NET Framework 4.8 is in-
box, the runner is x64 not ARM64) but a real consumer machine might.
The thread's hardest lesson is "CI environment ≠ consumer
environment" — applying the complete patch inherits smparkes'
complete hardening rather than a hand-picked subset we'd have to
revisit when a consumer machine diverges from our runner.

**Implementation.** `vendor/pywebview/winforms.py` is a copy of the
smparkes-patched winforms.py with a provenance header documenting
its derivation from pywebview 6.2.1 at the pinned smparkes commit
SHA, issue #1803 as upstream context, and the delete-when-upstream-
ships condition. SHA512 of the full vendored file (header + body) is
pinned in `.github/workflows/release.yml`.

**`PYTHONNET_RUNTIME=coreclr` env var coupling.** The smparkes patch
gates its .NET 8 compatibility branches on
`os.environ.get('PYTHONNET_RUNTIME') == 'coreclr'` — the public
documented way pywebview detects coreclr mode. Our `app/main.py`
top-of-module set_runtime block must set this env var alongside the
explicit `pythonnet.set_runtime()` call. The explicit API call still
drives the actual runtime selection (it sets `pythonnet._RUNTIME`
which wins over the env var fallback inside `import clr`); the env
var is purely a marker for the smparkes-patched code to read.

v0.6.1 shipped broken precisely because we'd set the runtime via the
explicit API but not set the env var: the patch's coreclr branches
never fired, the original OpenFolderDialog class body ran, and
AttributeError on `iFileDialogType.GetMethod` raised exactly as in
v0.5.9. The `--check-windows-runtime` three-stage assertions passed
(coreclr runtime, netcoreapp3.0 WebView2 binding, no ContextMenu)
because they don't cover the full pywebview load chain. v0.6.2's
detector tightens this gap: any exception emitted during the chain
walk fails the detector at the end, regardless of whether the
later assertions would pass.

The release workflow's "Apply pywebview winforms.py patch" step
(Windows-only) runs after `uv sync` and before `Install PyInstaller`:

1. SHA512-verify `vendor/pywebview/winforms.py` (source bytes).
2. Locate `.venv\Lib\site-packages\webview\platforms\winforms.py` —
   refuse if zero or multiple matches (layout-change canary).
3. `Copy-Item -Force` the vendored file over the venv file.
4. SHA512-verify the destination post-copy — same hash as the source.

The double-verify pattern is identical to the v0.5.8 WebView2 binding
override. PyInstaller then runs against the patched venv and bundles
the patched winforms.py.

**Empirical reflection inventory.** Audited all `GetType` /
`GetMethod` / `GetField` / `GetConstructor` / `LoadWithPartialName`
calls in pywebview 6.2.1's `winforms.py`. All 13 hits are inside
the `OpenFolderDialog` class body. The other module-level class
(`BrowserView`) uses only public .NET 8-compatible WinForms types
(no reflection over internals). The 23 top-level functions are not
evaluated at module-load. So `OpenFolderDialog` is the last
class-body crash site under .NET 8 — there is no "next layer"
lurking once it's guarded.

**Discipline: audit the caller-side interface, not just the patch.**
When vendoring a third-party patch, the diff tells you what the patch
*does*; it does not tell you what the patch *expects from its caller*.
v0.6.0 / v0.6.1 read the smparkes diff correctly and applied it cleanly
— and the patch still didn't fire because we never set the
`PYTHONNET_RUNTIME` env var the patch reads to decide which branch to
take. The code was right; the interface between our code and the patch
was wrong. The patch's `if`/`else` gates are caller-side preconditions
in disguise.

Before declaring a vendored patch applied, enumerate and verify:

- Every environment variable the patch reads (`os.environ.get(...)`)
- Every config the patch reads from `sys`, globals, or files
- Every callable the patch expects to find on the caller side
- Every assumption the patch makes about import order

…and confirm each is satisfied by our calling code. This rule matters
again whenever pywebview's upstream eventually ships a fix and we
re-vendor or drop the patch — the new code's gating model may differ
and the same audit must repeat.

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
3. **Stage 1 assertion: `pythonnet.get_runtime_info().kind == "CoreCLR"`** —
   asserts the bundled coreclr runtime is actually active, not netfx
   fallback. Catches a v0.5.3..v0.5.5-class regression where
   `set_runtime()` in `app/main.py`'s top-of-module block silently
   failed to apply.
4. **Stage 2 assertion: WebView2.WinForms assembly's
   `TargetFrameworkAttribute` contains `"NETCoreApp"`** — asserts the
   binding-override mechanism applied (the netcoreapp3.0 DLL is loaded,
   not pywebview's bundled net462 DLL). Catches a v0.5.6-class
   regression where the spec filter silently didn't apply
   (e.g., pywebview's layout changed and the filter no longer matches,
   override files weren't staged by the workflow, datas precedence
   silently changed).
5. **Stage 3 assertion: WebView2 type exposes `ContextMenuStrip` and
   not `ContextMenu`** — asserts the actual broken-in-v0.5.6 symptom
   is gone. Belt-and-braces against "right DLL loaded but somehow
   still has the broken reference."
6. Prints `ok: runtime=CoreCLR version=… webview2_tfm=…` /
   `fail: stage N — <reason>`, exits 0 / 1
7. On non-Windows: no-op exit 0 (portable across the matrix without a
   separate `if:` guard at the workflow level)

No window opens; `clr.dll` activates the CLR but creates no UI, and
the edgechromium import loads bindings without instantiating
`EdgeChrome`. The three-stage assertion structure deliberately
duplicates coverage between mechanism (stages 1, 2) and symptom
(stage 3) — single-check passing while reality fails is the recurring
failure mode of this whole arc; redundant checks at different layers
is the cheap insurance.

**Chain-walk exception gate (added v0.6.2).** Before the three-stage
assertions run, the detector walks the load chain (`import clr`,
`pythonnet.get_runtime_info()`, `import webview`, `import
webview.platforms.winforms`, `import webview.platforms.edgechromium`,
WebView2 binding resolution) and unmasks any exception swallowed at
each step via `_dump_exc()`. A module-level `_CHAIN_EXC_COUNT` counter
increments on every `_dump_exc()` call. After the walk, the detector
fails the run if the counter is non-zero, regardless of whether the
three-stage assertions would subsequently pass. This is structurally
necessary, not belt-and-braces — see the discipline note below.

**Discipline: every new chain layer needs an assertion AT that layer.**
v0.6.1 shipped broken with all three stage assertions passing: the
runtime was CoreCLR (stage 1), the WebView2 binding was netcoreapp3.0
(stage 2), and the WebView2 type exposed `ContextMenuStrip` not
`ContextMenu` (stage 3) — but `import webview.platforms.winforms` was
raising `AttributeError` from `OpenFolderDialog`'s class body and being
swallowed before the assertions ran. None of the three stages asserted
that the pywebview Windows backend module actually imported, because at
design time the chain only had three layers; the fourth (the winforms
import) was added implicitly when the patch was vendored and silently
inherited the prior stages' coverage.

The chain-walk exception gate closes the specific gap. The general rule
is broader:

- **When you add a new layer to the load chain, add an assertion AT
  that layer.** Do not delegate the new layer's coverage to assertions
  further down the chain.
- **Downstream assertions test that the chain produced the right
  artifact; they cannot, by construction, prove the chain ran without
  swallowing errors along the way.** Asserting the final artifact's
  properties says nothing about whether intermediate steps swallowed
  exceptions to get there.
- **A "false green" — CI passing while the bundle is broken — is more
  expensive than CI failing on a real issue.** The cost is not the
  failed release; it is the eroded trust in green CI as a signal,
  which makes the next debugging round start from doubt instead of
  evidence.

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
  Apply pywebview winforms.py patch (SHA-verify source + destination)
  Download + SHA-verify WebView2 netcoreapp3.0 binding override
  Download + SHA-verify bundled .NET 8 runtime
  uv run pyinstaller gamepile.spec
  Apply WebView2 binding override to built bundle (SHA-verify in-bundle)
  dist\gamepile\gamepile.exe --healthz-only           # expect "ok"
  dist\gamepile\gamepile.exe --check-windows-runtime  # expect "PASSED"
  Copy-Item README.bundled.md dist\gamepile\README.md
  choco install innosetup --no-progress -y
  iscc.exe /DAppVersion=<tag-without-v> installer\gamepile.iss
    → gamepile-setup-<tag>.exe in repo root
    (≥100 MB plausibility-floor check; fail-fast if smaller)

Upload:
  tag push     → softprops/action-gh-release@v2 published Release
                  (draft: false — tag push lands a public release with
                  the Linux .tar.gz and the Windows installer .exe
                  attached, no manual promote step)
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

## Inno Setup installer (Windows, v0.8.0+)

Through v0.7.0, the Windows release artifact was a raw
`gamepile-vX.Y.Z-windows-x64.zip` — the user extracted the archive, found
`gamepile.exe` inside, and double-clicked it. No installer, no
Add/Remove Programs entry, no uninstaller, no upgrade path beyond
"delete the old folder, extract the new one in its place."

v0.8.0 replaces that with a proper per-user Inno Setup installer:
`gamepile-setup-vX.Y.Z.exe`. The script lives at
`installer/gamepile.iss`. The raw `.zip` artifact is gone — the
installer is the only Windows release artifact attached to a Release.

### Architecture decisions (locked at v0.8.0)

These are inputs to this section, not open for re-litigation:

1. **Inno Setup, not MSIX / WiX / auto-update-from-app.** Inno is free,
   mature, well-documented, and has the lowest friction-to-effort ratio
   for friend-scale distribution. It pairs cleanly with the existing
   PyInstaller `--onedir` output.
2. **Per-user install, NOT per-machine.** `PrivilegesRequired=lowest` in
   the `.iss`. No admin elevation, no UAC prompt. Matters for the
   SmartScreen-shy friend audience: the unsigned "More info → Run
   anyway" click is already the friction gate; adding a second UAC
   dialog on top would compound the friction without buying any
   meaningful trust signal.
3. **The installer REPLACES the zip.** Friends get exactly one Windows
   download per release. No alongside-shipping, no "should I get the
   installer or the zip" confusion. The workflow's Publish-Release step
   attaches only the `.exe` (and the unchanged Linux `.tar.gz`).
4. **Unsigned, code signing deferred.** SmartScreen click-through
   ("More info → Run anyway") remains the expected first-launch
   experience. See "No code signing" below — the deferral rationale is
   unchanged from v0.5.0.

### Stable AppId GUID

Inno detects existing installations and routes them through its
upgrade-in-place path by matching on `AppId`. The GamePile AppId GUID
is **fixed at `{D72A3C2F-1F81-4B71-80C5-AFF7276673BD}`** and committed
as a literal constant in `installer/gamepile.iss`. Generated once via
`uuid.uuid4()` on 2026-05-19; it stays the same forever.

Do NOT regenerate this on a future version bump, refactor, or "clean
install" intention. Changing the AppId would orphan every existing
installation: Inno would treat the new installer as a fresh app and
not detect the prior install, so:

- The old install's program files would stay at
  `{userpf}\GamePile` (no upgrade-replace).
- The new install would land at the same path (in the best case) or
  silently elsewhere (worse).
- The Add/Remove Programs list would acquire two GamePile entries.
- The uninstall paths would no longer match the user's mental model.

This is exactly the kind of "deliberately preserved across versions"
identity that has to be load-bearing, not convention. If a future
maintainer ever genuinely wants to rebrand or re-identify the install
(e.g., a deliberate fork that should not upgrade-replace existing
GamePile installs), that decision is explicit and documented in a
follow-up phase; until then, the GUID does not move.

### Install location and scope

`DefaultDirName={userpf}\GamePile`. Under `PrivilegesRequired=lowest`
this resolves to `%LocalAppData%\Programs\GamePile`, which is the
standard per-user Programs directory since Inno Setup 6 and Windows 10.

The installer's `[Files]` section sources the entire PyInstaller
`--onedir` payload (`dist\gamepile\*` from the repo root) with
`recursesubdirs createallsubdirs ignoreversion`. The bundled
`README.md` (a copy of `README.bundled.md`) ships inside the install
directory as it did with the zip.

### User data preservation across uninstall

The installer **does not touch `%LocalAppData%\gamepile`** on install,
upgrade, or uninstall:

- No `[Files]` entry references the data directory.
- No `[UninstallDelete]` entry references the data directory.
- No `[Code]` section deletes anything outside `{app}`.

User data (the SQLite `gamepile.db`, logs, future per-user state)
survives every installer operation by virtue of the
install-location-vs-data-location separation documented in the
"Path-resolution audit" section above. That separation is a
load-bearing distribution-layer property — see the explicit
`Do not move the data directory under the install directory`
guidance there.

**No "also remove user data" checkbox in the uninstaller.** The cost
asymmetry is severely one-sided: misclick cost is months of
accumulated Steam sync + manual classifications + pick history +
affinity weights, gone irrecoverably. No-checkbox cost is ~10 KB of
leftover folder that anyone who actually wants it gone can delete
manually. Every well-behaved Windows app the friend audience runs
(Discord, Steam, browsers, Zoom) leaves user data on uninstall by
default — deviating from that mental model is surprising-in-a-bad-
direction. The decision is documented inline in the `.iss` script.

For the rare case where someone genuinely wants a fully-clean
removal, `README.bundled.md` documents the one-line manual cleanup
(delete `%LocalAppData%\gamepile` after uninstall). Deliberately
unceremonious — friction comes from being a manual filesystem
action, not from scary CAPS warnings.

### Upgrade-over-prior-version

The installer detects an existing install by `AppId`, prompts the
user about replacement (default-accept), and replaces the program
files in place. `CloseApplications=yes` handles the case where the
user runs the new installer while the old GamePile is still
running — Inno offers to close it cleanly rather than failing the
file replace. `RestartApplicationsIfNeeded=no` prevents an
auto-restart loop on a misbehaving Close.

The user's data at `%LocalAppData%\gamepile` is untouched (see
preceding section). Upgrade-preserves-DB is a structural property
of the install-vs-data separation, not a feature of the upgrade
code path.

### Publisher string

`AppPublisher=floxnt`. Matches the GitHub repo owner pseudonym
(`github.com/floxnt/gamepile`) used throughout the project. The
Publisher field in Add/Remove Programs identifies the maker, not
the app — the convention is "Microsoft" publishes "Visual Studio
Code," not "Visual Studio Code" publishes "Visual Studio Code."

### Version string flow

The version is **not hardcoded** in the `.iss`. The workflow strips
the leading `v` from `github.ref_name` (e.g., `v0.8.0` → `0.8.0`)
and passes it to ISCC via `/DAppVersion=0.8.0`. The `.iss` `#error`s
if `AppVersion` is not defined, so a misconfigured manual invocation
fails fast.

- `AppVersion={#AppVersion}` — the bare semver, shown in Add/Remove
  Programs per Windows convention.
- `VersionInfoVersion={#AppVersion}` — the file-version metadata
  embedded in the installer .exe.
- `OutputBaseFilename=gamepile-setup-v{#AppVersion}` — the installer
  re-adds the `v` prefix so the artifact filename matches the
  project's `gamepile-setup-vX.Y.Z.exe` convention.

### SHA-pinning discipline (formalized at v0.8.0)

The workflow SHA-pins multiple binaries: the bundled .NET 8 runtime
zips, the WebView2 netcoreapp3.0 NuGet package, the WebView2 DLLs
inside the built bundle, the vendored pywebview `winforms.py`. The
v0.8.0 installer phase deliberately does NOT SHA-pin the Inno Setup
compiler installed via chocolatey. The principle making both
choices coherent:

> **SHA-pin what ships TO USERS in the bundle. Don't ceremonially
> SHA-pin tools that only run on the CI host and never reach the
> user.**

The .NET 8 runtime, WebView2 binding DLLs, and patched pywebview
`winforms.py` all live in the user's filesystem after install — if
their identity is wrong, the user gets wrong code. Pinning protects
the user-facing output. ISCC.exe runs only on the GitHub-hosted CI
runner, compiles the installer once, and never reaches the user;
pinning it would add maintenance burden (chase chocolatey package
SHA on every refresh of windows-latest's runner image) without
protecting any user-visible output. Chocolatey + the
windows-latest base image is the trust boundary, and that boundary
is already in place for the rest of the build (Python, uv,
PyInstaller itself, the OS itself).

This discipline applies prospectively: future workflow additions
should ask "does this binary ship to users?" before deciding
whether SHA-pinning is load-bearing or ceremonial. Ceremonial
pinning compounds maintenance burden over time; load-bearing
pinning is non-negotiable.

### What CI cannot verify (v0.8.0 phase)

The workflow does the most CI can do:

- Compiles the `.iss` (ISCC exit code 0)
- Asserts the produced `.exe` exists at the expected path
- Asserts the `.exe` size is above a 100 MB plausibility floor (fails
  fast if the `[Files]` section silently caught nothing)
- Attaches the artifact to the Release on tag push

**CI cannot run the installer.** It cannot click through the wizard,
cannot confirm SmartScreen behavior, cannot launch the installed
app, cannot verify a window opens, cannot verify the upgrade-over-
prior-version flow preserves DB, cannot verify the uninstaller
behaves correctly, cannot verify the Add/Remove Programs entry
shows the right publisher string.

CI's coverage for the installer phase is bounded to "the `.iss`
compiled, produced a plausibly-sized `.exe`, and both jobs were
green." Treat this bound as the explicit honest scope, not an
implicit upper limit. See "Release acceptance is the manual test"
below.

### Release acceptance is the manual install/launch/upgrade/uninstall test

The structural release-acceptance criterion for any release that
touches the installer is the **manual hardware gate on a real
consumer Windows 11 machine**. CI green is necessary, never
sufficient. Same rule as the v0.6.2 saga — the same failure mode
that made CI-only validation insufficient for the bundle load chain
makes CI-only validation insufficient for the installer behavior.

The manual gate has four sub-tests:

1. **Install on a clean Windows 11 session.** Download
   `gamepile-setup-vX.Y.Z.exe`, double-click, click through
   SmartScreen ("More info → Run anyway"), complete the wizard,
   confirm "Launch GamePile" closes the wizard and opens the app
   window.
2. **Upgrade-over-prior-version.** With a prior version (e.g.,
   v0.7.0's unzipped folder or a prior installer-built version)
   already present, run the new installer. Confirm the upgrade
   completes, the app launches with the new version string, and
   the user's library / sync state / ratings / pick history are
   preserved at `%LocalAppData%\gamepile`.
3. **Add/Remove Programs entry.** Open Settings → Apps → Installed
   apps. Confirm GamePile is listed with Publisher = `floxnt` and
   Version = the bare semver.
4. **Uninstall.** Run the uninstaller from Add/Remove Programs.
   Confirm the program files at `{userpf}\GamePile` are removed,
   `%LocalAppData%\gamepile` is **untouched**, and re-running the
   installer afterward succeeds as a fresh install.

All four sub-tests pass = release validated. Any sub-test fails =
release reverted, the tag flagged prerelease with a release-specific
note, and the failure diagnosed before the next attempt.

Until the manual gate clears, **the Release object is flagged
prerelease with a plain note** explaining what's awaiting validation.
Friends should not be directed at a non-validated installer release.

## Distribution flow

1. Author runs `git tag vX.Y.Z && git push origin main --tags`
2. Workflow builds both bundles and publishes a Release directly (no
   draft intermediate). On v0.8.0+ the Release is flagged prerelease
   with an awaiting-manual-validation note until the author completes
   the manual hardware gate (see "Release acceptance is the manual
   install/launch/upgrade/uninstall test" above).
3. Author runs the manual gate on their Windows 11 machine. On pass,
   the prerelease flag is removed and the Release is promoted to
   canonical.
4. If the manual gate fails: delete the tag + Release, fix, re-tag
   with a patch bump.
5. Friends download from <https://github.com/floxnt/gamepile/releases>
   only the latest canonical (non-prerelease) Release.

### Why direct-publish instead of draft

The original v5 design used `draft: true` on the gh-release action, on
the theory that a manual "Publish release" click would keep the author
in control of when a release went public. In practice that step was
never taken — v0.5.0 through v0.5.3 all landed as drafts that were
invisible to anonymous viewers of the Releases page, so to friends the
project looked like it had no releases at all. "Author controls when
releases go public" collapsed to "releases never go public," which is
strictly worse for a friend-distribution project.

The fix is `draft: false`: tag pushes publish immediately. The
prerelease flag (added in v0.8.0 as the awaiting-manual-validation
marker) achieves the "author controls when this counts as canonical"
intent without invisibility: the Release is visible on the Releases
page but explicitly marked not-yet-validated. Friends know to wait;
the previous-canonical Release stays as the latest non-prerelease.

The recovery path for a bad build is the same as for any other
public release — delete the tag and the Release, patch-bump, re-tag.

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

v0.8.0's Inno Setup installer does not change this calculation. Both the
installer .exe and the installed `gamepile.exe` ship unsigned. The
SmartScreen prompt now appears on the *installer* run (not the
post-extract `gamepile.exe` double-click as it did with the .zip flow),
but the click-through is the same. If GamePile ever ships beyond
friends, code signing becomes the right next investment — EV cert +
per-build signing on both artifacts gets rid of the SmartScreen prompt.
See the "Revisit code signing" deferred-housekeeping item below.

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

### No Linux AppImage (yet)

The brief originally called out AppImage as a v5 deliverable. The
PyInstaller --onedir path + GTK/WebKit system deps (which can't be
bundled portably anyway) made AppImage redundant for the initial
friend-distribution scope: Linux ships a `.tar.gz` with a documented
"install the GTK/WebKit packages your distro uses, run the binary"
flow.

v0.8.0's Inno Setup installer for Windows reframes this as a
distribution-parity gap rather than a redundancy: Windows friends now
get a proper double-click installer with Add/Remove integration, while
Linux friends still get a raw tar.gz to extract by hand. Closing that
gap with an AppImage build (bundled GTK or relying on host GTK with a
clear error message) is queued as a separate post-installer-validation
phase — see the "Linux installation parity via AppImage" deferred-
housekeeping item below for the full scope description. Not in scope
for v0.8.0 itself.

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
- **Drop the WebView2 binding override when pywebview ships an upstream
  fix.** pywebview issue #1803 tracks merging a .NET-Core-compatible
  bundled WebView2 binding into the upstream package. When that lands
  in a pywebview release and we bump to it, the workflow's
  "Download and verify WebView2 netcoreapp3.0 binding override" step,
  `gamepile.spec`'s `_is_pywebview_net462_binding` filter, and the
  `webview2_override_datas` entry can all be removed and the
  "WebView2 binding override" section retired. Until then, the pin
  is 1.0.3856.49 (matching pywebview's own bundled version, just the
  netcoreapp3.0 TFM variant). If NuGet ever serves different bytes for
  the same version, the workflow's SHA512 check fails fast.
- **Drop the vendored pywebview winforms.py patch when pywebview ships
  an upstream fix.** Same pywebview issue #1803 also tracks merging
  the smparkes:fix/dotnet8-coreclr .NET 8 compatibility patch into
  upstream. When that lands in a pywebview release and we bump to it,
  delete `vendor/pywebview/`, remove the workflow's "Apply pywebview
  winforms.py patch" step, retire the "pywebview winforms.py patch"
  SPEC section, and bump the pywebview pin in `pyproject.toml`. Until
  then, the vendored file is pinned to smparkes commit
  `a2bf0df4a4728b170cb02f1f1f698387fbaf0379`.
- **Refactor SPEC_V5_DISTRIBUTION.md into v5-baseline + v5.x-maintenance-log
  sections** once the Windows-bundle saga is closed and validated. The
  v0.5.x → v0.6.0 arc grew this SPEC substantially with three layered
  workarounds (bundled .NET 8 runtime, WebView2 binding override,
  pywebview winforms.py patch), each tied to a specific failure mode
  the project hit. The current append-in-place layout served the
  incident well — every fix is documented next to its rationale — but
  reads as one long "what we tried" log rather than a clean spec. After
  v0.6.0 is hardware-validated and a few patch releases settle the dust,
  separate the original v5 design into a baseline section and the
  layered Windows workarounds into a maintenance-log section. Do NOT
  refactor mid-incident; refactor from a position of stability.

- **Linux installation parity via AppImage** — queued
  post-Windows-installer-validation, separate phase. v0.8.0 closed
  half the install-experience gap (Windows now ships a proper
  installer); Linux still ships a raw tar.gz with manual GTK/WebKit
  package install. An AppImage build that either bundles GTK or fails
  with a useful "install these packages, then retry" message would
  close the parity gap. Two design questions to answer when this
  phase opens: (1) bundle GTK or rely on host (size vs portability
  tradeoff — Discord's AppImage bundles GTK; some other apps don't),
  (2) keep the tar.gz as a power-user fallback alongside AppImage, or
  retire it entirely. Do NOT pull this work into the v0.8.0 installer
  phase mid-scope; it gets its own phase decided from a position of
  Windows-installer stability.

- **Revisit code signing if/when friend-count grows.** The unsigned-
  shipping decision was right for the friend-distribution scope of
  v0.5.0–v0.8.0: SmartScreen warning on a handful of friends'
  machines is friction, but a sub-$200/year EV-cert annual cost is
  not justified by the trust gain at that scale. The calculus
  changes if the project grows — SmartScreen reputation accrues
  with signed-binary install volume; without signing, every new
  friend pays the click-through cost forever. Re-evaluate when (a)
  the user base is meaningfully wider than a small friend group, or
  (b) feedback explicitly names SmartScreen as a deterrent to
  adoption. The installer (.exe) and the installed gamepile.exe
  both need signing; one without the other is incomplete. EV cert
  is the right tier — OV cert produces a smaller-warning click-
  through, not no warning. Until then: the "No code signing"
  section above + the README's "More info → Run anyway" note are
  the documented path.
