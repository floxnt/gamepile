import logging
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import webview
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import database as db
from app.routes import feedback, library, pick, refresh

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Tonight's Pick", docs_url=None, redoc_url=None)

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

app.include_router(pick.router)
app.include_router(library.router)
app.include_router(refresh.router)
app.include_router(feedback.router)


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
        title="Tonight's Pick",
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
