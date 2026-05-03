"""
HowLongToBeat wrapper.

howlongtobeatpy does its own (sync) HTTP calls, so we run it in a thread
executor to avoid blocking the async event loop.

Matching strategy (per v2.5 spec):
  1. Search the raw Steam title.
  2. If 0 results, search a cleaned title (™/® stripped, trailing edition
     suffix removed, "(YYYY)" stripped, whitespace collapsed).
  3. If allow_backoff_retry=True (caller has detected a batch-wide failure
     pattern), wait 5 s and retry the cleaned title once more.

Matches below MIN_SIMILARITY are treated as misses to avoid wrong games
being grafted onto the wrong row.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from howlongtobeatpy import HowLongToBeat, HowLongToBeatEntry

log = logging.getLogger(__name__)

MIN_SIMILARITY = 0.5
BACKOFF_WAIT_SECONDS = 5.0


# Trailing edition / version suffixes — stripped from the cleaned query when
# the raw query has already failed. Listed longest-first so e.g. "Game of the
# Year Edition" matches before "Edition". Match is case-insensitive (set on
# the regex), so duplicate-cased entries aren't needed.
_EDITION_SUFFIXES = [
    "Game of the Year Edition",
    "Anniversary Edition",
    "Definitive Edition",
    "Enhanced Edition",
    "Complete Edition",
    "Ultimate Edition",
    "Premium Edition",
    "Standard Edition",
    "Special Edition",
    "Reloaded Edition",
    "Deluxe Edition",
    "Gold Edition",
    "GOTY Edition",
    "The Original Classic",
    "Original Classic",
    "GOTY",
    "Remastered",
    "Remaster",
    "Legacy",
]
_TRADEMARK_RE = re.compile(r"[™®©]")
_YEAR_PARENS_RE = re.compile(r"\s*\(\d{4}\)\s*$")
_EDITION_RE = re.compile(
    r"\s*[-:–—]?\s*(?:" + "|".join(re.escape(s) for s in _EDITION_SUFFIXES) + r")\s*$",
    re.IGNORECASE,
)
# Trailing dash + optional function word (after edition strip). Catches
# residues like "Nioh 2 – The" where the suffix word ("Complete Edition")
# was already stripped, leaving the connector.
_DANGLING_TRAILING_RE = re.compile(
    r"\s*[-–—]\s*(?:The|A|An|Of)?\s*$",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def clean_title(name: str) -> str:
    """Normalize a Steam title for HLTB lookup.

    Pipeline:
      1. Replace ™/®/© with a space (not empty), so ``ACE COMBAT™7``
         becomes ``ACE COMBAT 7`` rather than ``ACE COMBAT7``.
      2. Strip trailing ``(YYYY)``.
      3. Loop edition-suffix and dangling-trailing strips until stable so
         compound cases (``Foo - The Complete Edition`` →
         strip ``Complete Edition`` → ``Foo - The`` → strip ``- The``) and
         repeated suffixes (``Foo GOTY Edition Remastered``) collapse in
         one call.
      4. Collapse repeated whitespace and trim.

    Internal hyphens within compound words (``Rain-Slick``) are preserved —
    the dangling-trailing regex is anchored to the end of the string with
    surrounding-whitespace required, so mid-word hyphens never match.
    """
    cur = _TRADEMARK_RE.sub(" ", name)
    prev = None
    # Year + edition + dangling-trailing all run in the same loop. Year
    # strip is anchored to end-of-string, so it must re-run after an
    # edition strip exposes the year as trailing (e.g. "Foo (2024) GOTY
    # Edition" → strip GOTY → strip year → "Foo").
    while prev != cur:
        prev = cur
        cur = _YEAR_PARENS_RE.sub("", cur)
        cur = _EDITION_RE.sub("", cur)
        cur = _DANGLING_TRAILING_RE.sub("", cur)
    cur = _WHITESPACE_RE.sub(" ", cur).strip()
    return cur


@dataclass
class HltbResult:
    """Outcome of one fetch_hltb call. ``found`` distinguishes a real match
    (≥ MIN_SIMILARITY) from a miss; ``queries_tried`` records each query string
    that was actually sent so the caller can write a paper-trail miss log."""

    found: bool
    hltb_main_hours: Optional[float] = None
    hltb_main_extra_hours: Optional[float] = None
    hltb_completionist_hours: Optional[float] = None
    matched_name: Optional[str] = None
    similarity: Optional[float] = None
    queries_tried: list[str] = field(default_factory=list)
    backoff_used: bool = False


async def fetch_hltb(name: str, allow_backoff_retry: bool = False) -> HltbResult:
    """
    Look up HLTB main/main+extra/completionist hours for ``name``. Returns
    HltbResult; the caller checks ``.found`` and reads either the duration
    fields or ``queries_tried`` to log the miss.

    ``allow_backoff_retry`` enables one retry-with-wait of the cleaned query
    after both the raw and cleaned attempts come back empty — only meaningful
    when the caller has detected a batch-wide failure pattern.
    """
    queries_tried: list[str] = []
    queries: list[str] = [name]
    cleaned = clean_title(name)
    if cleaned and cleaned.casefold() != name.casefold():
        queries.append(cleaned)

    # Attempts 1 (raw) and 2 (cleaned, when different from raw).
    for q in queries:
        queries_tried.append(q)
        result = await _try_query(q, name)
        if result.found:
            result.queries_tried = queries_tried
            return result

    # Attempt 3 — backoff retry of the cleaned query (or raw if cleaning was
    # a no-op) when the caller hints that the failure may be transient.
    if allow_backoff_retry:
        retry_query = cleaned if cleaned else name
        log.info(
            "HLTB: backoff retry for %r after %.1fs (queries so far: %s)",
            name, BACKOFF_WAIT_SECONDS, queries_tried,
        )
        await asyncio.sleep(BACKOFF_WAIT_SECONDS)
        queries_tried.append(retry_query)
        result = await _try_query(retry_query, name)
        result.backoff_used = True
        if result.found:
            result.queries_tried = queries_tried
            return result

    return HltbResult(found=False, queries_tried=queries_tried, backoff_used=allow_backoff_retry)


async def _try_query(query: str, original: str) -> HltbResult:
    """Run one HLTB search and convert it into an HltbResult."""
    try:
        results: list[HowLongToBeatEntry] = await asyncio.get_running_loop().run_in_executor(
            None, _search_sync, query,
        )
    except Exception as exc:
        log.warning("HLTB lookup failed for %r (query=%r): %s", original, query, exc)
        return HltbResult(found=False)

    if not results:
        log.debug("HLTB: 0 results for query %r (original %r)", query, original)
        return HltbResult(found=False)

    best = max(results, key=lambda r: r.similarity)
    if best.similarity < MIN_SIMILARITY:
        log.info(
            "HLTB: best match for %r is %r (sim %.2f) — below %.2f threshold, treating as miss",
            original, best.game_name, best.similarity, MIN_SIMILARITY,
        )
        return HltbResult(found=False)

    log.debug(
        "HLTB: matched %r → %r (sim %.2f) via query %r",
        original, best.game_name, best.similarity, query,
    )
    return HltbResult(
        found=True,
        hltb_main_hours=_hours(best.main_story),
        hltb_main_extra_hours=_hours(best.main_extra),
        hltb_completionist_hours=_hours(best.completionist),
        matched_name=best.game_name,
        similarity=best.similarity,
    )


def _search_sync(name: str) -> list[HowLongToBeatEntry]:
    return HowLongToBeat().search(name, similarity_case_sensitive=False) or []


def _hours(value) -> Optional[float]:
    """Convert HLTB time value to float hours, None if missing/zero."""
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None
