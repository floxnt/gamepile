"""
Library refresh orchestration.

Runs as a FastAPI background task. Progress is tracked in a module-level
dict so the /refresh/status polling endpoint can read it without shared state
complexity. Safe for single-user desktop use.

Caching behaviour (bypassed when force=True):
  - HLTB and OpenCritic scores are skipped if hltb_main_hours / opencritic_score
    is already populated AND last_refreshed is within CACHE_DAYS days.
  - Steam playtime, store details, and review data are always re-fetched
    (cheap, fast, and changes with some frequency).
  - last_refreshed is updated on every enrichment pass — even when HLTB/OC
    are served from cache — so the staleness check stays accurate.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import httpx

from app import database as db
from app.fetchers import hltb as hltb_fetcher
# OpenCritic disabled: API migrated to RapidAPI (requires paid key) as of 2025.
# Existing opencritic_score values in the DB remain valid and are still used for
# scoring; we just stop fetching new data.
from app.fetchers import steam as steam_fetcher
from app.fetchers import steamspy as steamspy_fetcher
from app.models import Game

log = logging.getLogger(__name__)

CACHE_DAYS = 30


@dataclass
class RefreshProgress:
    running: bool = False
    force: bool = False
    phase: str = ""
    current_game: str = ""
    current_index: int = 0
    total_games: int = 0
    games_added: int = 0
    games_updated: int = 0
    hltb_fetched: int = 0
    hltb_skipped: int = 0
    # OC fetch disabled (RapidAPI migration); fields kept for progress display compat
    oc_fetched: int = 0
    oc_skipped: int = 0
    spy_fetched: int = 0
    spy_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# Single shared progress instance — one refresh at a time.
progress = RefreshProgress()


def is_running() -> bool:
    return progress.running


def _is_stale(game: Game) -> bool:
    """True if enrichment data is old enough that we should re-fetch."""
    if game.last_refreshed is None:
        return True
    return datetime.utcnow() - game.last_refreshed >= timedelta(days=CACHE_DAYS)


async def run_refresh(force: bool = False) -> None:
    global progress
    if progress.running:
        return

    progress = RefreshProgress(running=True, force=force, started_at=datetime.utcnow())
    log.info("Refresh started (force=%s)", force)

    log_id: Optional[int] = None
    try:
        with db.get_db() as conn:
            log_id = db.start_refresh_log(conn)

        async with httpx.AsyncClient() as client:
            await _phase_steam(client)
            await _phase_enrich(client, force=force)

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
            "Refresh complete — added %d, updated %d, "
            "HLTB fetched/skipped %d/%d, OC fetched/skipped %d/%d, errors %d",
            progress.games_added, progress.games_updated,
            progress.hltb_fetched, progress.hltb_skipped,
            progress.oc_fetched, progress.oc_skipped,
            len(progress.errors),
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
        playtime = entry.get("playtime_forever", 0)
        is_new = appid not in existing_appids

        with db.get_db() as conn:
            # Only writes Steam-sourced fields; never touches HLTB/genres/scores.
            db.upsert_game_steam_fields(
                conn,
                appid=appid,
                name=name,
                playtime_minutes=playtime,
                last_played_steam=last_played,
                is_active=True,
            )
            # Pass playtime + last-played so initial inference can promote
            # actively-played games to in_progress on first sight.
            db.ensure_game_state(
                conn,
                appid,
                playtime_minutes=playtime,
                last_played_steam=last_played,
            )

        if is_new:
            progress.games_added += 1
        else:
            progress.games_updated += 1


async def _phase_enrich(client: httpx.AsyncClient, force: bool = False) -> None:
    """Enrich each game with store details, HLTB, OpenCritic, and review data.

    HLTB and OpenCritic are skipped when the existing value is populated and
    last_refreshed is within CACHE_DAYS — unless force=True.
    last_refreshed is always written so we know when we last checked.
    """
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn, active_only=True)

    progress.total_games = len(all_games)

    for i, gws in enumerate(all_games):
        game = gws.game
        progress.current_game = game.name
        progress.current_index = i + 1

        updates: dict = {}

        # --- Steam store details (always fetch — cheap and changes) ---
        progress.phase = f"Store details ({i+1}/{progress.total_games})"
        details = await steam_fetcher.fetch_app_details(client, game.appid)
        if details:
            updates.update(details)

        # --- HLTB (cached unless force or data missing or stale) ---
        if not force and game.hltb_main_hours is not None and not _is_stale(game):
            log.debug("HLTB cached: %s", game.name)
            progress.hltb_skipped += 1
        else:
            progress.phase = f"HLTB lookup ({i+1}/{progress.total_games})"
            hltb = await hltb_fetcher.fetch_hltb(game.name)
            if hltb:
                updates.update(hltb)
            progress.hltb_fetched += 1

        # OpenCritic fetch disabled — RapidAPI migration requires paid key (2025).
        # Existing scores in the DB are preserved and still used for scoring.
        progress.oc_skipped += 1

        # --- SteamSpy user tags (cached unless force or data missing or stale) ---
        if not force and game.user_tags and not _is_stale(game):
            log.debug("SteamSpy cached: %s", game.name)
            progress.spy_skipped += 1
        else:
            progress.phase = f"SteamSpy tags ({i+1}/{progress.total_games})"
            spy_tags = await steamspy_fetcher.fetch_user_tags(client, game.appid)
            if spy_tags:
                updates["user_tags"] = ",".join(name for name, _ in spy_tags)
            progress.spy_fetched += 1

        # --- Steam reviews (always fetch) ---
        progress.phase = f"Reviews ({i+1}/{progress.total_games})"
        reviews = await steam_fetcher.fetch_review_summary(client, game.appid)
        if reviews:
            updates.update(reviews)

        # Always write, even when sources were cached, so last_refreshed advances.
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
            user_tags=updates.get("user_tags", game.user_tags),
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
            # Refine inferred status now that HLTB data is available.
            # Skips games where the user has manually set a status.
            db.maybe_refine_inferred_status(
                conn,
                enriched.appid,
                enriched.playtime_minutes,
                enriched.hltb_main_hours,
                enriched.last_played_steam,
            )
