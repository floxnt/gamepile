import logging
import sys
import threading
import time

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


def run() -> None:
    # CI smoke-test bypass — never opens a window, exits with status.
    if "--healthz-only" in sys.argv:
        _run_healthz_only()
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
