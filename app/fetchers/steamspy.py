"""
SteamSpy fetcher.

Returns combined per-game data — user tags (top 10 by vote) plus the
playtime stats (median_forever, average_forever) used by the v3 hook-
point Phase 1a `playtime_median_avg_ratio` metric. One API call returns
all of it.

Rate limit: 1 request/second enforced via an asyncio lock + sleep.
On 429: exponential backoff (1s, 2s, 4s) up to 3 retries.
On 404 / no data / network error: return None.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_URL = "https://steamspy.com/api.php"
_RATE_LOCK = asyncio.Lock()
_last_request_at: float = 0.0


@dataclass
class SteamSpyData:
    """Combined SteamSpy response: tags + playtime stats."""
    user_tags: list = field(default_factory=list)  # [(tag_name, vote_count), ...] top 10 desc
    median_forever: Optional[int] = None           # minutes; None if missing or zero
    average_forever: Optional[int] = None          # minutes; None if missing or zero


async def fetch_steamspy_data(
    client: httpx.AsyncClient,
    appid: int,
) -> Optional[SteamSpyData]:
    """Return combined SteamSpy data, or None on any failure.

    Renamed from `fetch_user_tags` (which returned only tags) so callers
    can also access median_forever / average_forever for the hook-point
    `playtime_median_avg_ratio` metric. Single API call gives all three —
    the previous fetcher was discarding the playtime fields. Caller
    should treat None / empty as "data unavailable" (not an error).
    """
    await _rate_limit()

    backoff = 1.0
    for attempt in range(3):
        try:
            resp = await client.get(
                _URL,
                params={"request": "appdetails", "appid": appid},
                timeout=15,
            )
        except httpx.RequestError as exc:
            log.warning("SteamSpy request error for %d: %s", appid, exc)
            return None

        if resp.status_code == 429:
            log.warning("SteamSpy 429 for %d, backing off %.0fs", appid, backoff)
            await asyncio.sleep(backoff)
            backoff *= 2
            await _rate_limit()
            continue

        if resp.status_code == 404:
            log.debug("SteamSpy 404 for %d", appid)
            return None

        if not resp.is_success:
            log.warning("SteamSpy HTTP %d for %d", resp.status_code, appid)
            return None

        try:
            data = resp.json()
        except Exception as exc:
            log.warning("SteamSpy JSON parse error for %d: %s", appid, exc)
            return None

        tags_raw: dict = data.get("tags") or {}
        sorted_tags = sorted(tags_raw.items(), key=lambda kv: kv[1], reverse=True)[:10]

        # Zero / missing → None so downstream callers can short-circuit.
        median = data.get("median_forever") or None
        average = data.get("average_forever") or None
        try:
            median_min = int(median) if median else None
        except (TypeError, ValueError):
            median_min = None
        try:
            average_min = int(average) if average else None
        except (TypeError, ValueError):
            average_min = None

        return SteamSpyData(
            user_tags=sorted_tags,
            median_forever=median_min,
            average_forever=average_min,
        )

    log.warning("SteamSpy gave up after retries for %d", appid)
    return None


async def _rate_limit() -> None:
    """Enforce maximum 1 request/second globally across all callers."""
    global _last_request_at
    async with _RATE_LOCK:
        now = time.monotonic()
        wait = 1.0 - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()
