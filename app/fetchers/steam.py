"""
Steam API wrappers.

Three endpoints used:
  - IPlayerService/GetOwnedGames — full library list with playtime
  - ISteamApps/appdetails        — genres, tags, Metacritic, developer, publisher,
                                   release date, short_description
  - appreviews/{appid}           — positive review percentage + count
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Optional

import httpx

from app.config import STEAM_API_KEY, STEAM_ID

log = logging.getLogger(__name__)


# Steam returns release_date.date as a localised string. English locale only
# emits a handful of stable formats; everything else (e.g. "Coming Soon",
# "TBA", "Q4 2024") parses to None and the caller treats the field as absent.
_RELEASE_DATE_FORMATS = (
    "%d %b, %Y",   # "21 Aug, 2024"
    "%b %d, %Y",   # "Aug 21, 2024"
    "%d %B, %Y",   # "21 August, 2024"
    "%B %d, %Y",   # "August 21, 2024"
    "%Y-%m-%d",
)
_YEAR_ONLY_RE = re.compile(r"^\s*(\d{4})\s*$")


def parse_release_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str or not isinstance(date_str, str):
        return None
    s = date_str.strip()
    for fmt in _RELEASE_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    m = _YEAR_ONLY_RE.match(s)
    if m:
        try:
            return datetime(int(m.group(1)), 1, 1)
        except ValueError:
            return None
    return None

_OWNED_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
_REVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"

# Steam store API throttles aggressively; 1.5 s between detail calls is safe.
_STORE_DELAY = 1.5


async def fetch_owned_games(client: httpx.AsyncClient) -> list[dict]:
    """Return list of {appid, name, playtime_forever, rtime_last_played}."""
    resp = await client.get(_OWNED_URL, params={
        "key": STEAM_API_KEY,
        "steamid": STEAM_ID,
        "include_appinfo": 1,
        "include_played_free_games": 1,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("response", {}).get("games", [])


async def fetch_app_details(client: httpx.AsyncClient, appid: int) -> Optional[dict]:
    """
    Return store details for one appid, or None on failure.
    Returned dict has keys: genres, tags, metacritic_score, developer, publisher,
    description (and release_date when parseable).
    """
    await asyncio.sleep(_STORE_DELAY)
    try:
        # ``basic`` is required for short_description; it bundles a handful of
        # other fields we ignore. The request count is unchanged.
        resp = await client.get(_DETAILS_URL, params={
            "appids": appid,
            "filters": "basic,genres,categories,metacritic,developers,publishers,release_date",
            "l": "english",
        }, timeout=20)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("appdetails failed for %s: %s", appid, exc)
        return None

    data = resp.json().get(str(appid), {})
    if not data.get("success"):
        return None

    info = data.get("data", {})

    genres = ",".join(g["description"] for g in info.get("genres", []))

    # Top 10 user-defined tags come from a separate undocumented endpoint embedded
    # in the store page; appdetails doesn't include them. Use categories as a proxy
    # for now — they're the closest structured tag-like field available in appdetails.
    tags = ",".join(
        c["description"] for c in (info.get("categories") or [])[:10]
    )

    metacritic = info.get("metacritic", {})
    metacritic_score = metacritic.get("score") if metacritic else None

    developers = info.get("developers") or []
    publishers = info.get("publishers") or []

    release = info.get("release_date") or {}
    release_dt = parse_release_date(release.get("date"))

    short_desc = (info.get("short_description") or "").strip() or None

    out = {
        "genres": genres,
        "tags": tags,
        "metacritic_score": metacritic_score,
        "developer": developers[0] if developers else None,
        "publisher": publishers[0] if publishers else None,
    }
    # Only emit release_date when it parses cleanly — skipping the key keeps
    # the merge in sync.py from overwriting an existing good value with None.
    if release_dt is not None:
        out["release_date"] = release_dt
    if short_desc is not None:
        out["description"] = short_desc
    return out


async def fetch_review_summary(client: httpx.AsyncClient, appid: int) -> Optional[dict]:
    """Return {steam_review_pct, steam_review_count} or None on failure."""
    try:
        resp = await client.get(
            _REVIEWS_URL.format(appid=appid),
            params={"json": 1, "num_per_page": 0, "language": "all"},
            timeout=15,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("reviews failed for %s: %s", appid, exc)
        return None

    summary = resp.json().get("query_summary", {})
    total = summary.get("total_reviews", 0)
    positive = summary.get("total_positive", 0)
    if total == 0:
        return {"steam_review_pct": None, "steam_review_count": 0}
    return {
        "steam_review_pct": round(positive / total * 100),
        "steam_review_count": total,
    }


def parse_last_played(rtime: Optional[int]) -> Optional[datetime]:
    """Convert Steam's Unix rtime_last_played to datetime, None if zero."""
    if not rtime:
        return None
    return datetime.utcfromtimestamp(rtime)
