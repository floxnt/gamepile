# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for GamePile.
# Build with: pyinstaller gamepile.spec
#
# Linux runtime deps (install via pacman before distributing):
#   webkit2gtk-4.1   — WebKit rendering engine used by pywebview's GTK backend
#   python-gobject   — GObject introspection bindings (PyGObject)
#   gtk3             — GTK3 libraries
#
# These are system libraries that PyInstaller cannot bundle portably.
# The binary will fail to open a window on machines missing webkit2gtk-4.1.

from PyInstaller.utils.hooks import collect_all

# Collect pywebview and its platform data/binaries wholesale
webview_datas, webview_binaries, webview_hiddenimports = collect_all("webview")

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
        # GTK backend
        "webview.platforms.gtk",
        "gi",
        "gi.repository.WebKit2",
        "gi.repository.Gtk",
        "gi.repository.GLib",
        *webview_hiddenimports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Windows/macOS backends we don't need on Linux
        "webview.platforms.edgechromium",
        "webview.platforms.cocoa",
        "webview.platforms.winforms",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="gamepile",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,       # no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
