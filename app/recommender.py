"""
Recommendation engine — v1.

Four modes: short_term, long_term, both, surprise.

SHORT-TERM mode (fits tonight's time window)
--------------------------------------------
Goal: pick games you can meaningfully play within the time available.

1. Filter by user toggles (include_unplayed, include_in_progress).
2. Estimate remaining time per game:
     - in_progress + hours_played_manual set → max(hltb_main_hours - hours_played_manual, 0.5)
     - in_progress, no manual hours           → hltb_main_hours  (full estimate, UI hints user)
     - anything else                          → hltb_main_hours
   Games with no HLTB data are excluded — we can't estimate fit.
3. Filter to games where remaining_hours fits within ±50% of the requested window.
4. Score each candidate:
     +2  in_progress
     +1  not played in 30+ days (or never played)
     +1  high quality: Metacritic ≥ 85 OR OpenCritic ≥ 85 OR Steam % ≥ 90 with ≥ 1000 reviews
5. Iterative variety selection (prevents all picks sharing the same genre):
     - Pick the highest-scored candidate, add to results.
     - Apply -1 to every remaining candidate sharing its primary genre.
     - Repeat until 5 selected or candidates exhausted.

LONG-TERM mode (worth committing to across sessions)
-----------------------------------------------------
Goal: find a quality game worth investing hours in over multiple evenings.

1. Same toggle filtering.
2. Require hltb_main_hours ≥ 8.
3. Score each candidate:
     +3  Metacritic ≥ 85 OR OpenCritic ≥ 85
     +2  Steam % ≥ 90 with ≥ 1000 reviews
     +1  in_progress (continuation bias)
     -2  dropped (don't re-suggest games I bounced off)
4. Sort descending, take top 5 (no iterative variety step for long-term).

BOTH mode (default)
-------------------
Goal: return a balanced mix of tonight-sized and long-commitment games.

Targets 3 short-term picks and 2 long-term picks.
If either category comes up short, the other fills the remaining slots.
Duplicate games (eligible in both pools) are counted only once — whichever
pool surfaces them first keeps the slot.
Each candidate carries a source field ("tonight" or "long_term") so the
UI can badge them accordingly.

SURPRISE mode
-------------
Goal: surface something unexpected from the full eligible library.

No time-window filtering — the whole eligible library is the pool.
Weighted random selection per pick:
  50% chance: draw from high-quality games (Metacritic/OpenCritic ≥ 85 or Steam ≥ 90% / ≥ 1000)
  30% chance: draw from never-played games regardless of score
  20% chance: draw from anything eligible
If the weighted bucket is empty, falls back to the full eligible pool.
After each pick, the game's primary genre is excluded from subsequent picks
to enforce the same variety rule used by the other modes.
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from app.models import GameStatus, GameWithState


class RecommendMode(str, Enum):
    short_term = "short_term"
    long_term = "long_term"
    both = "both"
    surprise = "surprise"


@dataclass
class RecommendRequest:
    minutes: int                              # tonight's available time
    mode: RecommendMode
    include_unplayed: bool = True
    include_in_progress: bool = True
    installed_only: bool = False              # always False in v1 (data not available)
    excluded_ids: frozenset = field(default_factory=frozenset)


@dataclass
class Candidate:
    gws: GameWithState
    remaining_hours: float
    score: float = 0.0
    no_manual_hours_hint: bool = False        # show "using full estimate" hint on the card
    source: Optional[str] = None             # "tonight", "long_term", "surprise", or None


def recommend(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int = 5,
) -> list[Candidate]:
    # Apply exclusions once here so no sub-function needs to know about them.
    if req.excluded_ids:
        games = [g for g in games if g.game.appid not in req.excluded_ids]

    if req.mode == RecommendMode.short_term:
        return _short_term(games, req, max_results)
    if req.mode == RecommendMode.long_term:
        return _long_term(games, req, max_results)
    if req.mode == RecommendMode.surprise:
        return _surprise(games, req, max_results)
    return _both(games, req, max_results)


# ---------------------------------------------------------------------------
# Short-term
# ---------------------------------------------------------------------------

def _short_term(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int,
) -> list[Candidate]:
    window_hours = req.minutes / 60.0

    candidates = []
    for gws in games:
        if not _passes_toggles(gws, req):
            continue

        remaining, hint = _remaining_hours(gws)
        if remaining is None:
            continue  # no HLTB data — can't estimate fit

        # ±50% of requested window
        lo = window_hours * 0.5
        hi = window_hours * 1.5
        if not (lo <= remaining <= hi):
            continue

        c = Candidate(gws=gws, remaining_hours=remaining, no_manual_hours_hint=hint)
        c.score = _short_term_score(gws)
        candidates.append(c)

    return _iterative_variety_select(candidates, max_results)


def _short_term_score(gws: GameWithState) -> float:
    score = 0.0
    game, state = gws.game, gws.state

    if state.status == GameStatus.in_progress:
        score += 2

    if game.last_played_steam is None:
        score += 1
    elif game.last_played_steam < datetime.utcnow() - timedelta(days=30):
        score += 1

    if _is_high_quality(game):
        score += 1

    return score


def _iterative_variety_select(
    candidates: list[Candidate],
    max_results: int,
) -> list[Candidate]:
    """
    Pick greedily by score, then penalise remaining candidates that share the
    primary genre of each pick. This produces actual variety in the final list
    rather than just nudging scores.
    """
    pool = [Candidate(
        gws=c.gws,
        remaining_hours=c.remaining_hours,
        score=c.score,
        no_manual_hours_hint=c.no_manual_hours_hint,
        source=c.source,
    ) for c in candidates]

    selected: list[Candidate] = []
    while pool and len(selected) < max_results:
        pool.sort(key=lambda c: c.score, reverse=True)
        pick = pool.pop(0)
        selected.append(pick)

        picked_genre = pick.gws.game.primary_genre()
        if picked_genre:
            for remaining in pool:
                if remaining.gws.game.primary_genre() == picked_genre:
                    remaining.score -= 1

    return selected


# ---------------------------------------------------------------------------
# Long-term
# ---------------------------------------------------------------------------

def _long_term(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int,
) -> list[Candidate]:
    candidates = []
    for gws in games:
        if not _passes_toggles(gws, req):
            continue

        hltb = gws.game.hltb_main_hours
        if hltb is None or hltb < 8:
            continue

        c = Candidate(gws=gws, remaining_hours=hltb)
        c.score = _long_term_score(gws)
        candidates.append(c)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:max_results]


def _long_term_score(gws: GameWithState) -> float:
    score = 0.0
    game, state = gws.game, gws.state

    if _is_critically_acclaimed(game):
        score += 3

    if (
        game.steam_review_pct is not None
        and game.steam_review_pct >= 90
        and (game.steam_review_count or 0) >= 1000
    ):
        score += 2

    if state.status == GameStatus.in_progress:
        score += 1

    if state.status == GameStatus.dropped:
        score -= 2

    return score


# ---------------------------------------------------------------------------
# Both
# ---------------------------------------------------------------------------

def _both(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int = 5,
) -> list[Candidate]:
    """
    Blend short-term and long-term picks.
    Target allocation: 3 short ("tonight"), 2 long ("long_term").
    If either pool is short, the other fills the gap up to max_results.
    A game that appears in both pools is counted only once (short wins).
    """
    target_short = 3
    target_long = 2

    # Request the full pool from each so we have overflow to fill gaps.
    short_pool = _short_term(games, req, max_results=max_results)
    long_pool = _long_term(games, req, max_results=max_results)

    selected: list[Candidate] = []
    picked_ids: set[int] = set()

    # Fill up to target_short from the short pool.
    for c in short_pool:
        if len(selected) >= target_short:
            break
        c.source = "tonight"
        selected.append(c)
        picked_ids.add(c.gws.game.appid)

    # Fill up to target_long from the long pool, skipping duplicates.
    long_filled = 0
    for c in long_pool:
        if long_filled >= target_long:
            break
        if c.gws.game.appid not in picked_ids:
            c.source = "long_term"
            selected.append(c)
            picked_ids.add(c.gws.game.appid)
            long_filled += 1

    # Fill any remaining slots — long first (compensates for short shortage),
    # then short (compensates for long shortage or overall scarcity).
    for c in long_pool:
        if len(selected) >= max_results:
            break
        if c.gws.game.appid not in picked_ids:
            c.source = "long_term"
            selected.append(c)
            picked_ids.add(c.gws.game.appid)

    for c in short_pool:
        if len(selected) >= max_results:
            break
        if c.gws.game.appid not in picked_ids:
            c.source = "tonight"
            selected.append(c)
            picked_ids.add(c.gws.game.appid)

    return selected


# ---------------------------------------------------------------------------
# Surprise
# ---------------------------------------------------------------------------

def _surprise(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int = 5,
) -> list[Candidate]:
    """
    Weighted random selection from the full eligible library.

    For each pick, randomly choose a bucket then sample uniformly from it:
      50%  top-quality bucket  (Metacritic/OpenCritic ≥ 85, or Steam ≥ 90% / ≥ 1000)
      30%  never-played bucket (ignores score — surfaces forgotten library entries)
      20%  full eligible pool

    If the chosen bucket is empty, falls back to the full eligible pool.
    After each pick, the game's primary genre is excluded from all remaining
    picks (same variety enforcement as the scored modes).
    """
    eligible = [gws for gws in games if _passes_toggles(gws, req)]
    if not eligible:
        return []

    # Pre-compute quality and never-played subsets (rebuilt per pick below
    # to exclude already-picked games and genres).
    all_quality = [gws for gws in eligible if _is_high_quality(gws.game)]
    all_unplayed = [gws for gws in eligible if gws.state.status == GameStatus.never_played]

    selected: list[Candidate] = []
    picked_ids: set[int] = set()
    excluded_genres: set[str] = set()

    for _ in range(max_results):
        def _available(pool: list[GameWithState]) -> list[GameWithState]:
            return [
                gws for gws in pool
                if gws.game.appid not in picked_ids
                and (
                    gws.game.primary_genre() is None
                    or gws.game.primary_genre() not in excluded_genres
                )
            ]

        avail_all = _available(eligible)
        if not avail_all:
            break

        avail_quality = _available(all_quality)
        avail_unplayed = _available(all_unplayed)

        roll = random.random()
        if roll < 0.50 and avail_quality:
            pool = avail_quality
        elif roll < 0.80 and avail_unplayed:
            pool = avail_unplayed
        else:
            pool = avail_all

        pick_gws = random.choice(pool)
        remaining = pick_gws.game.hltb_main_hours or 0.0

        c = Candidate(gws=pick_gws, remaining_hours=remaining, source="surprise")
        selected.append(c)
        picked_ids.add(pick_gws.game.appid)

        genre = pick_gws.game.primary_genre()
        if genre:
            excluded_genres.add(genre)

    return selected


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _passes_toggles(gws: GameWithState, req: RecommendRequest) -> bool:
    state = gws.state

    # Always exclude not_interested and finished.
    if state.status in (GameStatus.not_interested, GameStatus.finished):
        return False

    if state.status == GameStatus.never_played and not req.include_unplayed:
        return False

    if state.status == GameStatus.in_progress and not req.include_in_progress:
        return False

    # installed_only is disabled in v1 (data not available)

    return True


def _remaining_hours(gws: GameWithState) -> tuple[Optional[float], bool]:
    """
    Returns (remaining_hours, no_manual_hours_hint).
    no_manual_hours_hint is True when we fell back to full HLTB estimate for
    an in_progress game — the UI surfaces this so the user knows to add hours.
    """
    game, state = gws.game, gws.state
    hltb = game.hltb_main_hours

    if hltb is None:
        return None, False

    if state.status == GameStatus.in_progress and state.hours_played_manual is not None:
        remaining = max(hltb - state.hours_played_manual, 0.5)
        return remaining, False

    if state.status == GameStatus.in_progress:
        return hltb, True

    return hltb, False


def _is_high_quality(game) -> bool:
    """Metacritic OR OpenCritic ≥ 85, or Steam ≥ 90% with ≥ 1000 reviews."""
    if game.metacritic_score is not None and game.metacritic_score >= 85:
        return True
    if game.opencritic_score is not None and game.opencritic_score >= 85:
        return True
    if (
        game.steam_review_pct is not None
        and game.steam_review_pct >= 90
        and (game.steam_review_count or 0) >= 1000
    ):
        return True
    return False


def _is_critically_acclaimed(game) -> bool:
    """Long-term quality signal: Metacritic OR OpenCritic ≥ 85."""
    if game.metacritic_score is not None and game.metacritic_score >= 85:
        return True
    if game.opencritic_score is not None and game.opencritic_score >= 85:
        return True
    return False
