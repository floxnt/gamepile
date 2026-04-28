"""
Library refresh orchestration.

Runs as a FastAPI background task. Progress is tracked in a module-level
dict so the /refresh/status polling endpoint can read it without shared state
complexity. Safe for single-user desktop use.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

from app import database as db
from app.fetchers import hltb as hltb_fetcher
from app.fetchers import opencritic as oc_fetcher
from app.fetchers import steam as steam_fetcher
from app.models import Game

log = logging.getLogger(__name__)


@dataclass
class RefreshProgress:
    running: bool = False
    phase: str = ""
    current_game: str = ""
    current_index: int = 0
    total_games: int = 0
    games_added: int = 0
    games_updated: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# Single shared progress instance — one refresh at a time.
progress = RefreshProgress()


def is_running() -> bool:
    return progress.running


async def run_refresh() -> None:
    global progress
    if progress.running:
        return

    progress = RefreshProgress(running=True, started_at=datetime.utcnow())
    log.info("Refresh started")

    log_id: Optional[int] = None
    try:
        with db.get_db() as conn:
            log_id = db.start_refresh_log(conn)

        async with httpx.AsyncClient() as client:
            await _phase_steam(client)
            await _phase_enrich(client)

        with db.get_db() as conn:
            db.finish_refresh_log(
                conn,
                log_id,
                progress.games_added,
                progress.games_updated,
                "\n".join(progress.errors) if progress.errors else None,
            )

    except Exception as exc:
        log.exception("Refresh failed with unhandled error")
        progress.errors.append(f"Fatal: {exc}")
        if log_id is not None:
            try:
                with db.get_db() as conn:
                    db.finish_refresh_log(conn, log_id, 0, 0, str(exc))
            except Exception:
                pass
    finally:
        progress.running = False
        progress.completed_at = datetime.utcnow()
        log.info(
            "Refresh complete — added %d, updated %d, errors %d",
            progress.games_added, progress.games_updated, len(progress.errors),
        )


async def _phase_steam(client: httpx.AsyncClient) -> None:
    progress.phase = "Fetching Steam library"
    progress.current_game = ""

    owned = await steam_fetcher.fetch_owned_games(client)
    progress.total_games = len(owned)
    log.info("Steam returned %d owned games", len(owned))

    steam_appids = {g["appid"] for g in owned}

    with db.get_db() as conn:
        existing_appids = db.get_all_active_appids(conn)

    # Mark games no longer in Steam library as inactive
    removed = existing_appids - steam_appids
    if removed:
        with db.get_db() as conn:
            db.mark_inactive(conn, list(removed))
        log.info("Marked %d games inactive (removed from library)", len(removed))

    for i, entry in enumerate(owned):
        appid = entry["appid"]
        name = entry.get("name", f"App {appid}")
        progress.current_game = name
        progress.current_index = i + 1

        last_played = steam_fetcher.parse_last_played(entry.get("rtime_last_played"))
        now = datetime.utcnow()

        game = Game(
            appid=appid,
            name=name,
            playtime_minutes=entry.get("playtime_forever", 0),
            last_played_steam=last_played,
            installed=None,
            hltb_main_hours=None,
            hltb_main_extra_hours=None,
            hltb_completionist_hours=None,
            genres="",
            tags="",
            developer=None,
            publisher=None,
            metacritic_score=None,
            opencritic_score=None,
            steam_review_pct=None,
            steam_review_count=None,
            last_refreshed=now,
            is_active=True,
        )

        is_new = appid not in existing_appids
        with db.get_db() as conn:
            db.upsert_game(conn, game)
            db.ensure_game_state(conn, appid)

        if is_new:
            progress.games_added += 1
        else:
            progress.games_updated += 1


async def _phase_enrich(client: httpx.AsyncClient) -> None:
    """Enrich each game with store details, HLTB, OpenCritic, and review data."""
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn, active_only=True)

    progress.total_games = len(all_games)

    for i, gws in enumerate(all_games):
        game = gws.game
        progress.current_game = game.name
        progress.current_index = i + 1

        updates: dict = {}

        # --- Steam store details ---
        progress.phase = f"Fetching store details ({i+1}/{progress.total_games})"
        details = await steam_fetcher.fetch_app_details(client, game.appid)
        if details:
            updates.update(details)
        else:
            log.debug("No store details for %s (%d)", game.name, game.appid)

        # --- HLTB ---
        progress.phase = f"Fetching HLTB data ({i+1}/{progress.total_games})"
        hltb = await hltb_fetcher.fetch_hltb(game.name)
        if hltb:
            updates.update(hltb)

        # --- OpenCritic ---
        progress.phase = f"Fetching OpenCritic data ({i+1}/{progress.total_games})"
        oc_score = await oc_fetcher.fetch_opencritic_score(client, game.name)
        if oc_score is not None:
            updates["opencritic_score"] = oc_score

        # --- Steam reviews ---
        progress.phase = f"Fetching review data ({i+1}/{progress.total_games})"
        reviews = await steam_fetcher.fetch_review_summary(client, game.appid)
        if reviews:
            updates.update(reviews)

        if updates:
            enriched = Game(
                appid=game.appid,
                name=game.name,
                playtime_minutes=game.playtime_minutes,
                last_played_steam=game.last_played_steam,
                installed=game.installed,
                hltb_main_hours=updates.get("hltb_main_hours", game.hltb_main_hours),
                hltb_main_extra_hours=updates.get("hltb_main_extra_hours", game.hltb_main_extra_hours),
                hltb_completionist_hours=updates.get("hltb_completionist_hours", game.hltb_completionist_hours),
                genres=updates.get("genres", game.genres),
                tags=updates.get("tags", game.tags),
                developer=updates.get("developer", game.developer),
                publisher=updates.get("publisher", game.publisher),
                metacritic_score=updates.get("metacritic_score", game.metacritic_score),
                opencritic_score=updates.get("opencritic_score", game.opencritic_score),
                steam_review_pct=updates.get("steam_review_pct", game.steam_review_pct),
                steam_review_count=updates.get("steam_review_count", game.steam_review_count),
                last_refreshed=datetime.utcnow(),
                is_active=True,
            )
            with db.get_db() as conn:
                db.upsert_game(conn, enriched)
