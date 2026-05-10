import logging
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import webview
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import credentials
from app import database as db
from app.routes import backlog, dashboard, feedback, game_detail, library, refresh, setup, shortlist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(title="GamePile", docs_url=None, redoc_url=None)

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
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


def run() -> None:
    from app.config import PORT

    server = uvicorn.Server(uvicorn.Config(
        "app.main:app",
        host="127.0.0.1",
        port=PORT,
        log_level="warning",
    ))

    # uvicorn must run in a daemon thread — pywebview's GTK event loop owns
    # the main thread (GTK requires this on Linux).
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    healthz_url = f"http://127.0.0.1:{PORT}/healthz"
    if not _wait_for_server(healthz_url):
        log.error("Server did not start within timeout — aborting")
        sys.exit(1)

    log.info("Server ready at http://127.0.0.1:%d", PORT)

    window = webview.create_window(
        title="GamePile",
        url=f"http://127.0.0.1:{PORT}/",
        width=1200,
        height=800,
        resizable=True,
        background_color="#0f0f13",  # matches --bg in style.css
    )

    # webview.start() blocks until the window is closed. Because the server
    # thread is a daemon, it dies automatically when this process exits.
    webview.start(gui="gtk")
    sys.exit(0)


if __name__ == "__main__":
    run()
