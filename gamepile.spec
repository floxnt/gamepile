# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for GamePile (cross-platform).
# Build with: pyinstaller gamepile.spec
#
# Output: dist/gamepile/ — a --onedir bundle containing the executable
# plus all dependencies. Distributed as a ZIP (Windows) or tar.gz (Linux)
# from the GitHub Releases page.
#
# --onefile is intentionally NOT used. Reasons:
#   - Windows Defender flags --onefile bundles as suspicious far more
#     often than --onedir, leading to SmartScreen warnings.
#   - --onefile startup is slower (extracts to a temp dir on every launch).
#
# Linux runtime deps (NOT bundled — must be installed on the target machine):
#   - webkit2gtk-4.1   WebKit rendering engine for pywebview's GTK backend
#   - python-gobject   GObject introspection bindings
#   - gtk3             GTK3 libraries
# These are documented in the bundled README.
#
# Windows runtime deps (NOT bundled — but auto-handled at runtime):
#   - Microsoft Edge WebView2 Runtime (preinstalled on Windows 11 standard;
#     missing on Windows 11 LTSC). app/main.py detects missing runtime at
#     launch and opens the installer page in the user's browser before
#     exiting.

import sys

from PyInstaller.utils.hooks import collect_all

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform == "linux"
IS_MACOS = sys.platform == "darwin"

# Collect pywebview wholesale — we lean on PyInstaller's auto-discovery
# for the per-platform backend modules and their support data.
webview_datas, webview_binaries, webview_hiddenimports = collect_all("webview")

# Keyring backends are lazy-imported by the keyring library at runtime.
# PyInstaller's static analysis misses them; declare each platform's
# backends explicitly so the bundle includes them.
keyring_hiddenimports = [
    # Linux
    "keyring.backends.SecretService",
    "keyring.backends.kwallet",
    # Windows
    "keyring.backends.Windows",
    # macOS
    "keyring.backends.macOS",
    # Always-present fallback
    "keyring.backends.fail",
    "keyring.backends.chainer",
]

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
        "webview.platforms.gtk",
        "gi",
        "gi.repository.WebKit2",
        "gi.repository.Gtk",
        "gi.repository.GLib",
    ]
    platform_excludes = [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "webview.platforms.cocoa",
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
    binaries=webview_binaries,
    datas=[
        ("app/templates", "app/templates"),
        ("app/static", "app/static"),
        *webview_datas,
    ],
    hiddenimports=[
        *platform_hiddenimports,
        *keyring_hiddenimports,
        *webview_hiddenimports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
