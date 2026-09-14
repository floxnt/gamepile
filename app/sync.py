"""
Library refresh orchestration.

Runs as a FastAPI background task. Progress is tracked in a module-level
dict so the /refresh/status polling endpoint can read it without shared state
complexity. Safe for single-user desktop use.

Caching policy (bypassed when force=True):
  - Per-game TTL is age-based (see _ttl_days). Newer games re-fetched more
    frequently; games released 2+ years ago are considered stable once
    successfully matched.
  - hltb_main_hours / user_tags missing always triggers a refetch regardless
    of TTL, so transient HLTB / SteamSpy outages can recover on the next run.
  - Steam playtime, store details, and review data are always re-fetched.
  - last_refreshed advances on every enrichment pass.

Adaptive pacing:
  Every HLTB lookup outcome (match vs miss) is recorded. If the last three
  outcomes are all misses, the next lookup gets a 2 s pause AND becomes
  eligible for a single 5 s back-off retry. A healthy run pays no pacing
  cost beyond Steam's existing 1.5 s store-details delay.
"""

import asyncio
import collections
import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import httpx

from dataclasses import fields as dc_fields, replace as dc_replace

from app import database as db
from app.fetchers import hltb as hltb_fetcher
from app.fetchers import steam as steam_fetcher
from app.fetchers import steam_achievements as achievements_fetcher
from app.fetchers import steamspy as steamspy_fetcher
from app.game_type import classify_game
from app.models import Game

log = logging.getLogger(__name__)

# Age-based TTL bands (in days). Indefinite once successfully matched.
_TTL_RECENT_DAYS = 30          # game released < 6 months ago
_TTL_MID_DAYS = 180            # game released 6 months to 2 years ago
_RECENT_AGE_DAYS = 180
_MID_AGE_DAYS = 730

_HLTB_BATCH_WINDOW = 3         # outcomes considered for the "batch unhealthy" check
_HLTB_BATCH_PAUSE = 2.0        # seconds inserted before next lookup when unhealthy


@dataclass
class HltbMissEntry:
    appid: int
    name: str
    attempts: list[str]
    backoff_used: bool

    def to_dict(self) -> dict:
        return {
            "appid": self.appid,
            "name": self.name,
            "attempts": self.attempts,
            "backoff_used": self.backoff_used,
        }


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
    hltb_attempted: int = 0          # tried a real HLTB lookup
    hltb_matched: int = 0            # of those, found a match
    hltb_missed: int = 0             # of those, came back empty (genuine + transient combined)
    hltb_transient_recovered: int = 0  # match found via 5s back-off retry — likely was transient
    hltb_skipped: int = 0            # didn't try (cache hit)
    spy_fetched: int = 0
    spy_skipped: int = 0
    achievements_fetched: int = 0
    achievements_skipped: int = 0
    achievements_no_data: int = 0
    release_date_backfilled: int = 0
    description_backfilled: int = 0
    errors: list[str] = field(default_factory=list)
    hltb_misses: list[HltbMissEntry] = field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    elapsed_seconds: Optional[float] = None
    # Set only when the run died on an unhandled error. Needed because the
    # finally block below stamps completed_at unconditionally, so
    # completed_at alone can't tell success from failure — the setup page
    # used to read a crashed sync as "complete" and redirect on it.
    # `errors` is not a substitute: it also collects non-fatal HLTB misses.
    fatal_error: Optional[str] = None


# Single shared progress instance — one refresh at a time.
progress = RefreshProgress()


def is_running() -> bool:
    return progress.running


# ---------------------------------------------------------------------------
# Caching policy
# ---------------------------------------------------------------------------

def _ttl_days(release_date: Optional[datetime]) -> Optional[int]:
    """
    Return the cache TTL (in days) for enrichment data, or None for
    "indefinite once matched". A None TTL means: as long as the data is
    populated, never re-fetch on a non-force refresh.
    """
    if release_date is None:
        # No release info — be conservative: don't waste lookups on what's
        # most likely an old, stable entry. Force refreshes still bypass.
        return None
    age_days = (datetime.utcnow() - release_date).days
    if age_days < _RECENT_AGE_DAYS:
        return _TTL_RECENT_DAYS
    if age_days < _MID_AGE_DAYS:
        return _TTL_MID_DAYS
    return None


