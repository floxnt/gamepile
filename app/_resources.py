"""Resource-path resolution that works for both dev source and PyInstaller --onedir bundles.

In a frozen bundle, PyInstaller flattens app/main.py such that
`Path(__file__).parent` no longer points at the app package directory.
The package's data files (templates/, static/) live under sys._MEIPASS/app/.
This module exposes `app_resource_dir()` returning the correct base
in both environments.
"""

import sys
from pathlib import Path


def app_resource_dir() -> Path:
    """Return the directory containing app/templates and app/static.

    Dev mode: the app package dir (where this file lives).
    Frozen mode: sys._MEIPASS/app — PyInstaller's spec datas=[('app/templates', ...)]
    preserves the layout."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "app"
    return Path(__file__).parent
