from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import sync

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.post("/refresh", response_class=HTMLResponse)
async def start_refresh(
    request: Request,
    background_tasks: BackgroundTasks,
    force: bool = False,
):
    if sync.is_running():
        return templates.TemplateResponse(request, "partials/refresh_status.html", {
            "progress": sync.progress,
            "already_running": True,
        })

    background_tasks.add_task(sync.run_refresh, force=force)

    return templates.TemplateResponse(request, "partials/refresh_status.html", {
        "progress": sync.progress,
        "already_running": False,
    })


@router.get("/refresh/status", response_class=HTMLResponse)
async def refresh_status(request: Request):
    return templates.TemplateResponse(request, "partials/refresh_status.html", {
        "progress": sync.progress,
        "already_running": False,
    })