def _is_stale(game: Game) -> bool:
    """True when the existing enrichment data is older than the age-based TTL."""
    if game.last_refreshed is None:
        return True
    ttl = _ttl_days(game.release_date)
    if ttl is None:
        return False
    return datetime.utcnow() - game.last_refreshed >= timedelta(days=ttl)



def _source_stale(game: Game, timestamp: Optional[datetime]) -> bool:
    if timestamp is None:
        return True
    ttl = _ttl_days(game.release_date)
    return ttl is not None and datetime.utcnow() - timestamp >= timedelta(days=ttl)


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

async def run_refresh(force: bool = False) -> None:
    global progress
    if progress.running:
        return

    started = datetime.utcnow()
    progress = RefreshProgress(running=True, force=force, started_at=started)
    log.info("Refresh started (force=%s)", force)

    log_id: Optional[int] = None
    t0 = time.monotonic()
    try:
        with db.get_db() as conn:
            log_id = db.start_refresh_log(conn)

        async with httpx.AsyncClient() as client:
            await _phase_steam(client)
            await _phase_enrich(client, force=force)

        progress.elapsed_seconds = time.monotonic() - t0

        with db.get_db() as conn:
            db.finish_refresh_log(
                conn,
                log_id,
                progress.games_added,
                progress.games_updated,
                _serialise_errors(),
            )

    except Exception as exc:
        log.exception("Refresh failed with unhandled error")
        progress.fatal_error = str(exc) or exc.__class__.__name__
        progress.errors.append(f"Fatal: {exc}")
        progress.elapsed_seconds = time.monotonic() - t0
        if log_id is not None:
            try:
                with db.get_db() as conn:
                    db.finish_refresh_log(conn, log_id, 0, 0, _serialise_errors())
            except Exception:
                pass
    finally:
        progress.running = False
        progress.completed_at = datetime.utcnow()
        log.info(
            "Refresh complete in %.1fs — added %d, updated %d, "
            "HLTB matched %d / missed %d / skipped %d (recovered %d via backoff), "
            "release_date backfilled %d, description backfilled %d, errors %d",
            progress.elapsed_seconds or 0.0,
            progress.games_added, progress.games_updated,
            progress.hltb_matched, progress.hltb_missed, progress.hltb_skipped,
            progress.hltb_transient_recovered,
            progress.release_date_backfilled,
            progress.description_backfilled,
            len(progress.errors),
        )


