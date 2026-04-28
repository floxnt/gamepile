"""
OpenCritic fetcher.

Search strategy per spec:
  1. Exact name match (case-insensitive)
  2. Fuzzy match via difflib.SequenceMatcher — accept ratio >= 0.85
  3. Log the miss and return None

Two endpoints:
  GET /api/game/search?criteria=<name>  — returns list of {id, name, ...}
  GET /api/game/<id>                    — returns full game including topCriticScore
"""

import difflib
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_BASE = "https://api.opencritic.com/api"
_FUZZY_THRESHOLD = 0.85


async def fetch_opencritic_score(client: httpx.AsyncClient, name: str) -> Optional[int]:
    """Return the OpenCritic top-critic score (0-100) or None."""
    candidates = await _search(client, name)
    if not candidates:
        log.info("OpenCritic: no search results for %r", name)
        return None

    matched_id = _pick_best_match(name, candidates)
    if matched_id is None:
        log.info("OpenCritic: no confident match for %r (best candidates: %s)",
                 name, [c.get("name") for c in candidates[:3]])
        return None

    return await _fetch_score(client, matched_id)


async def _search(client: httpx.AsyncClient, name: str) -> list[dict]:
    try:
        resp = await client.get(
            f"{_BASE}/game/search",
            params={"criteria": name},
            headers={"User-Agent": "gamepile/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        log.warning("OpenCritic search failed for %r: %s", name, exc)
        return []


def _pick_best_match(name: str, candidates: list[dict]) -> Optional[int]:
    """
    Prefer exact case-insensitive match; fall back to highest SequenceMatcher
    ratio if it clears the threshold.
    """
    name_lower = name.lower()

    for c in candidates:
        if c.get("name", "").lower() == name_lower:
            return c["id"]

    best_ratio = 0.0
    best_id = None
    for c in candidates:
        ratio = difflib.SequenceMatcher(
            None, name_lower, c.get("name", "").lower()
        ).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = c["id"]

    if best_ratio >= _FUZZY_THRESHOLD:
        return best_id
    return None


async def _fetch_score(client: httpx.AsyncClient, game_id: int) -> Optional[int]:
    try:
        resp = await client.get(
            f"{_BASE}/game/{game_id}",
            headers={"User-Agent": "gamepile/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        score = data.get("topCriticScore")
        if score is not None and score >= 0:
            return round(score)
        return None
    except httpx.HTTPError as exc:
        log.warning("OpenCritic game fetch failed for id %s: %s", game_id, exc)
        return None
