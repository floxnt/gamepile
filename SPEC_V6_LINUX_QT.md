# v6 Linux Qt-backed UI runtime (deferred architectural track)

## Status

Sketched architectural spec — not yet under active implementation.
Captured here so the Qt-backed Linux track has a stable home for
its rationale, expected work, and empirical motivation before
implementation begins. The implementation round will refine this
spec; right now it is the architectural anchor that explains why
the GTK AppImage saga stopped at v0.8.6 with documented-experimental
status rather than continuing to v0.8.7.

## Purpose

Replace the linuxdeploy + bundled-WebKitGTK Linux distribution path
with a pywebview Qt-backed bundle that uses PySide6's pre-bundled Qt
runtime. The goal is a Linux artifact that works portably across
Debian-multiarch and usrmerge-flat-layout distros without
bind-mount surgery, binary patching, or compile-time WebKit source
builds.

## Empirical motivation (from the v0.8.x GTK AppImage saga)

The v0.8.2 → v0.8.6 saga produced a working pipeline for bundling
the GTK 3 runtime + WebKit2GTK libraries + GObject-introspection
typelibs into an AppImage. Four rounds of empirical-then-fix
discipline closed four inference gaps between the PyInstaller
bootloader and linuxdeploy-plugin-gtk. The fifth layer is the
architectural ceiling and is documented in detail in
`docs/PROJECT_STATE.md` v0.8.6 ("Manual gate outcome — the saga's
architectural ceiling") and `SPEC_V5_DISTRIBUTION.md`
("Architectural ceiling: WebKit child-process executable lookup").
Short version:

1. Ubuntu's release-build `libwebkit2gtk-4.1.so.0` has the absolute
   path `/usr/lib/x86_64-linux-gnu/webkit2gtk-4.1` compiled into the
   binary at Ubuntu's package compile time. WebKit forks
   `WebKitNetworkProcess` and `WebKitWebProcess` at runtime via
   that exact compiled-in absolute path.
2. WebKit's documented escape-hatch env var `WEBKIT_EXEC_PATH` is
   gated by `#if ENABLE(DEVELOPER_MODE)` in
   `Source/WebKit/Shared/glib/ProcessExecutablePathGLib.cpp` and
   `Source/WebKit/UIProcess/Launcher/glib/BubblewrapLauncher.cpp`.
   Ubuntu does not ship `libwebkit2gtk-4.1-0` with
   `ENABLE_DEVELOPER_MODE=ON`. The env var is a no-op against the
   Ubuntu-built binary; the string literal does not even appear in
   the .so (`strings | grep -c WEBKIT_EXEC_PATH` → 0).
3. On Arch / CachyOS / Manjaro / Fedora / openSUSE — distros using
   the usrmerge flat `/usr/lib/` layout — the
   `/usr/lib/x86_64-linux-gnu` directory does not exist. The
   compiled-in path is structurally unmappable on these distros
   regardless of whether `webkit2gtk-4.1` is installed via the
   system package manager (the helpers land at `/usr/lib/
   webkit2gtk-4.1/`, not the Debian multiarch path).

Resolving these for GTK would require runtime bind-mount surgery
(bubblewrap or unprivileged user namespaces), binary patching the
.so, or compiling WebKitGTK from source with developer-mode
enabled. All three cost disproportionately more than the previous
saga layers. The discipline call: stop, document the ceiling,
defer to a different architecture.

## Why Qt + PySide6 doesn't have this property

Qt's distribution model is "bundle the runtime with the app."
PySide6 wheels include Qt binaries by design. There is no
compiled-in absolute path baked by an external packager — the
runtime ships with the wheel and is resolved at runtime via
standard library-loading mechanisms PyInstaller already handles
cleanly (Qt's plugin discovery uses `QT_PLUGIN_PATH` /
`QLibraryInfo` paths that respect the bundled location).

pywebview supports a Qt backend (`webview.platforms.qt`). The
backend wraps `QWebEngineView`, which is Chromium-based. Multi-
process architecture exists in QtWebEngine too, but its child
processes (`QtWebEngineProcess`) are shipped inside the PySide6
wheel and discovered via Qt's own resource model — not via a
compiled-in absolute path in a system library.

## Architecture decision

- **UI runtime:** PySide6 (Qt 6.x) via `pywebview[qt]`.
- **Backend module:** `webview.platforms.qt` becomes the Linux
  active backend; `webview.platforms.gtk` moves to the
  `platform_excludes` list in `gamepile.spec` (symmetric with how
  v0.8.6 added Qt to excludes when GTK was active).
- **Distribution wrapper:** TBD between AppImage (continuing the
  linuxdeploy pipeline, now without WebKit pain) and a plain
  `.tar.gz` that's actually portable now that the runtime is
  self-contained. Decision deferred to implementation round; the
  empirical evidence will resolve it.
- **Windows:** unchanged. The Inno Setup installer + WebView2
  binding on Windows is settled; this track is Linux-only.

## Expected work

Sketched at outline level; refined when implementation starts.

1. **Dependency changes (`pyproject.toml`).**
   - Add `PySide6>=6.6; sys_platform == 'linux'` (PEP 508 marker,
     symmetric with the existing `PyGObject; sys_platform == 'linux'`
     marker).
   - Add `qtpy>=2.4; sys_platform == 'linux'` if `pywebview[qt]`
     doesn't already pull it.
   - Add or update `pywebview` to ensure the Qt backend is
     functional (verify minimum version supporting QWebEngineView
     in the Qt 6.x line).
   - Drop `PyGObject` if Qt fully replaces it (verify no other code
     path depends on PyGObject for non-pywebview reasons).

2. **PyInstaller spec changes (`gamepile.spec`).**
   - Linux branch `platform_hiddenimports` swaps:
     - Remove: `webview.platforms.gtk`, `gi`,
       `gi.repository.WebKit2`, `gi.repository.Gtk`,
       `gi.repository.GLib`.
     - Add: `webview.platforms.qt`, `PySide6.QtWebEngineCore`,
       `PySide6.QtWebEngineWidgets`, `PySide6.QtCore`,
       `PySide6.QtGui`, `PySide6.QtWidgets` (verify the exact set
       PyInstaller's auto-discovery misses; many will be picked up
       automatically).
   - Linux branch `platform_excludes` swaps:
     - Remove: `webview.platforms.qt`.
     - Add: `webview.platforms.gtk`, `gi`,
       `gi.repository.{WebKit2,Gtk,GLib}`.
   - Linux branch `runtime_hooks`: remove
     `installer/pyinstaller/pyi_rth_gi_typelib_path.py` (no longer
     needed without GTK).
   - PyInstaller's PySide6 hook does most of the work; verify
     QtWebEngineProcess + QtWebEngine resource paths land
     correctly in the bundle via `collect_all('PySide6')` or
     equivalent.

3. **Build pipeline changes (`.github/workflows/release.yml`).**
   - Linux step's `apt install` block: drop GTK/WebKit dev
     packages (`libwebkit2gtk-4.1-0`, `gir1.2-webkit2-4.1`,
     `python3-gi`, etc.); add anything PySide6 needs at build time
     (probably nothing — the wheels are self-contained, but verify
     QtWebEngine doesn't need a system OpenGL/X11 lib for the
     build to succeed).
   - Drop the `Download and verify linuxdeploy + linuxdeploy-plugin-gtk`
     step entirely if AppImage isn't used post-Qt-swap; OR keep it
     if AppImage IS used post-Qt-swap (linuxdeploy without
     plugin-gtk is much simpler — no GTK bundling drama, just
     wraps the PyInstaller `--onedir` output).
   - Drop the `Build AppImage` step's three-phase invocation /
     apprun-hook append / `--library` complexity (those were all
     GTK/WebKit-specific bridges). If keeping AppImage, the new
     step is essentially a one-liner: stage the PyInstaller bundle
     into AppDir, run `linuxdeploy --output appimage`.
   - Update the bundled-runtime SHA-pinning entries in this SPEC
     (the .NET 8 / WebView2 / vendored-pywebview entries stay;
     linuxdeploy + plugin-gtk go away or get downgraded if AppImage
     stays; PySide6 wheel pinning lands in the `pyproject.toml`
     constraints, not in a SHA-pin step).

4. **`app/main.py` changes.**
   - The frozen-Windows `pythonnet.set_runtime()` block stays
     (Windows is unchanged).
   - The GTK-specific signal-handling or theme-detection code
     (none currently in `app/main.py` that I'm aware of, but
     verify) gets reviewed for Qt-equivalent behavior.
   - WebView2 runtime detection on Windows stays.

5. **Tests.**
   - `tests/test_paths.py` is unaffected (platformdirs paths
     don't change).
   - Add a Linux smoke test that imports `webview.platforms.qt`
     under the bundled runtime — the equivalent of
     `--check-windows-runtime` for the Qt path.
   - The Linux build's `tests/run_tests.py` invocation in CI may
     need PySide6 importable; verify the test runner doesn't have
     transitive GTK requirements that the post-swap dependency set
     would break.

6. **Documentation.**
   - `README.bundled.md` Linux section rewrites: drop the
     experimental-status caveats; document the new (likely)
     uniform Linux experience.
   - `docs/PROJECT_STATE.md` adds a v0.9.x section documenting the
     Qt-backed Linux track as shipped (when it lands).
   - `SPEC_V5_DISTRIBUTION.md` AppImage section: the
     `Architectural ceiling` subsection gets a closing note
     pointing at the v0.9.x implementation; the experimental
     status on v0.8.6 gets formally retired in favor of v0.9.x.
   - This SPEC gets folded into `SPEC_V5_DISTRIBUTION.md` (or
     moved alongside it as a sibling distribution-architecture
     doc) once the work lands. The naming convention is "in-flight
     architecture docs at the repo root, post-implementation docs
     consolidated into the dist SPEC."

## Expected artifact size impact

- **Add (PySide6 + Qt 6 + QtWebEngine):** ~150 MB compressed AppImage
  estimated. PySide6 wheels are ~100 MB unpacked on Linux; Qt 6
  with QtWebEngine adds another ~200 MB unpacked. Compressed
  AppImage growth: rough estimate 150 MB delta over the v0.8.6
  Linux AppImage size of 121 MB.
- **Drop (GTK + WebKit):** GTK + WebKit-related libraries currently
  bundled via linuxdeploy-plugin-gtk + `--library` are ~50 MB
  compressed; those leave with the GTK backend.
- **Net:** ~100 MB compressed artifact growth, landing at roughly
  220-250 MB for the Linux AppImage. Acceptable for
  friend-distribution scale; would be a real concern for public
  distribution where it would become an optimization target.

Verify the actual numbers empirically during the implementation
round — the above is estimation, not measurement. The plausibility
floor in the workflow's AppImage build step gets recalibrated to
the new range.

## Open questions for implementation time

1. **Distribution wrapper.** AppImage or plain `.tar.gz`? PySide6's
   self-contained bundle should be portable enough for a `.tar.gz`
   (the v0.8.1 .tar.gz crash was specifically about missing system
   GTK packages — that root cause is gone with Qt). AppImage's
   selling point is the single-file convenience; `.tar.gz` is
   simpler at the cost of "user has to extract first."
2. **macOS coverage.** Adding `macos-latest` to the CI matrix is
   a one-line change. The Qt-backed runtime might extend to macOS
   coverage cheaply (no `.NET-Framework-class indirection` since
   we're not relying on platform-native WebView). Decision deferred
   until someone asks; not a blocker for the Linux Qt swap.
3. **Theme integration.** GTK 3's theme detection was free via the
   plugin-gtk apprun-hook (probes `org.freedesktop.portal.Desktop`
   for color-scheme). Qt's theme integration on Linux follows
   different conventions; verify GamePile's dark-mode behavior is
   acceptable under Qt or design a small adapter.
4. **Bundle-size optimization.** PySide6 + Qt 6 bundles many Qt
   modules we don't use (Qt3D, QtMultimedia, QtCharts, QtDataVisualization,
   QtQuick, etc.). PyInstaller's auto-discovery may pull all of
   them via `collect_all('PySide6')`. Excluding unused Qt modules
   is the obvious size-optimization round; defer until measurement
   shows it's needed.
5. **Sandboxing.** QtWebEngine sandboxes its child processes via
   Chromium's sandbox infrastructure. On Linux this typically
   requires setuid or user namespaces. Verify whether the friend-
   audience hosts will hit any sandbox-related friction.

## Why this is the right deferral, not abandonment

The v0.8.x GTK AppImage saga produced real value even though it
ended at an architectural ceiling rather than at a validated
canonical release:

- The PyInstaller bootloader / linuxdeploy-plugin-gtk impedance
  audit is now thoroughly documented and the four resolvable
  layers each have a working fix. If the project ever needs to
  ship a GTK-backed Linux bundle for a different reason (different
  WebView library, different distribution channel), the
  foundational work is in place.
- The discipline of empirical-then-fix-then-reassess-scope is now
  codified in the project's `Key learnings` section. The same
  discipline will apply to the Qt-backed track when its own
  surprises surface.
- Windows distribution is unaffected — the Inno Setup installer
  continues to ship as the canonical Windows artifact, and the
  v0.5.x → v0.6.2 Windows saga's load-bearing infrastructure
  (bundled .NET 8 runtime, WebView2 binding override, vendored
  pywebview winforms.py patch) is independent of the Linux swap.
- The Linux dev experience via `uv run gamepile` is unchanged on
  any distro where the developer has GTK / WebKit / PyGObject
  installed — that's the supported path on non-supported AppImage
  distros until the Qt-backed build ships.

The Qt-backed track lands when it lands. There's no urgency:
GamePile's product goal is backlog management on the user's
machine, and the cross-distro Linux story is supporting
infrastructure, not the product itself. Return to the actual
product work (Library refinement, recommender tuning) and pick
this up as a dedicated architectural round.
