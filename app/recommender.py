"""
Recommendation engine — v1.

Two modes: short_term and long_term.

SHORT-TERM mode (fits tonight's time window)
--------------------------------------------
Goal: pick games you can meaningfully play within the time available.

1. Filter by user toggles (include_unplayed, include_in_progress, installed_only).
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
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from app.models import GameStatus, GameWithState


class RecommendMode(str, Enum):
    short_term = "short_term"
    long_term = "long_term"


@dataclass
class RecommendRequest:
    minutes: int                    # tonight's available time
    mode: RecommendMode
    include_unplayed: bool = True
    include_in_progress: bool = True
    installed_only: bool = False    # always False in v1 (data not available)


@dataclass
class Candidate:
    gws: GameWithState
    remaining_hours: float
    score: float = 0.0
    no_manual_hours_hint: bool = False  # show "using full estimate" hint


def recommend(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int = 5,
) -> list[Candidate]:
    if req.mode == RecommendMode.short_term:
        return _short_term(games, req, max_results)
    return _long_term(games, req, max_results)


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

    # Not played recently (or never)
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
    # Work on a mutable copy so we don't modify caller's scores
    pool = [Candidate(
        gws=c.gws,
        remaining_hours=c.remaining_hours,
        score=c.score,
        no_manual_hours_hint=c.no_manual_hours_hint,
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
# Shared helpers
# ---------------------------------------------------------------------------

def _passes_toggles(gws: GameWithState, req: RecommendRequest) -> bool:
    state = gws.state

    # Always exclude not_interested and finished
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
        # No manual hours — use full estimate and flag it
        return hltb, True

    return hltb, False


def _is_high_quality(game) -> bool:
    """Short-term quality signal: Metacritic OR OpenCritic ≥ 85, or very positive Steam."""
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
