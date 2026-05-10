"""
Steam achievement fetchers (Web API).

Two endpoints, both used together to produce display-name-annotated unlock
percentages for the v3 Phase 1a engagement signals:

  - GetGlobalAchievementPercentagesForApp (public, no key) — returns
    unlock percentages keyed by internal achievement ID
  - GetSchemaForGame (key required, but we have one) — returns the
    achievement schema with displayName per ID

The display-name resolution is essential: the percentages endpoint alone
returns opaque internal IDs (e.g. `ACH00`, `TrophyTitle_00_fiber_Steam`)
for many games, which makes the story-completion heuristic in
hook_metrics.py useless without join-side metadata. See the verification
script under tests/ for the empirical motivation.

The combined wrapper `fetch_achievements_with_metadata` returns the
joined data structure callers actually want.

Pacing: simple per-request delay (matches the appdetails fetcher pattern,
not HLTB's full adaptive-window logic — Steam Web API tolerates sustained
requests well at modest pace, and 429s are rare on these endpoints).
Retry-with-backoff on transient HTTP errors. Schema fetch failures are
non-fatal — the wrapper falls back to using the internal name as the
display name.
"""

import asyncio
import logging
from typing import Optional

import httpx

from app.credentials import get_steam_api_key

log = logging.getLogger(__name__)

_PERCENTAGES_URL = "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"
_SCHEMA_URL = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"

# Match the existing appdetails throttling style — small constant delay
# between requests rather than the full HLTB adaptive-window logic.
_REQUEST_DELAY = 0.2

# Retry-on-transient-error tuning. 429 / 5xx → backoff and retry; 404 →
# game has no achievements → return None immediately (not an error).
_MAX_RETRIES = 3
_INITIAL_BACKOFF = 1.0


async def fetch_global_achievement_percentages(
    client: httpx.AsyncClient, appid: int,
) -> Optional[list[dict]]:
    """Return [{name, percent}, ...] sorted by percent desc, or None.

    None means the game has no achievements (404 or empty list in the
    response). Any other failure (network error, 5xx after retries) also
    returns None — the caller treats achievements as enrichment, not core,
    and a single missing fetch shouldn't bubble.
    """
    await asyncio.sleep(_REQUEST_DELAY)

    backoff = _INITIAL_BACKOFF
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await client.get(_PERCENTAGES_URL, params={"gameid": appid}, timeout=15)
        except httpx.RequestError as exc:
            log.warning("achievements: request error for %d: %s", appid, exc)
            return None

        if resp.status_code == 404:
            # No achievements for this app — common for software, betas, MP-only.
            return None

        if resp.status_code in (429, 500, 502, 503, 504):
            log.warning(
                "achievements: HTTP %d for %d (attempt %d/%d), backing off %.1fs",
                resp.status_code, appid, attempt + 1, _MAX_RETRIES, backoff,
            )
            await asyncio.sleep(backoff)
            backoff *= 2
            continue

        if not resp.is_success:
            log.warning("achievements: HTTP %d for %d", resp.status_code, appid)
            return None

        try:
            data = resp.json()
        except Exception as exc:
            log.warning("achievements: JSON parse error for %d: %s", appid, exc)
            return None

        achievements = (data.get("achievementpercentages") or {}).get("achievements") or []
        if not achievements:
            return None

        # Steam returns ordered by percent desc, but normalize defensively.
        normalized: list = []
        for entry in achievements:
            name = entry.get("name")
            pct = entry.get("percent")
            if name is None or pct is None:
                continue
            try:
                normalized.append({"name": str(name), "percent": float(pct)})
            except (TypeError, ValueError):
                continue
        normalized.sort(key=lambda x: -x["percent"])
        return normalized

    log.warning("achievements: gave up after retries for %d", appid)
    return None


async def fetch_achievement_schema(
    client: httpx.AsyncClient, appid: int,
) -> Optional[dict]:
    """Return {internal_name: display_name, ...} from GetSchemaForGame, or None.

    None means schema unavailable (developer didn't publish stats publicly,
    legacy game without schema, network/parse error after retries). Callers
    should fall back to using the internal name as the display name.
    """
    await asyncio.sleep(_REQUEST_DELAY)

    backoff = _INITIAL_BACKOFF
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await client.get(
                _SCHEMA_URL,
                params={"key": get_steam_api_key(), "appid": appid, "l": "english"},
                timeout=15,
            )
        except httpx.RequestError as exc:
            log.warning("schema: request error for %d: %s", appid, exc)
            return None

        if resp.status_code in (403, 404):
            log.debug("schema: HTTP %d for %d (no public stats schema)", resp.status_code, appid)
            return None

        if resp.status_code in (429, 500, 502, 503, 504):
            log.warning(
                "schema: HTTP %d for %d (attempt %d/%d), backing off %.1fs",
                resp.status_code, appid, attempt + 1, _MAX_RETRIES, backoff,
            )
            await asyncio.sleep(backoff)
            backoff *= 2
            continue

        if not resp.is_success:
            log.warning("schema: HTTP %d for %d", resp.status_code, appid)
            return None

        try:
            data = resp.json()
        except Exception as exc:
            log.warning("schema: JSON parse error for %d: %s", appid, exc)
            return None

        achievements = (
            (data.get("game") or {})
            .get("availableGameStats", {})
            .get("achievements")
        ) or []
        if not achievements:
            return None

        out: dict = {}
        for ach in achievements:
            name = ach.get("name")
            display = ach.get("displayName")
            if name and display:
                out[str(name)] = str(display)
        return out or None

    log.warning("schema: gave up after retries for %d", appid)
    return None


async def fetch_achievements_with_metadata(
    client: httpx.AsyncClient, appid: int,
) -> Optional[list[dict]]:
    """Return [{name, displayName, percent}, ...] sorted by percent desc.

    Combines GetGlobalAchievementPercentagesForApp (always-fetched) with
    GetSchemaForGame (best-effort) so the heuristic in hook_metrics.py can
    pattern-match against displayName instead of opaque internal IDs.

    None when the game has no achievements at all (404 on percentages).
    When the schema fetch fails or returns no entries, displayName falls
    back to the internal name. Callers needing the original internal-ID
    fallback path can still see `name` is unchanged.
    """
    percentages = await fetch_global_achievement_percentages(client, appid)
    if percentages is None:
        return None

    schema = await fetch_achievement_schema(client, appid)
    if schema is None:
        log.info(
            "schema: falling back to internal IDs for %d (heuristic accuracy degraded for this game)",
            appid,
        )
        schema = {}

    enriched: list = []
    for ach in percentages:
        name = ach["name"]
        display = schema.get(name) or name
        enriched.append({"name": name, "displayName": display, "percent": ach["percent"]})
    return enriched
