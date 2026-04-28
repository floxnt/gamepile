"""
Recommendation engine — v1.

Four modes: short_term, long_term, both, surprise.

SHORT-TERM  — fits tonight's time window (±50%).
LONG-TERM   — worth committing to across sessions (hltb ≥ 8h).
BOTH        — blended: ~3 short-term + ~2 long-term.
SURPRISE    — weighted random from the full eligible library.

Every Candidate carries:
  source    — "tonight" | "long_term" | "surprise" (set in all modes)
  reasons   — up to 2 human-readable strings explaining the pick
  warnings  — zero or more amber advisory strings shown on the card
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
    surprise_me = "surprise_me"


@dataclass
class RecommendRequest:
    minutes: int                              # tonight's available time
    mode: RecommendMode
    include_unplayed: bool = True
    include_in_progress: bool = True
    installed_only: bool = False              # always False in v1 (data not available)
    excluded_ids: frozenset = field(default_factory=frozenset)
    # {(kind, value_lower): (weight, pick_count)} from db.get_all_affinities().
    # Empty dict = no affinity data yet; scoring is identical to v1 in that case.
    affinities: dict = field(default_factory=dict)


@dataclass
class Candidate:
    gws: GameWithState
    remaining_hours: float
    score: float = 0.0
    no_manual_hours_hint: bool = False
    source: Optional[str] = None             # "tonight" | "long_term" | "surprise"
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def recommend(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int = 5,
) -> list[Candidate]:
    # Hard exclusions first: blacklisted games never appear in any mode.
    games = [g for g in games if not g.state.blacklisted]
    if req.excluded_ids:
        games = [g for g in games if g.game.appid not in req.excluded_ids]

    if req.mode == RecommendMode.short_term:
        return _short_term(games, req, max_results)
    if req.mode == RecommendMode.long_term:
        return _long_term(games, req, max_results)
    if req.mode == RecommendMode.surprise_me:
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
    lo, hi = window_hours * 0.5, window_hours * 1.5

    candidates = []
    for gws in games:
        if not _passes_toggles(gws, req):
            continue
        remaining, hint = _remaining_hours(gws)
        if remaining is None:
            continue
        if not (lo <= remaining <= hi):
            continue

        score, score_reasons = _score_short_term(gws)
        affinity_reasons, affinity_delta = _affinity_contribution(gws.game, req.affinities)
        score += affinity_delta
        # Affinity reasons take priority; "fits window" is always the baseline context.
        all_reasons = affinity_reasons + score_reasons + [f"fits {req.minutes}min window"]
        c = Candidate(
            gws=gws,
            remaining_hours=remaining,
            score=score,
            no_manual_hours_hint=hint,
            source="tonight",
            reasons=all_reasons[:3],
            warnings=_build_warnings(gws),
        )
        candidates.append(c)

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

        score, score_reasons = _score_long_term(gws)
        affinity_reasons, affinity_delta = _affinity_contribution(gws.game, req.affinities)
        score += affinity_delta
        hltb_ctx = f"long-form ({_fmt_hours(hltb)})"
        all_reasons = affinity_reasons + score_reasons + [hltb_ctx]
        c = Candidate(
            gws=gws,
            remaining_hours=hltb,
            score=score,
            source="long_term",
            reasons=all_reasons[:3],
            warnings=_build_warnings(gws),
        )
        candidates.append(c)

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
# Both
# ---------------------------------------------------------------------------

def _both(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int = 5,
) -> list[Candidate]:
    target_short, target_long = 3, 2

    short_pool = _short_term(games, req, max_results=max_results)
    long_pool = _long_term(games, req, max_results=max_results)

    selected: list[Candidate] = []
    picked_ids: set[int] = set()

    for c in short_pool:
        if len(selected) >= target_short:
            break
        selected.append(c)
        picked_ids.add(c.gws.game.appid)

    long_filled = 0
    for c in long_pool:
        if long_filled >= target_long:
            break
        if c.gws.game.appid not in picked_ids:
            selected.append(c)
            picked_ids.add(c.gws.game.appid)
            long_filled += 1

    for c in long_pool:
        if len(selected) >= max_results:
            break
        if c.gws.game.appid not in picked_ids:
            selected.append(c)
            picked_ids.add(c.gws.game.appid)

    for c in short_pool:
        if len(selected) >= max_results:
            break
        if c.gws.game.appid not in picked_ids:
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
    eligible = [gws for gws in games if _passes_toggles(gws, req)]
    if not eligible:
        return []

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
            source="surprise_me",
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
# Iterative variety selection (shared by short-term)
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
    """Return (reason_strings, score_delta) for one game. Empty when no affinities."""
    if not affinities:
        return [], 0.0
    # Import here to avoid a circular import at module load time.
    from app.affinity import compute_affinity_score, get_affinity_summary
    delta = compute_affinity_score(game, affinities)
    reasons = get_affinity_summary(game, affinities) if abs(delta) >= 0.1 else []
    return reasons, delta


def _passes_toggles(gws: GameWithState, req: RecommendRequest) -> bool:
    state = gws.state
    # Exclude terminal/completion statuses by default.
    # played = auto-inferred from Steam hours; excluded like finished since the
    # user has already experienced the game. They can re-open it manually.
    if state.status in (GameStatus.not_interested, GameStatus.finished, GameStatus.played):
        return False
    if state.status == GameStatus.never_played and not req.include_unplayed:
        return False
    if state.status == GameStatus.in_progress and not req.include_in_progress:
        return False
    return True


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
    """Compact quality signal string for the Why line, or None."""
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
    """MC/OC critic score string for the Why line."""
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
    """1234 → '1.2k', 15000 → '15k', 999 → '999'."""
    if n >= 10_000:
        return f"{n // 1000}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return str(n)
