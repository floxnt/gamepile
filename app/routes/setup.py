"""v4 first-run setup wizard.

Multi-page flow:

  /setup/welcome
       │
       ├─► /setup/migrate  (shown only when data-dir .env detected
       │     │              and migration not yet handled)
       │     │
       │     ├─► [Migrate] → write .env → keyring, mark_migration_done("migrated")
       │     │              redirect /setup/done (creds already populated)
       │     └─► [Skip]   → mark_migration_done("declined"),
       │                    redirect /setup/api-key
       │
       └─► /setup/api-key      ─► /setup/steam-id  ─► /setup/validate
                ▲                       ▲                  │
                └───── /setup/edit ◄────┴─── (failure) ◄───┘
                                                            │
                                                            ▼
                                                       /setup/done
                                                       (kicks off sync,
                                                        polls progress,
                                                        redirects /shortlist)

In-flight credential values live in a module-level singleton dict
between page transitions — single-user desktop app, single window
makes this safe and avoids the FastAPI session-middleware overhead
just to hold two strings briefly. Cleared after successful persistence.
"""

import asyncio
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import credentials
from app import sync as sync_module
from app.fetchers import steam as steam_fetcher
from app.templates_config import templates

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# In-flight wizard state — module-level singleton
# ---------------------------------------------------------------------------

_setup_session: dict = {
    "api_key": None,
    "steam_id": None,
    "validation_error": None,
}

# Strong reference to the first-run sync task. asyncio only holds a weak
# one, so without this the task can be collected mid-flight. It also lets
# sync_status distinguish "still working" from "died on the way in".
_sync_task: Optional[asyncio.Task] = None


def _reset_session() -> None:
    _setup_session["api_key"] = None
    _setup_session["steam_id"] = None
    _setup_session["validation_error"] = None


async def _resolve_steam_id_input(api_key: str, value: str) -> tuple:
    """Normalise a SteamID input. Returns (steam_id_or_none, error_or_none).

    Accepts:
      - 17-digit numeric SteamID64 → returned as-is
      - Bare username "ike" → resolved via ResolveVanityURL
      - Full URL "https://steamcommunity.com/id/ike" → username extracted, resolved
      - Full URL "https://steamcommunity.com/profiles/76561198..." → digits extracted

    The resolution call requires a working API key — this is why the
    wizard collects the API key first.
    """
    value = value.strip()
    if not value:
        return (None, "SteamID can't be empty.")

    # Strip URL chrome if pasted
    lowered = value.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            lowered = value.lower()
    for host_prefix in ("www.steamcommunity.com/", "steamcommunity.com/"):
        if lowered.startswith(host_prefix):
            value = value[len(host_prefix):]
            lowered = value.lower()
            break
    # /profiles/<digits> → digits; /id/<vanity> → vanity
    if lowered.startswith("profiles/"):
        value = value[len("profiles/"):].rstrip("/").split("/", 1)[0]
    elif lowered.startswith("id/"):
        value = value[len("id/"):].rstrip("/").split("/", 1)[0]

    # Numeric SteamID64 — no resolution needed
    if value.isdigit() and len(value) >= 16:
        return (value, None)

    # Vanity → resolve
    try:
        async with httpx.AsyncClient() as client:
            resolved = await steam_fetcher.resolve_vanity_url(client, api_key, value)
    except Exception as exc:
        log.warning("vanity resolve failed: %s", exc)
        return (None, f"Couldn't resolve vanity name '{value}' — check your API key.")
    if not resolved:
        return (None, f"Couldn't resolve '{value}'. Make sure the vanity name "
                       f"exists at steamcommunity.com/id/{value}.")
    return (resolved, None)


async def _validate_credentials(api_key: str, steam_id: str) -> Optional[str]:
    """Run a test GetOwnedGames call to confirm the credentials work.
    Returns None on success, a user-facing error message on failure."""
    try:
        async with httpx.AsyncClient() as client:
            games = await steam_fetcher.fetch_owned_games(
                client, api_key=api_key, steam_id=steam_id,
            )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401 or exc.response.status_code == 403:
            return ("API rejected your credentials (HTTP "
                    f"{exc.response.status_code}). Double-check your API key "
                    "at steamcommunity.com/dev/apikey and your SteamID.")
        return f"Steam API returned HTTP {exc.response.status_code}. Try again in a moment."
    except httpx.RequestError as exc:
        return f"Couldn't reach Steam: {exc}. Check your network connection."
    except Exception as exc:
        return f"Validation failed: {exc}"

    # Empty library is a valid response — Steam returns the games array
    # (possibly empty) when both creds are valid. An empty response field
    # signals that the SteamID has private library settings.
    if not isinstance(games, list):
        return ("Steam responded but didn't return a games list. Your "
                "library might be set to private — change it to public at "
                "steamcommunity.com/my/edit/settings.")
    return None


