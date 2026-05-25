# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for GamePile (cross-platform).
# Build with: pyinstaller gamepile.spec
#
# Output: dist/gamepile/ — a --onedir bundle containing the executable
# plus all dependencies. The release workflow then wraps this output
# per platform: an Inno Setup installer .exe on Windows (v0.8.0+) or a
# self-contained AppImage on Linux (v0.8.2+).
# See SPEC_V5_DISTRIBUTION.md for the full distribution-layer architecture.
#
# --onefile is intentionally NOT used. Reasons:
#   - Windows Defender flags --onefile bundles as suspicious far more
#     often than --onedir, leading to SmartScreen warnings.
#   - --onefile startup is slower (extracts to a temp dir on every launch).
#
# Linux runtime model (v0.9.0+, Qt-backed):
#   PySide6 ships self-contained Qt + QtWebEngine binaries in the wheel.
#   PyInstaller's PySide6 hook bundles them into _internal/. No system
#   WebKit, no system GTK, no compiled-in PKGLIBEXECDIR child-process
#   paths, no bwrap bind-mount surgery. linuxdeploy packages the
#   PyInstaller output into an AppImage in a single phase (no plugins).
#   See SPEC_V6_LINUX_QT.md for the architectural decision record
#   (spike A bwrap vs spike B Qt comparison).
#
# Historical: v0.8.2–v0.8.6 used GTK 3 + WebKit2GTK via PyGObject +
#   linuxdeploy-plugin-gtk. That path hit an architectural ceiling at
#   v0.8.6 (WebKit child-process executables required bwrap bind-mount
#   overlay to resolve compiled-in absolute paths on non-Debian hosts).
#   See SPEC_V5_DISTRIBUTION.md for the full GTK saga record.
#
# Windows runtime deps (NOT bundled — but auto-handled at runtime):
#   - Microsoft Edge WebView2 Runtime (preinstalled on Windows 11 standard;
#     missing on Windows 11 LTSC). app/main.py detects missing runtime at
#     launch and opens the installer page in the user's browser before
#     exiting.

import os
import sys

from PyInstaller.utils.hooks import collect_all

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform == "linux"
IS_MACOS = sys.platform == "darwin"

# Collect pywebview wholesale — we lean on PyInstaller's auto-discovery
# for the per-platform backend modules and their support data.
webview_datas, webview_binaries, webview_hiddenimports = collect_all("webview")

# Filter out pywebview's bundled .NETFramework 4.6.2 WebView2 binding
# DLLs (Microsoft.Web.WebView2.{Core,WinForms}.dll under webview/lib/).
# Under the bundled .NET 8 coreclr runtime, the net462 WinForms.dll's
# ContextMenu type reference (removed in .NET Core 3.0+) causes
# TypeLoadException at module-load — the v0.5.6 crash class.
#
# IMPORTANT: this filter is NOT load-bearing on its own. pywebview ships
# a PyInstaller hook (webview/__pyinstaller/hook-webview.py) that's
# re-invoked independently during PyInstaller's Analysis() phase, which
# re-adds the same net462 DLLs after this filter ran. v0.5.7 shipped
# broken for exactly that reason. The LOAD-BEARING override happens at
# the bundle filesystem layer via the release workflow's "Apply WebView2
# binding override to built bundle" step, which Copy-Item -Force's the
# netcoreapp3.0 DLLs over whatever PyInstaller placed at
# _internal/webview/lib/ and SHA512-verifies the post-copy bytes.
#
# This spec-level filter stays for two reasons: (a) it removes the
# DLLs from collect_all's tracked collection so the spec is correct
# at its own layer; (b) belt-and-braces — if a future PyInstaller
# release changes hook-rediscovery behavior, this layer may again
# be sufficient. The sanity-raise below catches "filter matched
# nothing" as a layout-change warning.
#
# Only filter on Windows. On Linux pywebview's Qt backend doesn't use
# any of this; the binding files are absent from the install and the
# filter would be a no-op anyway, but the explicit IS_WINDOWS guard
# documents the intent.
if IS_WINDOWS:
    _BINDING_NAMES = (
        "Microsoft.Web.WebView2.Core.dll",
        "Microsoft.Web.WebView2.WinForms.dll",
    )

    def _is_pywebview_net462_binding(entry):
        # PyInstaller datas tuples are (src_path, dest_dir). The DLL
        # filename lives at the tail of src_path. Match by basename so
        # we don't depend on the exact venv-relative source layout.
        src = entry[0] if isinstance(entry, tuple) else entry
        return os.path.basename(src) in _BINDING_NAMES

    _filtered_count = sum(1 for d in webview_datas if _is_pywebview_net462_binding(d))
    webview_datas = [d for d in webview_datas if not _is_pywebview_net462_binding(d)]
    webview_binaries = [b for b in webview_binaries if not _is_pywebview_net462_binding(b)]
    if _filtered_count == 0:
        raise RuntimeError(
            "Expected to filter pywebview's bundled WebView2 binding "
            "DLLs from collect_all('webview') output, found none. "
            "Either pywebview's layout changed or collect_all stopped "
            "including them — investigate before building, otherwise "
            "the override below will quietly fail and v0.5.6's "
            "TypeLoadException crash class returns."
        )

