"""
Recommendation engine — Shortlist (v2.5).

Five user-intent modes:

I_ONLY_HAVE_TONIGHT — fits tonight's time window (±50%).
CONTINUE_SOMETHING — surfaces in_progress games, weighted by closeness to completion.
COMFORT_PICK       — high-playtime games the user has clearly enjoyed; ignores time.
START_SOMETHING_NEW — never_played + effectively-untouched played_unclassified games
                      worth committing to (uses the long-form scoring).
SURPRISE_ME        — weighted random with quality bias.

Every Candidate carries:
  source    — mode value (set in all modes)
  reasons   — up to 2-3 human-readable strings explaining the pick
  warnings  — zero or more amber advisory strings shown on the card
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from app.models import GameStatus, GameWithState


class RecommendMode(str, Enum):
    i_only_have_tonight = "i_only_have_tonight"
    continue_something = "continue_something"
    comfort_pick = "comfort_pick"
    start_something_new = "start_something_new"
    surprise_me = "surprise_me"


# Map legacy/aliased mode strings to current values. Keeps existing bookmarks,
# pick_history.mode rows, and the deprecated "both" default working.
_LEGACY_MODE_ALIASES = {
    "both": RecommendMode.i_only_have_tonight.value,
    "short_term": RecommendMode.i_only_have_tonight.value,
    "long_term": RecommendMode.start_something_new.value,
    "surprise": RecommendMode.surprise_me.value,
}


def normalize_mode(raw: Optional[str]) -> Optional[str]:
    """Translate legacy mode strings to canonical values; return None if unknown."""
    if not raw:
        return None
    if raw in _LEGACY_MODE_ALIASES:
        return _LEGACY_MODE_ALIASES[raw]
    try:
        return RecommendMode(raw).value
    except ValueError:
        return None


@dataclass
class RecommendRequest:
    minutes: int                              # tonight's available time
    mode: RecommendMode
    include_unplayed: bool = True
    include_in_progress: bool = True
    installed_only: bool = False              # always False in v1 (data not available)
    excluded_ids: frozenset = field(default_factory=frozenset)
    # {(kind, value_lower): (weight, pick_count)} from db.get_all_affinities().
    affinities: dict = field(default_factory=dict)


@dataclass
class Candidate:
    gws: GameWithState
    remaining_hours: float
    score: float = 0.0
    no_manual_hours_hint: bool = False
    source: Optional[str] = None
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Default-mode resolution
# ---------------------------------------------------------------------------

def default_mode_for_library(games: list[GameWithState]) -> str:
    """
    Pick the best initial mode for the user's current library shape:
      in_progress games exist            → continue_something
      else comfort-eligible games exist  → comfort_pick
      else                               → i_only_have_tonight
    """
    eligible = [g for g in games if not _is_globally_excluded(g)]
    if any(g.state.status == GameStatus.in_progress for g in eligible):
        return RecommendMode.continue_something.value
    if _has_any_comfort_candidate(eligible):
        return RecommendMode.comfort_pick.value
    return RecommendMode.i_only_have_tonight.value


def recommend(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int = 5,
) -> list[Candidate]:
    # Universal exclusions across every mode: blacklisted, not_interested,
    # strongly-dropped, and the rolling try-again exclusion list.
    games = [g for g in games if not _is_globally_excluded(g)]
    if req.excluded_ids:
        games = [g for g in games if g.game.appid not in req.excluded_ids]

    if req.mode == RecommendMode.i_only_have_tonight:
        return _short_term(games, req, max_results)
    if req.mode == RecommendMode.continue_something:
        return _continue_something(games, req, max_results)
    if req.mode == RecommendMode.comfort_pick:
        return _comfort_pick(games, req, max_results)
    if req.mode == RecommendMode.start_something_new:
        return _start_something_new(games, req, max_results)
    if req.mode == RecommendMode.surprise_me:
        return _surprise(games, req, max_results)
    return []


def _is_globally_excluded(gws: GameWithState) -> bool:
    """Universal hard filters applied before any mode-specific eligibility."""
    state = gws.state
    if state.blacklisted:
        return True
    if state.status == GameStatus.not_interested:
        return True
    if state.status == GameStatus.dropped and state.dropped_strength == "strong":
        return True
    return False


# ---------------------------------------------------------------------------
# I only have tonight (short-term, time-windowed)
# ---------------------------------------------------------------------------

def _passes_short_term_toggles(gws: GameWithState, req: RecommendRequest) -> bool:
    state = gws.state
    if state.status == GameStatus.finished:
        return False
    if state.status == GameStatus.never_played and not req.include_unplayed:
        return False
    if state.status == GameStatus.in_progress and not req.include_in_progress:
        return False
    return True


def _short_term(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int,
) -> list[Candidate]:
    window_hours = req.minutes / 60.0
    lo, hi = window_hours * 0.5, window_hours * 1.5

    candidates = []
    for gws in games:
        if not _passes_short_term_toggles(gws, req):
            continue
        remaining, hint = _remaining_hours(gws)
        if remaining is None:
            continue
        if not (lo <= remaining <= hi):
            continue

        score, score_reasons = _score_short_term(gws)
        affinity_reasons, affinity_delta = _affinity_contribution(gws.game, req.affinities)
        score += affinity_delta
        all_reasons = affinity_reasons + score_reasons + [f"fits {req.minutes}min window"]
        c = Candidate(
            gws=gws,
            remaining_hours=remaining,
            score=score,
            no_manual_hours_hint=hint,
            source=RecommendMode.i_only_have_tonight.value,
            reasons=all_reasons[:3],
            warnings=_build_warnings(gws),
        )
        candidates.append(c)

    for c in candidates:
        c.score += random.uniform(-1.0, 1.0)

    return _iterative_variety_select(candidates, max_results)


def _score_short_term(gws: GameWithState) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    game, state = gws.game, gws.state
    now = datetime.utcnow()

    if state.status == GameStatus.in_progress:
        score += 2
        reasons.append("in progress")

    if game.last_played_steam is None:
        score += 1
        reasons.append("never played")
    elif game.last_played_steam < now - timedelta(days=30):
        days = (now - game.last_played_steam).days
        score += 1
        reasons.append(f"last played {days}d ago")

    q = _quality_str(game)
    if q:
        score += 1
        reasons.append(q)

    return score, reasons


# ---------------------------------------------------------------------------
# Continue something (in-progress only, near-completion bias)
# ---------------------------------------------------------------------------

def _continue_something(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int,
) -> list[Candidate]:
    eligible = [g for g in games if g.state.status == GameStatus.in_progress]

    candidates = []
    for gws in eligible:
        game = gws.game
        playtime_h = game.playtime_minutes / 60.0
        hltb = game.hltb_main_hours

        score = 0.0
        reasons: list[str] = []
        if hltb and hltb > 0:
            ratio = min(playtime_h / hltb, 1.0)
            score += ratio * 5.0
            reasons.append(f"~{int(ratio * 100)}% through main")
        else:
            score += 1.0
            reasons.append(f"{int(playtime_h)}h played")

        affinity_reasons, affinity_delta = _affinity_contribution(game, req.affinities)
        score += affinity_delta
        all_reasons = affinity_reasons + reasons

        remaining = max((hltb or 0.0) - playtime_h, 0.0) if hltb else (hltb or 0.0)
        c = Candidate(
            gws=gws,
            remaining_hours=remaining,
            score=score,
            source=RecommendMode.continue_something.value,
            reasons=all_reasons[:3],
            warnings=_build_warnings(gws),
        )
        candidates.append(c)

    for c in candidates:
        c.score += random.uniform(-0.3, 0.3)
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:max_results]


# ---------------------------------------------------------------------------
# Comfort pick (high-playtime favorites, time-window ignored)
# ---------------------------------------------------------------------------

_COMFORT_STATUSES = (
    GameStatus.played_unclassified,
    GameStatus.in_progress,
    GameStatus.finished,
)
_COMFORT_FLOOR_DEFAULT_MIN = 600        # 10h baseline
_COMFORT_FLOOR_STEP_MIN = 30
_COMFORT_TARGET_CANDIDATES = 5


def _comfort_eligible(gws: GameWithState) -> bool:
    return gws.state.status in _COMFORT_STATUSES and gws.game.playtime_minutes > 0


def _has_any_comfort_candidate(games: list[GameWithState]) -> bool:
    return any(_comfort_eligible(g) for g in games)


def _resolve_comfort_floor(games: list[GameWithState]) -> int:
    """Top-quartile playtime, floored at 10h, lowered in 30-min steps until 5 qualify."""
    eligible = [g for g in games if _comfort_eligible(g)]
    if not eligible:
        return 0

    playtimes = sorted((g.game.playtime_minutes for g in eligible), reverse=True)
    quartile_idx = max(0, len(playtimes) // 4 - 1)
    quartile_floor = playtimes[quartile_idx]
    floor = max(_COMFORT_FLOOR_DEFAULT_MIN, quartile_floor)

    while floor > 0:
        count = sum(1 for g in eligible if g.game.playtime_minutes >= floor)
        if count >= _COMFORT_TARGET_CANDIDATES:
            return floor
        floor -= _COMFORT_FLOOR_STEP_MIN

    return 0


def _comfort_pick(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int,
) -> list[Candidate]:
    floor = _resolve_comfort_floor(games)
    eligible = [
        g for g in games
        if _comfort_eligible(g) and g.game.playtime_minutes >= floor
    ]
    if not eligible:
        return []

    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)

    candidates = []
    for gws in eligible:
        game = gws.game
        score = min(game.playtime_minutes / 3000.0, 5.0)
        affinity_reasons, affinity_delta = _affinity_contribution(game, req.affinities)
        score += affinity_delta
        if game.last_played_steam and game.last_played_steam >= week_ago:
            score -= 1.0

        hours_played = int(game.playtime_minutes / 60)
        why = [f"{hours_played} hours played"] + affinity_reasons

        c = Candidate(
            gws=gws,
            remaining_hours=game.hltb_main_hours or 0.0,
            score=score,
            source=RecommendMode.comfort_pick.value,
            reasons=why[:3],
            warnings=_build_warnings(gws),
        )
        candidates.append(c)

    return _iterative_variety_select(candidates, max_results)


# ---------------------------------------------------------------------------
# Start something new (long-form unplayed/effectively-untouched)
# ---------------------------------------------------------------------------

def _start_something_new(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int,
) -> list[Candidate]:
    eligible = []
    for g in games:
        s = g.state.status
        if s == GameStatus.never_played:
            eligible.append(g)
        elif s == GameStatus.played_unclassified and g.game.playtime_minutes < 30:
            eligible.append(g)
    return _long_form_score(
        eligible, req, max_results,
        source=RecommendMode.start_something_new.value,
    )


def _long_form_score(
    eligible: list[GameWithState],
    req: RecommendRequest,
    max_results: int,
    source: str,
) -> list[Candidate]:
    candidates = []
    for gws in eligible:
        hltb = gws.game.hltb_main_hours
        if hltb is None or hltb < 8:
            continue

        score, score_reasons = _score_long_term(gws)
        affinity_reasons, affinity_delta = _affinity_contribution(gws.game, req.affinities)
        score += affinity_delta
        hltb_ctx = f"long-form ({_fmt_hours(hltb)})"
        all_reasons = affinity_reasons + score_reasons + [hltb_ctx]
        c = Candidate(
            gws=gws,
            remaining_hours=hltb,
            score=score,
            source=source,
            reasons=all_reasons[:3],
            warnings=_build_warnings(gws),
        )
        candidates.append(c)

    for c in candidates:
        c.score += random.uniform(-1.0, 1.0)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:max_results]


def _score_long_term(gws: GameWithState) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    game, state = gws.game, gws.state

    if _is_critically_acclaimed(game):
        score += 3
        critic = _critic_str(game)
        if critic:
            reasons.append(critic)

    if (
        game.steam_review_pct is not None
        and game.steam_review_pct >= 90
        and (game.steam_review_count or 0) >= 1000
    ):
        score += 2
        ct = game.steam_review_count or 0
        reasons.append(f"Steam {game.steam_review_pct}% ({_fmt_count(ct)})")

    if state.status == GameStatus.in_progress:
        score += 1
        reasons.append("in progress")

    if state.status == GameStatus.dropped:
        score -= 2

    return score, reasons


# ---------------------------------------------------------------------------
# Surprise me (weighted random)
# ---------------------------------------------------------------------------

def _surprise(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int = 5,
) -> list[Candidate]:
    if not games:
        return []

    all_quality = [gws for gws in games if _is_high_quality(gws.game)]
    all_unplayed = [gws for gws in games if gws.state.status == GameStatus.never_played]

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

        avail_all = _available(games)
        if not avail_all:
            break

        avail_quality = _available(all_quality)
        avail_unplayed = _available(all_unplayed)

        roll = random.random()
        if roll < 0.50 and avail_quality:
            pool = avail_quality
            bucket = "quality"
        elif roll < 0.80 and avail_unplayed:
            pool = avail_unplayed
            bucket = "unplayed"
        else:
            pool = avail_all
            bucket = "any"

        pick_gws = random.choice(pool)
        remaining = pick_gws.game.hltb_main_hours or 0.0

        reasons = _surprise_reasons(pick_gws, bucket)
        c = Candidate(
            gws=pick_gws,
            remaining_hours=remaining,
            source=RecommendMode.surprise_me.value,
            reasons=reasons,
            warnings=_build_warnings(pick_gws),
        )
        selected.append(c)
        picked_ids.add(pick_gws.game.appid)

        genre = pick_gws.game.primary_genre()
        if genre:
            excluded_genres.add(genre)

    return selected


def _surprise_reasons(gws: GameWithState, bucket: str) -> list[str]:
    reasons: list[str] = []
    if bucket == "quality":
        q = _quality_str(gws.game)
        reasons.append(q or "highly rated")
        if gws.state.status == GameStatus.never_played:
            reasons.append("never played")
    elif bucket == "unplayed":
        reasons.append("never played")
        q = _quality_str(gws.game)
        if q:
            reasons.append(q)
    else:
        reasons.append("random pick")
    return reasons[:2]


# ---------------------------------------------------------------------------
# Iterative variety selection (shared by short-term and comfort)
# ---------------------------------------------------------------------------

def _iterative_variety_select(
    candidates: list[Candidate],
    max_results: int,
) -> list[Candidate]:
    pool = [Candidate(
        gws=c.gws,
        remaining_hours=c.remaining_hours,
        score=c.score,
        no_manual_hours_hint=c.no_manual_hours_hint,
        source=c.source,
        reasons=c.reasons,
        warnings=c.warnings,
    ) for c in candidates]

    selected: list[Candidate] = []
    while pool and len(selected) < max_results:
        pool.sort(key=lambda c: c.score, reverse=True)
        pick = pool.pop(0)
        selected.append(pick)
        picked_genre = pick.gws.game.primary_genre()
        if picked_genre:
            for rem in pool:
                if rem.gws.game.primary_genre() == picked_genre:
                    rem.score -= 1

    return selected


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _affinity_contribution(game, affinities: dict) -> tuple[list[str], float]:
    if not affinities:
        return [], 0.0
    from app.affinity import compute_affinity_score, get_affinity_summary
    delta = compute_affinity_score(game, affinities)
    reasons = get_affinity_summary(game, affinities) if abs(delta) >= 0.1 else []
    return reasons, delta


def _remaining_hours(gws: GameWithState) -> tuple[Optional[float], bool]:
    game, state = gws.game, gws.state
    hltb = game.hltb_main_hours
    if hltb is None:
        return None, False
    if state.status == GameStatus.in_progress and state.hours_played_manual is not None:
        return max(hltb - state.hours_played_manual, 0.5), False
    if state.status == GameStatus.in_progress:
        return hltb, True
    return hltb, False


def _build_warnings(gws: GameWithState) -> list[str]:
    warnings: list[str] = []
    game, state = gws.game, gws.state
    if game.hltb_main_hours is None:
        warnings.append("duration unknown")
    if state.status == GameStatus.dropped:
        warnings.append("dropped previously")
    if game.last_refreshed is None:
        warnings.append("data never refreshed")
    elif datetime.utcnow() - game.last_refreshed > timedelta(days=90):
        warnings.append("data older than 90 days")
    return warnings


def _quality_str(game) -> Optional[str]:
    parts: list[str] = []
    if game.metacritic_score is not None and game.metacritic_score >= 85:
        parts.append(f"MC {game.metacritic_score}")
    if game.opencritic_score is not None and game.opencritic_score >= 85:
        parts.append(f"OC {game.opencritic_score}")
    if (
        game.steam_review_pct is not None
        and game.steam_review_pct >= 90
        and (game.steam_review_count or 0) >= 1000
    ):
        ct = game.steam_review_count or 0
        parts.append(f"Steam {game.steam_review_pct}% ({_fmt_count(ct)})")
    return " · ".join(parts) if parts else None


def _critic_str(game) -> Optional[str]:
    parts: list[str] = []
    if game.metacritic_score is not None and game.metacritic_score >= 85:
        parts.append(f"MC {game.metacritic_score}")
    if game.opencritic_score is not None and game.opencritic_score >= 85:
        parts.append(f"OC {game.opencritic_score}")
    return " · ".join(parts) if parts else None


def _is_high_quality(game) -> bool:
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
    if game.metacritic_score is not None and game.metacritic_score >= 85:
        return True
    if game.opencritic_score is not None and game.opencritic_score >= 85:
        return True
    return False


def _fmt_hours(h: float) -> str:
    if h < 1:
        return f"{int(h * 60)}m"
    if h == int(h):
        return f"{int(h)}h"
    return f"{h:.1f}h"


def _fmt_count(n: int) -> str:
    if n >= 10_000:
        return f"{n // 1000}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return str(n)
