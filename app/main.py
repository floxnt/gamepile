import logging
import os
import sys
import threading
import time

# LOAD-BEARING ORDER (Windows-frozen only). pywebview's edgechromium and
# winforms backends `import clr` at module load. `import clr` triggers
# pythonnet's default runtime selection, which on Windows is the netfx
# (.NET Framework) loader. The netfx path's success depends on the host
# machine's .NET Framework facade-assembly resolution behavior — which
# differs in load-bearing ways between developer images (e.g. GitHub
# Actions windows-latest, where it works) and clean consumer Windows 11
# machines (where it does NOT — v0.5.3, v0.5.4, and v0.5.5 all shipped
# Windows bundles that passed CI and crashed at end-user launch with
# RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize).
#
# The fix: bundle a self-contained .NET 8 runtime alongside the app and
# pin pythonnet to clr_loader's coreclr backend pointing at the bundled
# runtimeconfig. This removes the host-.NET dependency entirely — the
# bundled runtime is what gets loaded, regardless of what the host has.
#
# DO NOT move `import webview` (or any module that pulls pywebview)
# above this block. DO NOT defer set_runtime() into a function body.
# Both reorderings silently reintroduce the v0.5.3..v0.5.5 crash.
if sys.platform == "win32" and getattr(sys, "frozen", False):
    from pathlib import Path
    _meipass = Path(sys._MEIPASS)
    _dotnet_root = _meipass / "dotnet"
    _runtimeconfig = _dotnet_root / "Python.Runtime.runtimeconfig.json"
    if _dotnet_root.is_dir() and _runtimeconfig.is_file():
        os.environ["DOTNET_ROOT"] = str(_dotnet_root)
        import clr_loader
        import pythonnet
        pythonnet.set_runtime(clr_loader.get_coreclr(runtime_config=str(_runtimeconfig)))

import httpx
import uvicorn
import webview
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import credentials
from app import database as db
from app._resources import app_resource_dir
from app.routes import backlog, dashboard, feedback, game_detail, library, refresh, settings, setup, shortlist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(title="GamePile", docs_url=None, redoc_url=None)

app.mount(
    "/static",
    StaticFiles(directory=str(app_resource_dir() / "static")),
    name="static",
)

app.include_router(shortlist.router)
app.include_router(library.router)
app.include_router(backlog.router)
app.include_router(dashboard.router)
app.include_router(game_detail.router)
app.include_router(refresh.router)
app.include_router(feedback.router)
app.include_router(setup.router)
app.include_router(settings.router)


# Routes / path prefixes that bypass the first-run redirect. /setup/* is
# obviously needed (the wizard itself); /static/* serves CSS + HTMX;
# /healthz is the server liveness probe used by main.run() before
# launching the webview; /refresh* and /setup/sync-status power the done
# page's progress polling. The middleware lets these through even when
# credentials are missing.
_FIRST_RUN_BYPASS_PREFIXES = (
    "/setup",
    "/static",
    "/healthz",
    "/refresh",
)


@app.middleware("http")
async def first_run_redirect(request: Request, call_next):
    """Redirect every non-bypassed request to /setup/welcome until the
    user has finished the setup wizard. Single check per request via
    has_complete_credentials (which probes keyring once + caches)."""
    path = request.url.path
    for prefix in _FIRST_RUN_BYPASS_PREFIXES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return await call_next(request)
        if path.startswith(prefix) and prefix == "/refresh":
            # /refresh, /refresh/status, /refresh?force=true all permitted.
            return await call_next(request)
    if not credentials.has_complete_credentials():
        return RedirectResponse(url="/setup/welcome", status_code=303)
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


@app.on_event("startup")
async def startup():
    db.init_db()


