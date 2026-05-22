
# GamePile: inject LD_LIBRARY_PATH so dlopen calls from inside bundled
# libraries (libgirepository -> libgtk-3.so.0 via PyGObject's
# gi.require_version) can find the bundled GTK stack at $APPDIR/usr/lib/.
# This file is appended to the linuxdeploy-plugin-gtk apprun-hook by
# the release workflow's "Build AppImage" step — see that step's
# comment block for the full structural rationale (why the plugin
# doesn't set this itself, why we append to its hook rather than
# modifying AppRun, why this is needed for PyInstaller AppImages but
# not for natively-linked GTK apps).
export LD_LIBRARY_PATH="$APPDIR/usr/lib:${LD_LIBRARY_PATH:-}"