# pythonnet + clr_loader: Windows-only. pywebview's edgechromium and
# winforms backends both `import clr` at module load, which fires
# pythonnet → clr_loader → netfx → Python.Runtime.Loader.Initialize.
# PyInstaller's auto-walk finds Python.Runtime.dll but misses its
# non-binary companions (Python.Runtime.dll.config binding redirects,
# Python.Runtime.runtimeconfig.json, .deps.json) and may miss
# clr_loader/ffi/dlls/clr.dll (the native CLR-host shim). Both v0.5.3
# and v0.5.4 shipped Windows bundles that crashed at end-user launch
# with "Failed to resolve Python.Runtime.Loader.Initialize" despite the
# DLL being physically present — the netfx loader could load the
# assembly but couldn't resolve its entry point because the .config
# binding redirects to netstandard 2.0 were absent. collect_all closes
# that gap. Not needed on Linux (Qt backend does not import clr) or
# macOS (unsupported in v5).
if IS_WINDOWS:
    pythonnet_datas, pythonnet_binaries, pythonnet_hiddenimports = collect_all("pythonnet")
    clr_loader_datas, clr_loader_binaries, clr_loader_hiddenimports = collect_all("clr_loader")
    # Bundled self-contained .NET 8 runtime — see SPEC_V5_DISTRIBUTION.md
    # "Self-contained .NET runtime (Windows)". The release workflow
    # downloads, SHA-verifies, and extracts BOTH Microsoft.NETCore.App
    # and Microsoft.WindowsDesktop.App runtimes into dotnet/runtime/
    # before invoking pyinstaller. The WindowsDesktop SKU is required:
    # pywebview's edgechromium backend uses System.Windows.Forms to host
    # the WebView2 control, and WinForms is not in the base NETCore.App.
    # Pinning the runtime version inside the bundle removes the
    # host-environment dependency that broke v0.5.3..v0.5.5 on clean
    # Windows 11 (.NET Framework facade-assembly resolution varies
    # between developer images and consumer machines).
    dotnet_datas = [
        ("dotnet/runtime", "dotnet"),
        ("dotnet/Python.Runtime.runtimeconfig.json", "dotnet"),
    ]
    # WebView2 netcoreapp3.0 binding override DLLs — staged into
    # webview2_override/ by the release workflow's "Download and verify
    # WebView2 netcoreapp3.0 binding override" step. They land at
    # _internal/webview/lib/ in the final bundle, the same location
    # pywebview's loader uses (interop_dll_path in webview/util.py
    # resolves to webview/lib/), with the net462 originals filtered out
    # above so there's no ambiguity about which DLL gets loaded.
    webview2_override_datas = [
        ("webview2_override/Microsoft.Web.WebView2.Core.dll", "webview/lib"),
        ("webview2_override/Microsoft.Web.WebView2.WinForms.dll", "webview/lib"),
    ]