def _wait_for_server(url: str, timeout: float = 15.0) -> bool:
    """Poll /healthz until it returns 200 or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=1.0)
            if resp.status_code == 200:
                return True
        except httpx.RequestError:
            pass
        time.sleep(0.1)
    return False


_WEBVIEW2_INSTALL_URL = "https://developer.microsoft.com/en-us/microsoft-edge/webview2/"


def _webview2_installed() -> bool:
    """True when WebView2 Runtime is registered on this Windows machine.

    pywebview's Windows backend renders through WebView2; if the runtime
    is absent (Windows 11 LTSC, very old Windows 10 builds) pywebview
    crashes opaquely. We probe three registry locations covering both
    machine-wide installs (system + WOW6432) and per-user installs.
    Returns False on any non-Windows platform or registry error."""
    if sys.platform != "win32":
        return False
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return False
    client_id = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    locations = [
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{client_id}"),
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{client_id}"),
        (winreg.HKEY_CURRENT_USER,
         rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{client_id}"),
    ]
    for root, subkey in locations:
        try:
            with winreg.OpenKey(root, subkey) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                # Some uninstalls blank the value rather than removing the key.
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _check_webview2_runtime() -> None:
    """No-op on non-Windows. On Windows without WebView2, open the install
    page in the user's default browser and exit cleanly — better UX than
    a pywebview backtrace for users on LTSC / fresh Windows 10."""
    if sys.platform != "win32":
        return
    if _webview2_installed():
        return
    import webbrowser
    print(
        "GamePile requires Microsoft Edge WebView2 Runtime on Windows.\n"
        f"Opening installer page: {_WEBVIEW2_INSTALL_URL}\n"
        "After installing, re-launch GamePile.",
        file=sys.stderr,
    )
    try:
        webbrowser.open(_WEBVIEW2_INSTALL_URL)
    except Exception:
        pass
    sys.exit(2)


def _start_server_thread(port: int) -> threading.Thread:
    """Run uvicorn in a daemon thread — pywebview's GTK event loop owns the
    main thread on Linux. Daemon means the server dies with the process.

    Passing the FastAPI `app` object directly (not the import string
    "app.main:app") so this works inside the PyInstaller --onedir bundle,
    where uvicorn's dynamic module resolution fails due to PyInstaller
    flattening the script entry point."""
    server = uvicorn.Server(uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread


def _run_healthz_only() -> None:
    """CI smoke-test mode used by the Windows runner in .github/workflows/
    release.yml. Starts uvicorn, polls /healthz, prints 'ok'/'fail',
    exits 0/1. No GUI subsystem touched — verifies the bundle imports
    and boots without bringing up pywebview."""
    from app.config import PORT
    _start_server_thread(PORT)
    healthz_url = f"http://127.0.0.1:{PORT}/healthz"
    # 30s timeout: windows-latest cold-starts can be slow (Python init +
    # SQLite schema + Jinja compile). Not load-bearing for performance.
    if _wait_for_server(healthz_url, timeout=30.0):
        print("ok")
        sys.exit(0)
    print("fail: healthz timeout after 30s", file=sys.stderr)
    sys.exit(1)


def _dump_bundle_webview_lib_inventory() -> None:
    """Enumerate what's actually in the frozen bundle's webview/lib/
    directory at RUNTIME — file names, sizes, and TFM for the two
    Microsoft.Web.WebView2.*.dll files. Build-time SHA verification
    (v0.5.8's post-copy check) confirms the bytes that landed during
    the workflow; this runtime probe confirms what's there when the
    EXE actually runs. The v0.5.7->v0.5.8 hook-readd saga proved we
    cannot conflate build-time and runtime state."""
    from pathlib import Path
    print("[inventory] frozen=" + str(getattr(sys, "frozen", False)), file=sys.stderr)
    if not getattr(sys, "frozen", False):
        print("[inventory] not frozen — skipping bundle dir enumeration", file=sys.stderr)
        return
    lib_dir = Path(sys._MEIPASS) / "webview" / "lib"
    print(f"[inventory] dir={lib_dir} exists={lib_dir.is_dir()}", file=sys.stderr)
    if not lib_dir.is_dir():
        return
    for entry in sorted(lib_dir.iterdir()):
        if entry.is_file():
            print(f"[inventory]   file {entry.name} size={entry.stat().st_size}", file=sys.stderr)
        else:
            print(f"[inventory]   dir  {entry.name}/", file=sys.stderr)
    # Extract TargetFrameworkAttribute from the two WebView2 DLLs via
    # byte-search for the TFM marker. Doesn't require .NET to be
    # working — useful even if pythonnet bootstrap subsequently fails.
    for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
        path = lib_dir / name
        if not path.is_file():
            print(f"[inventory] {name}: MISSING", file=sys.stderr)
            continue
        data = path.read_bytes()
        tfms = []
        for needle in (b".NETFramework,Version=v", b".NETCoreApp,Version=v", b".NETStandard,Version=v"):
            idx = 0
            while True:
                pos = data.find(needle, idx)
                if pos < 0:
                    break
                end = data.find(b"\x00", pos)
                if end < 0 or end - pos > 80:
                    end = pos + 40
                tfms.append(data[pos:end].decode("ascii", errors="replace"))
                idx = end
        print(f"[inventory] {name} tfm_markers={tfms}", file=sys.stderr)


def _dump_exc(label: str, exc: BaseException) -> None:
    """Surface a swallow-prone exception fully: type, message, full
    traceback, and any wrapped CLR/.NET inner exception that pythonnet
    may have attached. pywebview's `import_winforms` catches
    `except ImportError` at guilib.py:73-76 and converts it to the
    generic 'You must have pythonnet installed' message, hiding the
    real cause. This helper exists to undo that masking."""
    import traceback
    print(f"[exc] === {label} ===", file=sys.stderr)
    print(f"[exc] type: {type(exc).__module__}.{type(exc).__name__}", file=sys.stderr)
    print(f"[exc] str:  {exc}", file=sys.stderr)
    print(f"[exc] repr: {exc!r}", file=sys.stderr)
    print("[exc] traceback:", file=sys.stderr)
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    # Walk __cause__ and __context__ chains
    seen = {id(exc)}
    cur = exc
    while True:
        nxt = cur.__cause__ or cur.__context__
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        print(f"[exc] -> chained ({'__cause__' if cur.__cause__ else '__context__'}): "
              f"{type(nxt).__module__}.{type(nxt).__name__}: {nxt}", file=sys.stderr)
        cur = nxt
    # pythonnet-wrapped .NET exceptions often expose .InnerException /
    # .StackTrace / .Message — read them defensively.
    for attr in ("Message", "InnerException", "StackTrace", "FusionLog", "TypeName"):
        try:
            val = getattr(exc, attr, None)
        except Exception:
            val = "<getattr-raised>"
        if val is not None:
            print(f"[exc] .NET attr {attr}: {val!r}", file=sys.stderr)


def _run_check_windows_runtime() -> None:
    """CI smoke-test mode that exercises the .NET loader chain bypassed
    by --healthz-only.

    v0.5.9 instrumentation pass: the v0.5.8 Windows bundle now fails at
    runtime with pywebview's generic 'You must have pythonnet installed
    in order to use pywebview' message — which is FALSE. pythonnet is
    bundled and was demonstrably working as of v0.5.6's detector. The
    real exception is swallowed at webview/guilib.py:73-76:

        def import_winforms():
            try:
                import webview.platforms.winforms as guilib
                return True
            except ImportError:
                logger.exception('pythonnet cannot be loaded')
                return False

    This pass surfaces the swallowed exception by walking the same
    imports pywebview's Windows path performs, each wrapped to print
    the full real exception (type, message, traceback, .NET inner
    exception details) instead of letting it be caught-and-generalized.

    Exits 1 at the end of this round so the Windows workflow fails
    and does NOT publish a broken artifact. The diagnostic stderr is
    the payload — pulling it from the workflow log is the deliverable.
    Reverts to assert/fail behavior once the real fault is fixed."""
    if sys.platform != "win32":
        print("ok: non-windows skip")
        sys.exit(0)

    # Layer 1: enumerate the frozen bundle's webview/lib/ at runtime.
    # Confirms the netcoreapp3.0 DLL override landed and survived to
    # process-launch time, not just to build time.
    _dump_bundle_webview_lib_inventory()

    # Layer 2: walk the import chain pywebview's import_winforms
    # would have walked, each step wrapped to surface real exceptions.
    # Continue past each failure — we want the full picture even if an
    # early step fails.
    print("[chain] === stage clr ===", file=sys.stderr)
    try:
        import clr  # noqa: F401
        print("[chain] import clr: OK", file=sys.stderr)
    except BaseException as exc:
        _dump_exc("import clr", exc)
        # Without clr, downstream steps will all fail the same way.
        # Still print the runtime info we can gather.
        try:
            import pythonnet
            info = pythonnet.get_runtime_info()
            print(f"[chain] pythonnet.get_runtime_info() after failed import clr: {info!r}",
                  file=sys.stderr)
        except BaseException as e2:
            _dump_exc("pythonnet.get_runtime_info() after failed import clr", e2)
        sys.exit(0)

    print("[chain] === stage pythonnet runtime info ===", file=sys.stderr)
    try:
        import pythonnet
        info = pythonnet.get_runtime_info()
        kind = info.kind if info is not None else "<None>"
        version = info.version if info is not None else "<None>"
        print(f"[chain] runtime kind={kind} version={version}", file=sys.stderr)
    except BaseException as exc:
        _dump_exc("pythonnet.get_runtime_info()", exc)

    print("[chain] === stage AddReference base assemblies ===", file=sys.stderr)
    for ref in ("System.Windows.Forms", "System.Collections", "System.Threading", "System.Reflection"):
        try:
            clr.AddReference(ref)
            print(f"[chain] AddReference({ref}): OK", file=sys.stderr)
        except BaseException as exc:
            _dump_exc(f"clr.AddReference({ref!r})", exc)

    print("[chain] === stage import System namespaces ===", file=sys.stderr)
    try:
        import System.Windows.Forms as WinForms  # type: ignore[import-not-found]  # noqa: F401
        print("[chain] import System.Windows.Forms: OK", file=sys.stderr)
    except BaseException as exc:
        _dump_exc("import System.Windows.Forms", exc)

    print("[chain] === stage AddReference WebView2 binding DLLs ===", file=sys.stderr)
    try:
        from webview.util import interop_dll_path
        for dll in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
            try:
                path = interop_dll_path(dll)
                print(f"[chain] interop_dll_path({dll!r}) -> {path}", file=sys.stderr)
                clr.AddReference(path)
                print(f"[chain] AddReference({path}): OK", file=sys.stderr)
            except BaseException as exc:
                _dump_exc(f"clr.AddReference(interop_dll_path({dll!r}))", exc)
    except BaseException as exc:
        _dump_exc("from webview.util import interop_dll_path", exc)

    print("[chain] === stage import Microsoft.Web.WebView2 namespaces ===", file=sys.stderr)
    try:
        from Microsoft.Web.WebView2.Core import CoreWebView2Cookie  # type: ignore[import-not-found]  # noqa: F401
        print("[chain] import from Microsoft.Web.WebView2.Core: OK", file=sys.stderr)
    except BaseException as exc:
        _dump_exc("from Microsoft.Web.WebView2.Core import CoreWebView2Cookie", exc)
    try:
        from Microsoft.Web.WebView2.WinForms import WebView2 as _WV2Type  # type: ignore[import-not-found]  # noqa: F401
        print("[chain] import from Microsoft.Web.WebView2.WinForms: OK", file=sys.stderr)
    except BaseException as exc:
        _dump_exc("from Microsoft.Web.WebView2.WinForms import WebView2", exc)

    print("[chain] === stage import pywebview Windows backend modules ===", file=sys.stderr)
    # This is the EXACT call pywebview's import_winforms() makes inside
    # the swallowing try/except at guilib.py:73-76. Doing it directly
    # surfaces what that catch hides.
    try:
        import webview.platforms.winforms  # type: ignore[import-not-found]  # noqa: F401
        print("[chain] import webview.platforms.winforms: OK", file=sys.stderr)
    except BaseException as exc:
        _dump_exc("import webview.platforms.winforms (THE SWALLOW SITE)", exc)
    try:
        import webview.platforms.edgechromium  # type: ignore[import-not-found]  # noqa: F401
        print("[chain] import webview.platforms.edgechromium: OK", file=sys.stderr)
    except BaseException as exc:
        _dump_exc("import webview.platforms.edgechromium", exc)

    print("[chain] === done ===", file=sys.stderr)
    # Exit 1 by design: this is the v0.5.9 diagnostic round. Forcing
    # the smoke-test step to fail keeps the broken Windows bundle from
    # auto-publishing; the diagnostic stderr above is what we came for.
    print("fail: v0.5.9 diagnostic round — see stderr above for the unmasked exceptions", file=sys.stderr)
    sys.exit(1)


def run() -> None:
    # CI smoke-test bypasses — never open a window, exit with status.
    # --healthz-only verifies the uvicorn/FastAPI half boots.
    # --check-windows-runtime verifies the pywebview/.NET loader half
    # resolves; required because --healthz-only doesn't `import clr`.
    if "--healthz-only" in sys.argv:
        _run_healthz_only()
        return
    if "--check-windows-runtime" in sys.argv:
        _run_check_windows_runtime()
        return

    _check_webview2_runtime()

    from app.config import PORT
    _start_server_thread(PORT)

    healthz_url = f"http://127.0.0.1:{PORT}/healthz"
    if not _wait_for_server(healthz_url):
        log.error("Server did not start within timeout — aborting")
        sys.exit(1)

    log.info("Server ready at http://127.0.0.1:%d", PORT)

    webview.create_window(
        title="GamePile",
        url=f"http://127.0.0.1:{PORT}/",
        width=1200,
        height=800,
        resizable=True,
        background_color="#0f0f13",  # matches --bg in style.css
    )

    # gui="gtk" is correct on Linux; on Windows pass None so pywebview
    # auto-selects edgechromium (and only edgechromium — we've already
    # verified the WebView2 runtime above). macOS isn't shipped in v5
    # but the auto-detect path would pick cocoa.
    gui = "gtk" if sys.platform == "linux" else None
    webview.start(gui=gui)
    sys.exit(0)


if __name__ == "__main__":
    run()
