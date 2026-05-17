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


def _run_check_windows_runtime() -> None:
    """CI smoke-test mode that exercises the .NET loader chain bypassed
    by --healthz-only. v0.5.3..v0.5.5 Windows bundles shipped broken
    because nothing tested `import clr` against an environment that
    matched a clean consumer machine. v0.5.6 sidesteps the environmental
    dependency entirely by bundling a .NET 8 coreclr runtime and pinning
    pythonnet to it — this detector verifies the pin actually took.

    Two-stage check:
    1. `import clr` succeeds — the bundled runtime resolved and the
       pythonnet bootstrap completed.
    2. The active runtime is coreclr, NOT netfx fallback. If
       `pythonnet.set_runtime()` in app/main.py's top-of-module block
       silently failed to apply (e.g., bundled runtime files missing,
       runtimeconfig.json malformed, import order regressed by a future
       refactor), pythonnet would fall back to netfx and the bundle
       would once again be at the mercy of the host's .NET Framework
       state. This second-stage assertion catches that regression at
       build time.

    On non-Windows: no-op exit 0. No display required — clr.dll
    activates the CLR but creates no window; importing edgechromium
    loads C# bindings but does not instantiate EdgeChrome.

    What this does NOT cover: window-actually-renders. CI runners are
    developer images and cannot honestly simulate a clean consumer
    Windows 11 environment for the GUI-rendering layer. The manual
    download-and-double-click test on real consumer hardware remains
    the release-acceptance criterion."""
    if sys.platform != "win32":
        print("ok: non-windows skip")
        sys.exit(0)
    try:
        import clr  # noqa: F401  triggers pythonnet → clr_loader bootstrap
        import pythonnet
        import webview.platforms.edgechromium  # noqa: F401  pywebview backend
        info = pythonnet.get_runtime_info()
        kind = info.kind if info is not None else "None"
        # "CoreCLR" is the only acceptable outcome on Windows; clr_loader's
        # netfx backend returns ".NET Framework" here. Anything other than
        # CoreCLR means set_runtime() in app/main.py's top-of-module block
        # didn't apply — a v0.5.3..v0.5.5-class regression.
        if kind == "CoreCLR":
            print(f"ok: runtime={kind} version={info.version}")
            sys.exit(0)
        print(
            f"fail: expected CoreCLR runtime active, got {kind!r}. "
            "set_runtime() did not apply — Windows bundle would crash "
            "on clean consumer machines exactly as v0.5.3..v0.5.5 did.",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"fail: {type(exc).__name__}: {exc}", file=sys.stderr)
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