# ---------------------------------------------------------------------------
# Wizard pages
# ---------------------------------------------------------------------------

@router.get("/setup/welcome", response_class=HTMLResponse)
async def welcome(request: Request):
    return templates.TemplateResponse(request, "setup_welcome.html", {})


@router.post("/setup/welcome", response_class=HTMLResponse)
async def welcome_continue(request: Request):
    """User clicks 'Get started' on welcome. Route to migrate page if
    a data-dir .env exists and migration hasn't been handled yet."""
    if (credentials.dotenv_path_for_migration() is not None
        and credentials.keyring_available()
        and not credentials.migration_already_handled()):
        return RedirectResponse(url="/setup/migrate", status_code=303)
    return RedirectResponse(url="/setup/api-key", status_code=303)


@router.get("/setup/migrate", response_class=HTMLResponse)
async def migrate_page(request: Request):
    env_path = credentials.dotenv_path_for_migration()
    return templates.TemplateResponse(request, "setup_migrate.html", {
        "env_path": str(env_path) if env_path else None,
    })


@router.post("/setup/migrate/migrate", response_class=HTMLResponse)
async def migrate_action(request: Request):
    success = credentials.migrate_env_to_keyring()
    if not success:
        # Migration shouldn't fail at this point; if it does, drop the user
        # into the regular wizard rather than blocking on a re-prompt.
        credentials.mark_migration_done("declined")
        return RedirectResponse(url="/setup/api-key", status_code=303)
    credentials.mark_migration_done("migrated")
    return RedirectResponse(url="/setup/done", status_code=303)


@router.post("/setup/migrate/skip", response_class=HTMLResponse)
async def migrate_skip(request: Request):
    credentials.mark_migration_done("declined")
    return RedirectResponse(url="/setup/api-key", status_code=303)


@router.get("/setup/api-key", response_class=HTMLResponse)
async def api_key_page(request: Request):
    return templates.TemplateResponse(request, "setup_api_key.html", {
        "api_key": _setup_session.get("api_key") or "",
    })


@router.post("/setup/api-key", response_class=HTMLResponse)
async def api_key_save(request: Request, api_key: str = Form(...)):
    api_key = api_key.strip()
    if not api_key:
        return templates.TemplateResponse(request, "setup_api_key.html", {
            "api_key": "",
            "error": "API key can't be empty.",
        })
    _setup_session["api_key"] = api_key
    return RedirectResponse(url="/setup/steam-id", status_code=303)


@router.get("/setup/steam-id", response_class=HTMLResponse)
async def steam_id_page(request: Request):
    if not _setup_session.get("api_key"):
        # User skipped ahead — bounce back.
        return RedirectResponse(url="/setup/api-key", status_code=303)
    return templates.TemplateResponse(request, "setup_steam_id.html", {
        "steam_id": _setup_session.get("steam_id") or "",
    })


@router.post("/setup/steam-id", response_class=HTMLResponse)
async def steam_id_save(request: Request, steam_id_input: str = Form(...)):
    api_key = _setup_session.get("api_key")
    if not api_key:
        return RedirectResponse(url="/setup/api-key", status_code=303)
    resolved, error = await _resolve_steam_id_input(api_key, steam_id_input)
    if error:
        return templates.TemplateResponse(request, "setup_steam_id.html", {
            "steam_id": steam_id_input,
            "error": error,
        })
    _setup_session["steam_id"] = resolved
    return RedirectResponse(url="/setup/validate", status_code=303)


@router.get("/setup/validate", response_class=HTMLResponse)
async def validate(request: Request):
    api_key = _setup_session.get("api_key")
    steam_id = _setup_session.get("steam_id")
    if not api_key or not steam_id:
        return RedirectResponse(url="/setup/api-key", status_code=303)

    error = await _validate_credentials(api_key, steam_id)
    if error:
        _setup_session["validation_error"] = error
        return RedirectResponse(url="/setup/edit", status_code=303)

    # Persist via credentials module. Keyring is the canonical write path;
    # fallback (no keyring) raises here, which would mean the user's setup
    # can't actually save — surface a clear error rather than swallowing.
    try:
        credentials.set_steam_api_key(api_key)
        credentials.set_steam_id(steam_id)
    except RuntimeError as exc:
        _setup_session["validation_error"] = (
            f"Can't save credentials: {exc} "
            "Set them via .env in your data directory and restart."
        )
        return RedirectResponse(url="/setup/edit", status_code=303)

    _reset_session()
    return RedirectResponse(url="/setup/done", status_code=303)


@router.get("/setup/edit", response_class=HTMLResponse)
async def edit_page(request: Request):
    """Combined edit page — surfaces both fields with the validation
    error so the user can fix whichever credential was wrong without
    losing the other."""
    return templates.TemplateResponse(request, "setup_edit.html", {
        "api_key": _setup_session.get("api_key") or "",
        "steam_id": _setup_session.get("steam_id") or "",
        "error": _setup_session.get("validation_error"),
    })


