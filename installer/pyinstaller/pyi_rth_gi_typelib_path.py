# GamePile PyInstaller runtime hook — GI_TYPELIB_PATH override (Linux).
#
# WHY THIS HOOK EXISTS:
# PyInstaller's auto-included `pyi_rth_gi.py` runs at bootloader startup
# and unconditionally overwrites GI_TYPELIB_PATH to
# `os.path.join(sys._MEIPASS, "gi_typelibs")`. PyInstaller's `gi` hook is
# supposed to populate that directory with typelibs collected from the
# build host, but in our build pipeline that directory ends up empty
# because linuxdeploy-plugin-gtk bundles the typelibs at
# `$APPDIR/usr/lib/girepository-1.0/` (the AppImage convention).
# libgirepository then finds nothing at PyInstaller's path and falls
# through to its compiled-in default
# `/usr/lib/x86_64-linux-gnu/girepository-1.0/`, which exists on
# Debian/Ubuntu/WSL (so the bug is masked there) but does NOT exist on
# CachyOS / Arch / Fedora / non-Debian distros (so the AppImage fails
# with `ValueError: Namespace Gtk not available`). Diagnosed at v0.8.5
# via strace on CachyOS + local PyInstaller rthook chain analysis.
#
# HOW THIS HOOK WORKS:
# Installs a sys.meta_path finder that fires at `import gi`. PyInstaller's
# runtime hooks all run synchronously at bootloader startup, BEFORE user
# imports resolve; our finder fires LATER, when user code does
# `import gi`, so we win the last-writer race against pyi_rth_gi.py.
# libgirepository reads GI_TYPELIB_PATH lazily at typelib-search time
# (g_irepository_require -> gi.require_version()), which is well after
# our finder has set the correct value.
#
# DO NOT remove this hook even if pyi_rth_gi.py looks like the canonical
# place for this — removing this hook re-introduces the CachyOS failure.
# See `docs/PROJECT_STATE.md` v0.8.5 for the full audit history.
import os
import sys
import importlib.abc


def _bundled_typelib_dir():
    appdir = os.environ.get("APPDIR")
    if appdir:
        return os.path.join(appdir, "usr", "lib", "girepository-1.0")
    # Fallback for non-AppImage layouts: derive from _MEIPASS.
    meipass = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
    if os.path.basename(meipass) == "_internal":
        usr_dir = os.path.dirname(os.path.dirname(meipass))
    else:
        usr_dir = os.path.dirname(meipass)
    return os.path.normpath(os.path.join(usr_dir, "lib", "girepository-1.0"))


class _GiTypelibPathFixer(importlib.abc.MetaPathFinder):
    _applied = False

    def find_spec(self, name, path=None, target=None):
        if not _GiTypelibPathFixer._applied and (name == "gi" or name.startswith("gi.")):
            _GiTypelibPathFixer._applied = True
            os.environ["GI_TYPELIB_PATH"] = _bundled_typelib_dir()
        return None  # let normal import resolution proceed


sys.meta_path.insert(0, _GiTypelibPathFixer())
