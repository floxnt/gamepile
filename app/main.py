import logging
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import database as db
from app.routes import library, pick, refresh

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="Tonight's Pick", docs_url=None, redoc_url=None)

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

app.include_router(pick.router)
app.include_router(library.router)
app.include_router(refresh.router)


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


@app.on_event("startup")
async def startup():
    db.init_db()


def _open_browser(port: int) -> None:
    # Give uvicorn a moment to bind before opening the browser
    time.sleep(1.2)
    webbrowser.open(f"http://localhost:{port}")


def run() -> None:
    from app.config import PORT
    threading.Thread(target=_open_browser, args=(PORT,), daemon=True).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    run()