else:
    pythonnet_datas, pythonnet_binaries, pythonnet_hiddenimports = [], [], []
    clr_loader_datas, clr_loader_binaries, clr_loader_hiddenimports = [], [], []
    dotnet_datas = []
    webview2_override_datas = []

# Keyring backends are lazy-imported by the keyring library at runtime.
# PyInstaller's static analysis misses them; declare the active
# platform's backends explicitly. Linux Secret Service depends on
# `secretstorage` which isn't installed on Windows wheels, so we
# can't blanket-import all backends — they get scoped per platform.
keyring_hiddenimports = [
    "keyring.backends.fail",
    "keyring.backends.chainer",
]
if IS_WINDOWS:
    keyring_hiddenimports += ["keyring.backends.Windows"]
elif IS_LINUX:
    keyring_hiddenimports += [
        "keyring.backends.SecretService",
        "keyring.backends.kwallet",
        "secretstorage",
    ]
elif IS_MACOS:
    keyring_hiddenimports += ["keyring.backends.macOS"]

# Per-platform pywebview backend choices. We hard-include the active
# backend and exclude the others to keep bundle size minimal.
if IS_WINDOWS:
    platform_hiddenimports = [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
    ]
    platform_excludes = [
        "webview.platforms.gtk",
        "webview.platforms.cocoa",
        "gi",
        "gi.repository.WebKit2",
        "gi.repository.Gtk",
        "gi.repository.GLib",
    ]
elif IS_LINUX:
    platform_hiddenimports = [
        "webview.platforms.qt",
        "qtpy",
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebChannel",
        "PySide6.QtNetwork",
    ]
    platform_excludes = [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "webview.platforms.cocoa",
        "webview.platforms.gtk",
        "gi",
        "gi.repository.WebKit2",
        "gi.repository.Gtk",
        "gi.repository.GLib",
        # Conservative PySide6 module stripping: exclude obviously-
        # irrelevant heavy modules that GamePile never imports. Saves
        # ~50 MB from the AppImage (234 MB → ~180 MB target). Leave
        # ambiguous smaller modules in the bundle — aggressive stripping
        # risks "did I miss a transitive dep" failures caught only at
        # the hardware gate.
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DExtras",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DRender",
        "PySide6.QtBluetooth",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtNfc",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtSerialBus",
        "PySide6.QtRemoteObjects",
        "PySide6.QtTextToSpeech",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtQuick3D",
    ]
else:  # macOS (not officially supported in v5; left functional in case someone runs the spec there)
    platform_hiddenimports = [
        "webview.platforms.cocoa",
    ]
    platform_excludes = [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "webview.platforms.gtk",
        "gi",
    ]


a = Analysis(
    ["app/main.py"],
    pathex=[],
    binaries=[
        *webview_binaries,
        *pythonnet_binaries,
        *clr_loader_binaries,
    ],
    datas=[
        ("app/templates", "app/templates"),
        ("app/static", "app/static"),
        ("assets/icons/gamepile-icon-256.png", "assets/icons"),
        *webview_datas,
        *pythonnet_datas,
        *clr_loader_datas,
        *dotnet_datas,
        *webview2_override_datas,
    ],
    hiddenimports=[
        *platform_hiddenimports,
        *keyring_hiddenimports,
        *webview_hiddenimports,
        *pythonnet_hiddenimports,
        *clr_loader_hiddenimports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["installer/pyinstaller/pyi_rth_qt_backend.py"] if IS_LINUX else [],
    excludes=[
        *platform_excludes,
        # Test files and ad-hoc scripts never run at runtime.
        "tests",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="gamepile",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=False suppresses the terminal window on Windows (pywebview
    # is the UI). On Linux this flag is a no-op for the executable but
    # still affects nothing harmful.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# --onedir collector. Outputs dist/gamepile/gamepile{,.exe} alongside
# the runtime dependencies the EXE loads.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="gamepile",
)