@router.post("/setup/edit", response_class=HTMLResponse)
async def edit_save(
    request: Request,
    api_key: str = Form(...),
    steam_id_input: str = Form(...),
):
    api_key = api_key.strip()
    if not api_key:
        return templates.TemplateResponse(request, "setup_edit.html", {
            "api_key": "", "steam_id": steam_id_input,
            "error": "API key can't be empty.",
        })
    _setup_session["api_key"] = api_key

    # Re-resolve the SteamID input (vanity may need re-resolution if API key changed).
    resolved, resolve_error = await _resolve_steam_id_input(api_key, steam_id_input)
    if resolve_error:
        return templates.TemplateResponse(request, "setup_edit.html", {
            "api_key": api_key, "steam_id": steam_id_input,
            "error": resolve_error,
        })
    _setup_session["steam_id"] = resolved

    error = await _validate_credentials(api_key, resolved)
    if error:
        _setup_session["validation_error"] = error
        return templates.TemplateResponse(request, "setup_edit.html", {
            "api_key": api_key, "steam_id": resolved,
            "error": error,
        })

    try:
        credentials.set_steam_api_key(api_key)
        credentials.set_steam_id(resolved)
    except RuntimeError as exc:
        return templates.TemplateResponse(request, "setup_edit.html", {
            "api_key": api_key, "steam_id": resolved,
            "error": f"Can't save credentials: {exc}",
        })
    _reset_session()
    return RedirectResponse(url="/setup/done", status_code=303)


# ---------------------------------------------------------------------------
# Done page — kicks off sync, polls progress, redirects when complete
# ---------------------------------------------------------------------------

@router.get("/setup/done", response_class=HTMLResponse)
async def done_page(request: Request):
    """Done page renders immediately; the inline HTMX trigger then POSTs
    /setup/start-sync to begin the initial library sync. Once kicked off,
    the page polls /setup/sync-status every 2s — the partial returned
    surfaces progress and triggers a /shortlist redirect when sync
    completes."""
    return templates.TemplateResponse(request, "setup_done.html", {})


@router.post("/setup/start-sync", response_class=HTMLResponse)
async def start_sync(request: Request):
    """Kick off the initial library sync as a background task. Returns
    the first sync-status partial so the polling chain begins."""
    global _sync_task
    if not sync_module.is_running():
        # Schedule as background task. The existing sync_module.progress
        # singleton tracks state for the polling endpoint to read.
        #
        # The reference is held deliberately. asyncio keeps only a weak
        # reference to a running task, so dropping this one lets the
        # first-run sync be garbage-collected mid-flight — and because
        # run_refresh's finally block would then never execute, the page
        # polls a progress object that stays at its defaults forever.
        # Holding it also gives sync_status a way to see the task died.
        _sync_task = asyncio.create_task(sync_module.run_refresh(force=False))
    return await sync_status(request)


def _sync_failure() -> Optional[str]:
    """Describe how the sync failed, or None if it didn't.

    Two distinct failure shapes, and neither is visible in completed_at:

    1. run_refresh caught an unhandled error. Its finally block still
       stamps completed_at, so the naive "completed_at is not None" check
       read a crashed sync as success and redirected the user into a
       half-populated app. progress.fatal_error is the honest signal.

    2. The coroutine died before or outside that try — cancelled, or
       raised on the way in. The finally never ran, nothing was stamped,
       and the page polls a default progress object forever showing
       "Starting…". Only the task object knows.
    """
    p = sync_module.progress
    if p.fatal_error:
        return p.fatal_error

    task = _sync_task
    if task is None or not task.done():
        return None

    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return "The sync was cancelled before it finished."
    if exc is not None:
        return str(exc) or exc.__class__.__name__
    if p.completed_at is None:
        # Returned without ever reaching the finally block.
        return "The sync stopped unexpectedly before it finished."
    return None


@router.get("/setup/sync-status", response_class=HTMLResponse)
async def sync_status(request: Request):
    """Polled by the done page every 2s. Returns a partial that:
      - Shows the current phase + game name + count while running
      - Renders an hx-trigger redirect to /shortlist when sync completes
      - Surfaces a failure with an escape hatch when it doesn't
    """
    p = sync_module.progress
    failure = _sync_failure()
    return templates.TemplateResponse(request, "partials/setup_sync_status.html", {
        "running": p.running,
        "phase": p.phase,
        "current_game": p.current_game,
        "current_index": p.current_index,
        "total_games": p.total_games,
        "completed": (not p.running) and p.completed_at is not None and not failure,
        "failed": bool(failure),
        "failure_message": failure,
        "errors": p.errors,
    })
