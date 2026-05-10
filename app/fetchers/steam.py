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

from app.credentials import get_steam_api_key, get_steam_id

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
_VANITY_URL = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"

# Steam store API throttles aggressively; 1.5 s between detail calls is safe.
_STORE_DELAY = 1.5


async def resolve_vanity_url(
    client: httpx.AsyncClient,
    api_key: str,
    vanity: str,
) -> Optional[str]:
    """Resolve a Steam vanity URL ('username' from steamcommunity.com/id/username)
    to a numeric SteamID via ISteamUser/ResolveVanityURL.

    Returns the SteamID string on success, or None when:
      - The vanity name doesn't resolve (success=42 in Steam's response)
      - The API call fails (network error, bad API key, etc.)

    Used by the v4 setup wizard's SteamID page so users can paste either
    a numeric SteamID (no resolution needed) or a vanity URL / bare
    username. The wizard handles URL parsing (stripping the
    steamcommunity.com/id/ prefix) before calling this function."""
    if not vanity or not api_key:
        return None
    try:
        resp = await client.get(_VANITY_URL, params={
            "key": api_key,
            "vanityurl": vanity,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except httpx.RequestError:
        return None
    except httpx.HTTPStatusError:
        return None
    except ValueError:
        return None
    response = data.get("response") or {}
    # Steam returns success=1 + steamid on hit, success=42 + message on miss.
    if response.get("success") != 1:
        return None
    return response.get("steamid")


async def fetch_owned_games(
    client: httpx.AsyncClient,
    api_key: Optional[str] = None,
    steam_id: Optional[str] = None,
) -> list[dict]:
    """Return list of {appid, name, playtime_forever, rtime_last_played}.

    api_key / steam_id explicit overrides exist for the v4 setup wizard's
    validation step (it tests credentials BEFORE persisting them, so the
    credentials module accessors don't have the values yet). Defaults
    pull from credentials accessors for normal sync operation."""
    key = api_key or get_steam_api_key()
    sid = steam_id or get_steam_id()
    resp = await client.get(_OWNED_URL, params={
        "key": key,
        "steamid": sid,
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
    description, app_type (and release_date when parseable).

    app_type comes straight from Steam's `data.type` field — values include
    "game", "dlc", "demo", "music", "video", "advertising", "mod", etc.
    Used by app/game_type.classify_game to identify expansions and
    software/utility apps.
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

    app_type = (info.get("type") or "").strip().lower() or None

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
    if app_type is not None:
        out["app_type"] = app_type
    # Surface the coming_soon flag — passed to classify_game alongside
    # playtime to detect Early Access games whose Steam release date is
    # still "coming soon" but the user has access. Not a DB column;
    # sync.py pops this key out of updates before the Game-construction
    # merge so it never tries to land in the schema.
    if release.get("coming_soon"):
        out["coming_soon"] = True
    return out


async def fetch_review_data(client: httpx.AsyncClient, appid: int) -> Optional[dict]:
    """Return {steam_review_pct, steam_review_count, playtimes} or None.

    Renamed from `fetch_review_summary` (which only returned the
    aggregate fields) so callers can also access per-review playtime
    data for the v3 hook-point Phase 1a `review_playtime_median` and
    `stickiness_ratio` metrics.

    Single API call with num_per_page=100 returns one page of reviews
    (max 100 entries) along with the query_summary the prior version
    used. Median converges quickly over a 100-review sample even for
    games with tens of thousands of reviews; pagination would be
    wasteful. filter=all ensures the median reflects the population of
    reviewers who played and rated, not just recent reviews.

    `playtimes` is a list of int minutes (`author.playtime_at_review`)
    extracted from the returned reviews. May be empty when the game
    has no reviews or when the response shape is unexpected.
    """
    try:
        resp = await client.get(
            _REVIEWS_URL.format(appid=appid),
            params={
                "json": 1,
                "num_per_page": 100,
                "filter": "all",
                "language": "all",
            },
            timeout=20,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("reviews failed for %s: %s", appid, exc)
        return None

    body = resp.json()
    summary = body.get("query_summary", {})
    total = summary.get("total_reviews", 0)
    positive = summary.get("total_positive", 0)

    reviews_raw = body.get("reviews") or []
    playtimes: list = []
    for r in reviews_raw:
        author = r.get("author") or {}
        pt = author.get("playtime_at_review")
        if isinstance(pt, (int, float)) and pt >= 0:
            playtimes.append(int(pt))

    if total == 0:
        return {"steam_review_pct": None, "steam_review_count": 0, "playtimes": playtimes}
    return {
        "steam_review_pct": round(positive / total * 100),
        "steam_review_count": total,
        "playtimes": playtimes,
    }


def parse_last_played(rtime: Optional[int]) -> Optional[datetime]:
    """Convert Steam's Unix rtime_last_played to datetime, None if zero."""
    if not rtime:
        return None
    return datetime.utcfromtimestamp(rtime)
