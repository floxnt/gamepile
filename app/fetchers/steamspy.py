"""
SteamSpy fetcher.

Returns user-applied tags (name → vote count) for a game, sorted by vote
count descending. Used to populate games.user_tags (top 10).

Rate limit: 1 request/second enforced via an asyncio lock + sleep.
On 429: exponential backoff (1s, 2s, 4s) up to 3 retries, then empty list.
On 404 / no data: return [], log, don't raise.
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_URL = "https://steamspy.com/api.php"
_RATE_LOCK = asyncio.Lock()
_last_request_at: float = 0.0


async def fetch_user_tags(
    client: httpx.AsyncClient,
    appid: int,
) -> list[tuple[str, int]]:
    """
    Return [(tag_name, vote_count), ...] sorted by vote count desc, top 10.
    Returns [] on any failure.
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
            return []

        if resp.status_code == 429:
            log.warning("SteamSpy 429 for %d, backing off %.0fs", appid, backoff)
            await asyncio.sleep(backoff)
            backoff *= 2
            await _rate_limit()
            continue

        if resp.status_code == 404:
            log.debug("SteamSpy 404 for %d", appid)
            return []

        if not resp.is_success:
            log.warning("SteamSpy HTTP %d for %d", resp.status_code, appid)
            return []

        try:
            data = resp.json()
        except Exception as exc:
            log.warning("SteamSpy JSON parse error for %d: %s", appid, exc)
            return []

        tags: dict = data.get("tags") or {}
        if not tags:
            return []

        sorted_tags = sorted(tags.items(), key=lambda kv: kv[1], reverse=True)
        return sorted_tags[:10]

    log.warning("SteamSpy gave up after retries for %d", appid)
    return []


async def _rate_limit() -> None:
    """Enforce maximum 1 request/second globally across all callers."""
    global _last_request_at
    async with _RATE_LOCK:
        now = time.monotonic()
        wait = 1.0 - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()
