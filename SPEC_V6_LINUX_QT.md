# v6 Linux Qt-backed UI runtime

## Status

**Shipped as v0.9.0.** Qt-backed Linux AppImage is the canonical
Linux distribution. Replaces the v0.8.x GTK + WebKit2GTK path
which hit an architectural ceiling at v0.8.6 (see "Empirical
motivation" below).

Decision informed by two 60-minute feasibility spikes (2026-05-23):
- **Spike A (bwrap-based GTK bind mount):** Cleared the v0.8.6
  ceiling via bwrap `--overlay-src` + `--tmp-overlay` to bind-mount
  bundled WebKit child-process executables onto the compiled-in
  PKGLIBEXECDIR path. Required 3 iterations (permission denied →
  arg cap → overlayfs). Revealed underlying pywebview gtk.py:428
  opacity bug (blank window). Adds bwrap runtime dependency.
- **Spike B (Qt pivot):** Library rendered 648 games on first
  attempt with zero workarounds. Single-phase build pipeline, no
  external runtime deps, no child-process spawn surgery. PySide6's
  self-contained Qt model delivers the "works on any Linux desktop"
  property that the GTK path fought against.

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

## Architecture decision (locked at v0.9.0)

- **UI runtime:** PySide6 6.11.1 (Qt 6.x) via qtpy shim.
  pywebview==6.2.1 pinned to prevent silent version drift.
- **Backend module:** `webview.platforms.qt` is the active Linux
  backend. `webview.platforms.gtk` + `gi.*` are in
  `platform_excludes` in `gamepile.spec`. PyGObject removed from
  dependencies entirely — Qt-everywhere for both dev and production.
- **Distribution wrapper:** AppImage (single-phase linuxdeploy,
  no plugins). Custom AppRun sets `PYWEBVIEW_GUI=qt` (suppresses
  GTK auto-detect fallback) and `QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu`
  (suppresses EGL/GBM warnings on Wayland).
- **Windows:** unchanged. Inno Setup installer + edgechromium
  backend via pythonnet/.NET 8 — fully independent, no regression
  risk from the Linux Qt swap.

## What shipped (v0.9.0)

1. **Dependencies:** `PySide6>=6.5` + `qtpy` added (Linux-only
   markers). `PyGObject` removed entirely. `pywebview==6.2.1`
   pinned. `uv lock` resolves PySide6 6.11.1 + shiboken6 +
   pyside6-essentials + pyside6-addons.

2. **gamepile.spec:** Linux `platform_hiddenimports` swapped to Qt
   modules (PySide6.QtCore/Gui/Widgets/WebEngineWidgets/WebEngineCore/
   WebChannel/Network). GTK + gi excluded. Conservative PySide6
   stripping: Qt3D, QtBluetooth, QtMultimedia, QtNfc, QtSensors,
   QtSerialPort/Bus, QtRemoteObjects, QtTextToSpeech, QtCharts,
   QtDataVisualization, QtQuick3D excluded. Runtime hook:
   `pyi_rth_qt_backend.py` (sets `PYWEBVIEW_GUI=qt`), replaces
   `pyi_rth_gi_typelib_path.py` (deleted).

3. **release.yml:** GTK/WebKit apt-get removed, EGL/GL/OpenGL
   added. linuxdeploy-plugin-gtk download removed. Build AppImage
   step simplified to single-phase linuxdeploy with `--custom-apprun`
   (no plugins, no `--library`, no apprun hooks, no 3-phase chain).
   Plausibility floor raised from 40 MB to 100 MB.

4. **Custom AppRun:** `installer/linux/AppRun` sets `PYWEBVIEW_GUI=qt`
   (suppresses GTK fallback tracebacks) and
   `QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu` (suppresses EGL/GBM
   warnings).

5. **Removed:** `apprun-libpath-hook.sh` (GTK LD_LIBRARY_PATH),
   `pyi_rth_gi_typelib_path.py` (GI typelib path override).

## Artifact size (measured)

- **Spike B (unstripped):** 234 MB compressed AppImage.
- **v0.9.0 (conservative stripping):** target ~180 MB (Qt3D,
  QtBluetooth, QtMultimedia, etc. excluded).
- **v0.8.6 GTK baseline:** 116 MB for comparison.
- **Size is acceptable** for friend-distribution scale. Further
  aggressive stripping deferred until size becomes a real complaint.

## Resolved open questions

1. **Distribution wrapper:** AppImage. Single-file convenience wins
   over `.tar.gz` extract step. linuxdeploy (without plugins) is
   trivial.
2. **macOS coverage:** deferred. Not blocking.
3. **Theme integration:** GamePile's dark theme is CSS-based
   (served via HTMX/Jinja2). QtWebEngine renders it identically to
   WebKit2GTK — no theme adapter needed.
4. **Bundle-size optimization:** conservative stripping shipped in
   v0.9.0. Aggressive optimization deferred.
5. **Sandboxing:** QtWebEngine's Chromium sandbox uses
   `--no-zygote-sandbox` when user namespaces aren't available.
   CachyOS (unprivileged userns enabled) works out of the box.
   Hardened distros may need `sysctl
   kernel.unprivileged_userns_clone=1` — acceptable for friend-
   distribution.

## Historical: GTK saga value

The v0.8.x GTK AppImage saga (v0.8.2 → v0.8.6) produced real value
even though it ended at an architectural ceiling:

- The PyInstaller bootloader / linuxdeploy-plugin-gtk impedance
  audit is thoroughly documented (SPEC_V5_DISTRIBUTION.md). If
  the project ever needs a GTK-backed Linux bundle for a different
  reason, the four resolvable layers each have a working fix.
- The discipline of empirical-then-fix-then-reassess-scope is
  codified in PROJECT_STATE.md Key learnings. The same discipline
  drove the Qt pivot decision via spikes.
- Windows distribution is unaffected — Inno Setup installer
  continues shipping as the canonical Windows artifact.