def _serialise_errors() -> Optional[str]:
    """JSON blob of non-fatal misses + fatal errors for refresh_log.errors."""
    if not progress.errors and not progress.hltb_misses:
        return None
    payload = {
        "fatal": progress.errors,
        "hltb_matched": progress.hltb_matched,
        "hltb_missed": progress.hltb_missed,
        "hltb_transient_recovered": progress.hltb_transient_recovered,
        "hltb_misses": [m.to_dict() for m in progress.hltb_misses],
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Phase 1 — Steam library
# ---------------------------------------------------------------------------

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
            db.upsert_game_steam_fields(
                conn,
                appid=appid,
                name=name,
                playtime_minutes=playtime,
                last_played_steam=last_played,
                is_active=True,
            )
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


# ---------------------------------------------------------------------------
# Phase 2 — Enrichment (HLTB, SteamSpy, Steam reviews)
# ---------------------------------------------------------------------------

async def _phase_enrich(client: httpx.AsyncClient, force: bool = False) -> None:
    """Enrich each game with store details, HLTB, and review data.

    OpenCritic was integrated in v1 and removed in v3 — see PROJECT_STATE.md.
    The opencritic_score column is preserved nullable in case we revisit;
    enrichment never writes to it now, so existing values pass through
    untouched and new games stay NULL.
    """
    with db.get_db() as conn:
        all_games = db.get_games_with_state(conn, active_only=True)

    progress.total_games = len(all_games)
    hltb_outcomes: collections.deque[bool] = collections.deque(maxlen=_HLTB_BATCH_WINDOW)

    for i, gws in enumerate(all_games):
        game = gws.game
        progress.current_game = game.name
        progress.current_index = i + 1

        updates: dict = {}
        had_release_date = game.release_date is not None
        had_description = game.description is not None

        # --- Steam store details (always fetch — cheap and changes) ---
        progress.phase = f"Store details ({i+1}/{progress.total_games})"
        details = await steam_fetcher.fetch_app_details(client, game.appid)
        if details:
            updates.update(details)
            if not had_release_date and details.get("release_date") is not None:
                progress.release_date_backfilled += 1
            if not had_description and details.get("description") is not None:
                progress.description_backfilled += 1

        # Resolve the effective release_date for this game (post-merge) so
        # cache decisions below use the freshest available value.
        effective_release = updates.get("release_date", game.release_date)
        effective_game = _shadow_game(game, release_date=effective_release)

        # --- HLTB ---
        if not force and game.hltb_main_hours is not None and not _source_stale(effective_game, game.hltb_fetched_at):
            log.debug("HLTB cached: %s", game.name)
            progress.hltb_skipped += 1
        elif game.hltb_id_manual:
            # Phase 4 manual HLTB ID override — bypass name-based search
            # entirely, fetch by ID, persist whatever HLTB returns. Misses
            # here are noteworthy (the user provided an ID that doesn't
            # resolve) — keep stored values via COALESCE rather than
            # nulling, and don't append to hltb_outcomes (this lookup
            # carries no signal about server health for the adaptive
            # pacing window).
            progress.phase = f"HLTB by-id ({i+1}/{progress.total_games})"
            result = await hltb_fetcher.fetch_hltb_by_id(game.hltb_id_manual)
            progress.hltb_attempted += 1
            if result.found:
                updates.update({
                    "hltb_main_hours": result.hltb_main_hours,
                    "hltb_main_extra_hours": result.hltb_main_extra_hours,
                    "hltb_completionist_hours": result.hltb_completionist_hours,
                })
                updates["hltb_fetched_at"] = datetime.utcnow()
                progress.hltb_matched += 1
            else:
                progress.hltb_missed += 1
                progress.hltb_misses.append(HltbMissEntry(
                    appid=game.appid,
                    name=f"{game.name} (manual id={game.hltb_id_manual})",
                    attempts=[f"id={game.hltb_id_manual}"],
                    backoff_used=False,
                ))
        else:
            # Adaptive pacing: pause before this lookup if recent batch is unhealthy
            batch_unhealthy = (
                len(hltb_outcomes) == _HLTB_BATCH_WINDOW
                and not any(hltb_outcomes)
            )
            if batch_unhealthy:
                log.info("HLTB: last %d misses — pausing %.1fs", _HLTB_BATCH_WINDOW, _HLTB_BATCH_PAUSE)
                await asyncio.sleep(_HLTB_BATCH_PAUSE)

            progress.phase = f"HLTB lookup ({i+1}/{progress.total_games})"
            result = await hltb_fetcher.fetch_hltb(
                game.name,
                allow_backoff_retry=batch_unhealthy,
            )
            progress.hltb_attempted += 1

            if result.found:
                updates.update({
                    "hltb_main_hours": result.hltb_main_hours,
                    "hltb_main_extra_hours": result.hltb_main_extra_hours,
                    "hltb_completionist_hours": result.hltb_completionist_hours,
                })
                updates["hltb_fetched_at"] = datetime.utcnow()
                progress.hltb_matched += 1
                if result.backoff_used:
                    progress.hltb_transient_recovered += 1
                hltb_outcomes.append(True)
            else:
                progress.hltb_missed += 1
                progress.hltb_misses.append(HltbMissEntry(
                    appid=game.appid,
                    name=game.name,
                    attempts=list(result.queries_tried),
                    backoff_used=result.backoff_used,
                ))
                hltb_outcomes.append(False)

        # --- SteamSpy data (tags only — playtime_median_avg_ratio was a
        # hook-point metric, removed in v0.7.0; column preserved dormant). ---
        if not force and game.user_tags and not _source_stale(effective_game, game.tags_fetched_at):
            log.debug("SteamSpy cached: %s", game.name)
            progress.spy_skipped += 1
        else:
            progress.phase = f"SteamSpy ({i+1}/{progress.total_games})"
            spy = await steamspy_fetcher.fetch_steamspy_data(client, game.appid)
            if spy is not None and spy.user_tags:
                updates["user_tags"] = ",".join(name for name, _ in spy.user_tags)
                updates["tags_fetched_at"] = datetime.utcnow()
            progress.spy_fetched += 1

        # --- Steam reviews (always fetch — aggregate fields only;
        # review-playtime-derived hook-point metrics removed in v0.7.0). ---
        progress.phase = f"Reviews ({i+1}/{progress.total_games})"
        reviews = await steam_fetcher.fetch_review_data(client, game.appid)
        if reviews:
            reviews.pop("playtimes", None)
            updates.update(reviews)

        # Global percentages change slowly; a user's progress can change
        # on any game regardless of release age. Keep separate clocks.
        has_achievements = game.median_achievement_unlock_pct is not None
        if not force and has_achievements and not _source_stale(effective_game, game.achievements_fetched_at):
            progress.achievements_skipped += 1
        else:
            progress.phase = f"Achievements ({i+1}/{progress.total_games})"
            achs = await achievements_fetcher.fetch_global_achievement_percentages(client, game.appid)
            if achs is None:
                progress.achievements_no_data += 1
            else:
                percents = [a["percent"] for a in achs if a.get("percent") is not None]
                if percents:
                    has_achievements = True
                    updates["median_achievement_unlock_pct"] = float(statistics.median(percents))
                    updates["achievements_fetched_at"] = datetime.utcnow()
                progress.achievements_fetched += 1
        user_stale = (game.user_achievements_fetched_at is None
            or datetime.utcnow() - game.user_achievements_fetched_at >= timedelta(days=1)
            or (game.last_played_steam is not None
                and game.last_played_steam > game.user_achievements_fetched_at))
        if has_achievements and (force or user_stale):
            user_pct = await achievements_fetcher.fetch_player_achievement_pct(client, game.appid)
            if user_pct is not None:
                updates["user_achievement_pct"] = user_pct
                updates["user_achievements_fetched_at"] = datetime.utcnow()

        # --- Game-type classification ---
        # The `coming_soon` flag from appdetails informs early_access detection
        # but is NOT a Game-row column — pop it out of `updates` so the
        # downstream Game() construction doesn't see it.
        coming_soon = updates.pop("coming_soon", False)

        # Manual-override safety: when the user has set game_type via the
        # Game Detail Type dropdown (game_type_manual=True), refresh
        # inference must not override. This is the primary safety; the
        # COALESCE in upsert_game's UPDATE is the secondary layer.
        if not game.game_type_manual:
            # Build a temporary merged Game with this iteration's `updates`
            # applied so classify_game sees freshly-fetched genres /
            # app_type / user_tags rather than the stale row values.
            valid_field_names = {f.name for f in dc_fields(Game)}
            merge_overrides = {k: v for k, v in updates.items() if k in valid_field_names}
            merged = dc_replace(game, **merge_overrides) if merge_overrides else game
            new_type = classify_game(merged, coming_soon=coming_soon)
            updates["game_type"] = new_type

        with db.get_db() as conn:
            enriched = db.apply_enrichment(conn, game, updates)
            if enriched is not None:
                db.maybe_refine_inferred_status(
                    conn, enriched.appid, enriched.playtime_minutes,
                    enriched.hltb_main_hours, enriched.last_played_steam,
                )


def _shadow_game(game: Game, release_date: Optional[datetime]) -> Game:
    """Return a copy of ``game`` with release_date overridden — used so the
    age-based cache check below can see a release_date freshly fetched in
    this same iteration before the row is written back."""
    if game.release_date == release_date:
        return game
    return dc_replace(game, release_date=release_date)
