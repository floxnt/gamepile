"""
HowLongToBeat wrapper.

howlongtobeatpy does its own HTTP calls (sync), so we run it in a thread
executor to avoid blocking the async event loop.
"""

import asyncio
import logging
from typing import Optional

from howlongtobeatpy import HowLongToBeat, HowLongToBeatEntry

log = logging.getLogger(__name__)


async def fetch_hltb(name: str) -> Optional[dict]:
    """
    Return {hltb_main_hours, hltb_main_extra_hours, hltb_completionist_hours}
    for the best-matching game, or None if no match found.
    """
    loop = asyncio.get_running_loop()
    try:
        results: list[HowLongToBeatEntry] = await loop.run_in_executor(
            None, _search_sync, name
        )
    except Exception as exc:
        log.warning("HLTB lookup failed for %r: %s", name, exc)
        return None

    if not results:
        log.debug("HLTB: no results for %r", name)
        return None

    best = max(results, key=lambda r: r.similarity)
    log.debug("HLTB: matched %r -> %r (similarity %.2f)", name, best.game_name, best.similarity)

    return {
        "hltb_main_hours": _hours(best.main_story),
        "hltb_main_extra_hours": _hours(best.main_extra),
        "hltb_completionist_hours": _hours(best.completionist),
    }


def _search_sync(name: str) -> list[HowLongToBeatEntry]:
    return HowLongToBeat().search(name, similarity_case_sensitive=False) or []


def _hours(value) -> Optional[float]:
    """Convert HLTB time value to float hours, None if missing/zero."""
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None
