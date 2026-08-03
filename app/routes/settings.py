"""v4 settings page — credential management after initial setup.

Shows current credentials (API key masked, SteamID visible) plus
edit-in-place forms that re-validate against Steam's GetOwnedGames
before persisting. Save fails inline (no persist) when validation fails;
no full-wizard re-walk required for a single-credential change.

When keyring is unavailable and the app is running off .env fallback,
the settings page surfaces a banner explaining the situation."""

import logging

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import backup as backup_module
from app import credentials
from app import database as db
from app.fetchers import steam as steam_fetcher
from app.routes.setup import _resolve_steam_id_input, _validate_credentials
from app.templates_config import templates

log = logging.getLogger(__name__)

router = APIRouter()


def _mask_api_key(value: str) -> str:
    """Render the API key as 'XXXX…last4' so the user can verify which
    key they're looking at without exposing the full value to a
    shoulder-surfer."""
    if not value:
        return ""
    if len(value) <= 4:
        return "·" * len(value)
    return "·" * (len(value) - 4) + value[-4:]


def _build_context() -> dict:
    """Shared context for the settings page render — used by GET and
    by the save handlers' re-render-with-error path."""
    api_key = credentials.get_steam_api_key() or ""
    steam_id = credentials.get_steam_id() or ""
    return {
        "api_key_masked": _mask_api_key(api_key),
        "api_key_present": bool(api_key),
        "steam_id": steam_id,
        "using_env_fallback": credentials.using_env_fallback(),
        "keyring_available": credentials.keyring_available(),
        "api_key_error": None,
        "steam_id_error": None,
        "api_key_saved": False,
        "steam_id_saved": False,
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", _build_context())


@router.post("/settings/api-key", response_class=HTMLResponse)
async def update_api_key(request: Request, api_key: str = Form(...)):
    api_key = api_key.strip()
    ctx = _build_context()
    if not api_key:
        ctx["api_key_error"] = "API key can't be empty."
        return templates.TemplateResponse(request, "settings.html", ctx)

    # Re-validate using the new API key paired with the existing SteamID.
    steam_id = credentials.get_steam_id()
    if not steam_id:
        ctx["api_key_error"] = "Set your SteamID first."
        return templates.TemplateResponse(request, "settings.html", ctx)

    error = await _validate_credentials(api_key, steam_id)
    if error:
        ctx["api_key_error"] = error
        return templates.TemplateResponse(request, "settings.html", ctx)

    try:
        credentials.set_steam_api_key(api_key)
    except RuntimeError as exc:
        ctx["api_key_error"] = f"Couldn't save: {exc}"
        return templates.TemplateResponse(request, "settings.html", ctx)

    ctx = _build_context()
    ctx["api_key_saved"] = True
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings/steam-id", response_class=HTMLResponse)
async def update_steam_id(request: Request, steam_id_input: str = Form(...)):
    ctx = _build_context()
    api_key = credentials.get_steam_api_key()
    if not api_key:
        ctx["steam_id_error"] = "Set your API key first."
        return templates.TemplateResponse(request, "settings.html", ctx)

    resolved, resolve_error = await _resolve_steam_id_input(api_key, steam_id_input)
    if resolve_error:
        ctx["steam_id_error"] = resolve_error
        return templates.TemplateResponse(request, "settings.html", ctx)

    error = await _validate_credentials(api_key, resolved)
    if error:
        ctx["steam_id_error"] = error
        return templates.TemplateResponse(request, "settings.html", ctx)

    try:
        credentials.set_steam_id(resolved)
    except RuntimeError as exc:
        ctx["steam_id_error"] = f"Couldn't save: {exc}"
        return templates.TemplateResponse(request, "settings.html", ctx)

    ctx = _build_context()
    ctx["steam_id_saved"] = True
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings/export", response_class=HTMLResponse)
async def export_backup(request: Request):
    """Write the user-authored layer to disk and report where it landed.

    Deliberately not a Content-Disposition download. GamePile runs inside
    a webview, and neither embedded engine completes one: pywebview gates
    downloads behind a flag that defaults off, and the Windows backend
    cancels them outright. The button did nothing at all, silently. See
    app/backup.py for the full detail.

    Because there's no save dialog to show the user where the file went,
    the response must name the resolved path — "Saved!" alone would be
    useless.
    """
    try:
        with db.get_db() as conn:
            path = backup_module.write_backup(conn)
    except OSError as exc:
        target = backup_module.default_target_dir()
        log.exception("Backup export failed")
        # Returned as 200 on purpose. htmx only swaps 2xx responses, so a
        # 500 here would render nothing at all — reproducing the silent
        # no-op this whole fix exists to remove. The failure is carried
        # in the partial, where the user can actually see it.
        return templates.TemplateResponse(
            request, "partials/settings_export_result.html",
            {"ok": False, "target_dir": str(target), "error": str(exc)},
        )

    return templates.TemplateResponse(
        request, "partials/settings_export_result.html",
        {"ok": True, "path": str(path)},
    )
