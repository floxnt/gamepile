"""
Recommendation engine — Shortlist (v2.5).

Five user-intent modes:

I_ONLY_HAVE_TONIGHT — fits tonight's time window. Initial filter is ±25% of
                     the requested minutes; widens to ±50% then ±75% only if
                     fewer than 5 candidates qualify. Games with no HLTB data
                     skip the time filter entirely (eligible regardless).
CONTINUE_SOMETHING — surfaces in_progress games, weighted by closeness to
                     completion. Time is a soft scoring preference, not a filter.
COMFORT_PICK       — high-playtime games the user has clearly enjoyed; ignores time.
START_SOMETHING_NEW — never_played + effectively-untouched played_unclassified
                     games worth committing to (uses the long-form scoring).
                     Time is a soft scoring preference, not a filter.
SURPRISE_ME        — weighted random with quality bias; ignores time.

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

from app.game_type import (
    GAME_TYPE_BETA_PLAYTEST,
    GAME_TYPE_MMO,
    GAME_TYPE_MULTIPLAYER,
    GAME_TYPE_NO_ENDPOINT,
    GAME_TYPE_SANDBOX,
    GAME_TYPE_SOFTWARE,
    resolve_type,
)
from app.models import GameStatus, GameWithState


# Game types globally excluded from every Shortlist mode. Software entries
# (Wallpaper Engine, Lossless Scaling, 3DMark, etc.) aren't games the
# "Find Games" surface should ever recommend. Beta/playtest entries are
# transient builds and equally inappropriate. Early Access and Expansion
# stay eligible — both surface real games the user may want to play.
_SHORTLIST_EXCLUDED_GAME_TYPES: frozenset = frozenset({
    GAME_TYPE_SOFTWARE,
    GAME_TYPE_BETA_PLAYTEST,
})

_OPEN_ENDED_GAME_TYPES: frozenset = frozenset({
    GAME_TYPE_MULTIPLAYER,
    GAME_TYPE_MMO,
    GAME_TYPE_NO_ENDPOINT,
    GAME_TYPE_SANDBOX,
})


class RecommendMode(str, Enum):
    i_only_have_tonight = "i_only_have_tonight"
    continue_something = "continue_something"
    comfort_pick = "comfort_pick"
    start_something_new = "start_something_new"
    surprise_me = "surprise_me"


# Score boost applied to games pinned via the Backlog view's "Add to Shortlist"
# button. Calibrated so a low-base pinned game (0–2) reliably clears the 5th-
# place cut in every mode, while a top-tier organic competitor (~base 5 +
# affinity 5 = 10) still beats a low-base pinned game (~6–8) by 2–4 points —
# i.e. pins push to top-5 reliably without auto-winning.
# Surprise me is excluded by design (no additive scoring there).
PIN_SCORE_BOOST = 6.0


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
class ExplanationComponent:
    label: str
    magnitude: float  # absolute contribution magnitude for ordering
    is_negative: bool = False

@dataclass
class Candidate:
    gws: GameWithState
    remaining_hours: float
    score: float = 0.0
    no_manual_hours_hint: bool = False
    source: Optional[str] = None
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    explanation: list = field(default_factory=list)  # list[ExplanationComponent]


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
    if resolve_type(gws.game) in _SHORTLIST_EXCLUDED_GAME_TYPES:
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


_SHORT_TERM_WIDENING = (0.25, 0.50, 0.75)


def _short_term(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int,
) -> list[Candidate]:
    window_h = req.minutes / 60.0
    eligible = [g for g in games if _passes_short_term_toggles(g, req)]

    # Split eligible games into time-known and time-unknown. Time-unknown
    # games (no HLTB) bypass the time filter entirely so they remain reachable.
    time_known: list[tuple[GameWithState, float, bool]] = []
    time_unknown: list[GameWithState] = []
    for gws in eligible:
        rem, hint = _remaining_hours(gws)
        if rem is None:
            time_unknown.append(gws)
        else:
            time_known.append((gws, rem, hint))

    # Iteratively widen the time filter until 5 candidates qualify (counting
    # the time-unknown pool, which is constant across widenings).
    fit: list[tuple[GameWithState, float, bool]] = []
    spread_used = _SHORT_TERM_WIDENING[0]
    for spread in _SHORT_TERM_WIDENING:
        lo, hi = window_h * (1 - spread), window_h * (1 + spread)
        fit = [t for t in time_known if lo <= t[1] <= hi]
        spread_used = spread
        if len(fit) + len(time_unknown) >= max_results:
            break

    candidates: list[Candidate] = []
    for gws, remaining, hint in fit:
        score, score_reasons = _score_short_term(gws)
        affinity_reasons, affinity_delta = _affinity_contribution(gws.game, req.affinities)
        score += affinity_delta
        pin_reasons, pin_delta = _pin_contribution(gws)
        score += pin_delta
        fit_reason = f"fits {req.minutes}min window"
        if spread_used > 0.25:
            fit_reason = f"close to {req.minutes}min window"
        all_reasons = pin_reasons + affinity_reasons + score_reasons + [fit_reason]

        st_label, st_mag, rec_label, rec_mag, q_label, q_mag = _short_term_explanation_parts(gws)
        fit_label = f"Short — fits tonight's window" if spread_used <= 0.25 else f"Close to tonight's window"
        explanation = _build_explanation(
            gws, req,
            status_label=st_label, status_magnitude=st_mag,
            recency_label=rec_label, recency_magnitude=rec_mag,
            quality_label=q_label, quality_magnitude=q_mag,
            time_fit_label=fit_label, time_fit_magnitude=1.0,
            pin_active=gws.state.pinned_for_shortlist,
        )
        candidates.append(Candidate(
            gws=gws,
            remaining_hours=remaining,
            score=score,
            no_manual_hours_hint=hint,
            source=RecommendMode.i_only_have_tonight.value,
            reasons=all_reasons[:3],
            warnings=_build_warnings(gws),
            explanation=explanation,
        ))

    for gws in time_unknown:
        score, score_reasons = _score_short_term(gws)
        affinity_reasons, affinity_delta = _affinity_contribution(gws.game, req.affinities)
        score += affinity_delta
        pin_reasons, pin_delta = _pin_contribution(gws)
        score += pin_delta
        all_reasons = pin_reasons + affinity_reasons + score_reasons

        st_label, st_mag, rec_label, rec_mag, q_label, q_mag = _short_term_explanation_parts(gws)
        explanation = _build_explanation(
            gws, req,
            status_label=st_label, status_magnitude=st_mag,
            recency_label=rec_label, recency_magnitude=rec_mag,
            quality_label=q_label, quality_magnitude=q_mag,
            pin_active=gws.state.pinned_for_shortlist,
        )
        candidates.append(Candidate(
            gws=gws,
            remaining_hours=0.0,
            score=score,
            no_manual_hours_hint=False,
            source=RecommendMode.i_only_have_tonight.value,
            reasons=all_reasons[:3],
            warnings=_build_warnings(gws),
            explanation=explanation,
        ))

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


def _short_term_explanation_parts(gws: GameWithState) -> tuple:
    """Return (status_label, status_mag, recency_label, recency_mag, quality_label, quality_mag)."""
    game, state = gws.game, gws.state
    now = datetime.utcnow()
    st_label, st_mag = "", 0.0
    rec_label, rec_mag = "", 0.0
    q_label, q_mag = "", 0.0

    if state.status == GameStatus.in_progress:
        st_label, st_mag = "Already in progress", 2.0
    elif state.status == GameStatus.never_played:
        st_label, st_mag = "Never played", 1.0

    if game.last_played_steam is None:
        if state.status != GameStatus.never_played:
            rec_label, rec_mag = "Never played", 1.0
    elif game.last_played_steam < now - timedelta(days=30):
        rec_label, rec_mag = "Not played recently", 1.0

    q = _quality_str(game)
    if q:
        q_label, q_mag = "Highly reviewed", 1.0

    return st_label, st_mag, rec_label, rec_mag, q_label, q_mag


# ---------------------------------------------------------------------------
# Continue something (in-progress only, near-completion bias)
# ---------------------------------------------------------------------------

def _continue_something(
    games: list[GameWithState],
    req: RecommendRequest,
    max_results: int,
) -> list[Candidate]:
    eligible = [g for g in games if g.state.status == GameStatus.in_progress]
    window_h = req.minutes / 60.0

    candidates = []
    for gws in eligible:
        game = gws.game
        playtime_h = game.playtime_minutes / 60.0
        hltb = game.hltb_main_hours

        score = 0.0
        reasons: list[str] = []
        is_open_ended = resolve_type(game) in _OPEN_ENDED_GAME_TYPES
        if hltb and hltb > 0 and not is_open_ended:
            ratio = min(playtime_h / hltb, 1.0)
            score += ratio * 5.0
            reasons.append(f"~{int(ratio * 100)}% through main")
        else:
            score += 1.0
            reasons.append(f"{int(playtime_h)}h played")

        remaining, _ = _remaining_hours(gws)
        score += _time_fit_penalty(remaining, window_h)

        affinity_reasons, affinity_delta = _affinity_contribution(game, req.affinities)
        score += affinity_delta
        pin_reasons, pin_delta = _pin_contribution(gws)
        score += pin_delta
        all_reasons = pin_reasons + affinity_reasons + reasons

        progress_lbl = ""
        progress_mag = 0.0
        if hltb and hltb > 0 and not is_open_ended:
            ratio = min(playtime_h / hltb, 1.0)
            progress_lbl = f"~{int(ratio * 100)}% through main story"
            progress_mag = ratio * 5.0
        else:
            progress_lbl = f"Played {int(playtime_h)}h"
            progress_mag = 1.0

        tf_penalty = _time_fit_penalty(remaining, window_h)
        tf_label, tf_mag, tf_neg = "", 0.0, False
        if tf_penalty < -0.5:
            tf_label = "Large remaining time"
            tf_mag = abs(tf_penalty)
            tf_neg = True

        explanation = _build_explanation(
            gws, req,
            status_label="Already in progress", status_magnitude=2.0,
            progress_label=progress_lbl, progress_magnitude=progress_mag,
            time_fit_label=tf_label, time_fit_magnitude=tf_mag, time_fit_negative=tf_neg,
            pin_active=gws.state.pinned_for_shortlist,
        )

        c = Candidate(
            gws=gws,
            remaining_hours=remaining or 0.0,
            score=score,
            source=RecommendMode.continue_something.value,
            reasons=all_reasons[:3],
            warnings=_build_warnings(gws),
            explanation=explanation,
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
        pin_reasons, pin_delta = _pin_contribution(gws)
        score += pin_delta
        if game.last_played_steam and game.last_played_steam >= week_ago:
            score -= 1.0

        hours_played = int(game.playtime_minutes / 60)
        why = pin_reasons + [f"{hours_played} hours played"] + affinity_reasons

        recency_label = ""
        recency_mag = 0.0
        if game.last_played_steam and game.last_played_steam >= week_ago:
            recency_label = "Recently played — deprioritized for variety"
            recency_mag = 1.0

        explanation = _build_explanation(
            gws, req,
            status_label=f"{hours_played} hours played — a proven favorite",
            status_magnitude=min(game.playtime_minutes / 3000.0, 5.0),
            recency_label=recency_label, recency_magnitude=recency_mag,
            pin_active=gws.state.pinned_for_shortlist,
        )

        c = Candidate(
            gws=gws,
            remaining_hours=game.hltb_main_hours or 0.0,
            score=score,
            source=RecommendMode.comfort_pick.value,
            reasons=why[:3],
            warnings=_build_warnings(gws),
            explanation=explanation,
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
    window_h = req.minutes / 60.0

    candidates = []
    for gws in eligible:
        hltb = gws.game.hltb_main_hours
        if hltb is None or hltb < 8:
            continue

        score, score_reasons = _score_long_term(gws)
        affinity_reasons, affinity_delta = _affinity_contribution(gws.game, req.affinities)
        score += affinity_delta
        pin_reasons, pin_delta = _pin_contribution(gws)
        score += pin_delta

        remaining, _ = _remaining_hours(gws)
        score += _time_fit_penalty(remaining, window_h)

        hltb_ctx = f"long-form ({_fmt_hours(hltb)})"
        all_reasons = pin_reasons + affinity_reasons + score_reasons + [hltb_ctx]

        q_label, q_mag = "", 0.0
        if _is_critically_acclaimed(gws.game):
            q_label = _critic_str(gws.game) or "Critically acclaimed"
            q_mag = 3.0
        elif _is_high_quality(gws.game):
            q_label = "Highly reviewed"
            q_mag = 2.0

        tf_penalty = _time_fit_penalty(remaining, window_h)
        tf_label, tf_mag, tf_neg = "", 0.0, False
        if tf_penalty < -0.5:
            tf_label = "Long commitment"
            tf_mag = abs(tf_penalty)
            tf_neg = True

        explanation = _build_explanation(
            gws, req,
            status_label="Never played", status_magnitude=1.0,
            quality_label=q_label, quality_magnitude=q_mag,
            time_fit_label=tf_label, time_fit_magnitude=tf_mag, time_fit_negative=tf_neg,
            pin_active=gws.state.pinned_for_shortlist,
            extra=[ExplanationComponent(f"Long-form commitment ({_fmt_hours(hltb)})", 0.5)],
        )

        c = Candidate(
            gws=gws,
            remaining_hours=remaining or hltb,
            score=score,
            source=source,
            reasons=all_reasons[:3],
            warnings=_build_warnings(gws),
            explanation=explanation,
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

        explanation: list[ExplanationComponent] = []
        if bucket == "quality":
            explanation.append(ExplanationComponent("Highly reviewed", 2.0))
        if pick_gws.state.status == GameStatus.never_played:
            explanation.append(ExplanationComponent("Never played", 1.0))
        explanation.append(ExplanationComponent("Adds variety — random selection for discovery", 1.0))
        explanation.extend(_affinity_explanation(pick_gws.game, req.affinities))
        explanation.sort(key=lambda c: c.magnitude, reverse=True)

        c = Candidate(
            gws=pick_gws,
            remaining_hours=remaining,
            source=RecommendMode.surprise_me.value,
            reasons=reasons,
            warnings=_build_warnings(pick_gws),
            explanation=explanation,
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
        explanation=c.explanation,
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


def _affinity_explanation(game, affinities: dict) -> list[ExplanationComponent]:
    if not affinities:
        return []
    from app.affinity import get_affinity_explanation
    return get_affinity_explanation(game, affinities)


def _build_explanation(
    gws: GameWithState,
    req: RecommendRequest,
    *,
    status_label: str = "",
    status_magnitude: float = 0.0,
    quality_label: str = "",
    quality_magnitude: float = 0.0,
    recency_label: str = "",
    recency_magnitude: float = 0.0,
    progress_label: str = "",
    progress_magnitude: float = 0.0,
    time_fit_label: str = "",
    time_fit_magnitude: float = 0.0,
    time_fit_negative: bool = False,
    pin_active: bool = False,
    extra: list = None,
) -> list:
    """Assemble explanation components, sorted by magnitude, skipping zeroes."""
    components: list[ExplanationComponent] = []

    if pin_active:
        components.append(ExplanationComponent("Pinned from Backlog", PIN_SCORE_BOOST))

    if status_label and status_magnitude > 0:
        components.append(ExplanationComponent(status_label, status_magnitude))

    if quality_label and quality_magnitude > 0:
        components.append(ExplanationComponent(quality_label, quality_magnitude))

    if recency_label and recency_magnitude > 0:
        components.append(ExplanationComponent(recency_label, recency_magnitude))

    if progress_label and progress_magnitude > 0:
        components.append(ExplanationComponent(progress_label, progress_magnitude))

    if time_fit_label and time_fit_magnitude > 0:
        components.append(ExplanationComponent(time_fit_label, time_fit_magnitude, is_negative=time_fit_negative))

    components.extend(_affinity_explanation(gws.game, req.affinities))

    if extra:
        components.extend(extra)

    components.sort(key=lambda c: c.magnitude, reverse=True)
    return components


def _pin_contribution(gws: GameWithState) -> tuple[list[str], float]:
    """Pin boost for games flagged in the Backlog view.

    Mirrors _affinity_contribution shape so callers can compose the same way.
    Surprise me does not call this — it has no additive scoring.
    """
    if gws.state.pinned_for_shortlist:
        return ["pinned from backlog"], PIN_SCORE_BOOST
    return [], 0.0


def _remaining_hours(gws: GameWithState) -> tuple[Optional[float], bool]:
    """
    Estimated hours of main-story play left for this game.

    Returns (remaining_h, no_manual_hours_hint).
    A None remaining_h means HLTB data is unavailable — callers should treat
    the candidate as time-unknown rather than excluding it.

    Per the v2.5 time-handling spec:
      - in_progress / played_unclassified: max(hltb - hours_played_manual, 0.5)
        if manual hours are set; otherwise max(hltb - playtime_minutes/60, 0.5).
        The hint is True when the fallback Steam-playtime path was used.
      - never_played, finished, dropped: full HLTB main.
    """
    game, state = gws.game, gws.state
    hltb = game.hltb_main_hours
    if hltb is None:
        return None, False

    progress_states = (GameStatus.in_progress, GameStatus.played_unclassified)
    if state.status in progress_states:
        if state.hours_played_manual is not None:
            return max(hltb - state.hours_played_manual, 0.5), False
        played_h = game.playtime_minutes / 60.0
        return max(hltb - played_h, 0.5), True

    return hltb, False


def _time_fit_penalty(remaining_h: Optional[float], window_h: float) -> float:
    """
    Soft scoring penalty for games whose remaining time exceeds the user's
    available window. Used by Continue something and Start something new where
    time is a preference, not a hard filter.

    Undershoot is free (you have spare time). Overshoot is penalised at
    -0.2 per hour, capped at -2.0 so it can never dominate quality/affinity
    scoring. Returns 0 when remaining time is unknown.
    """
    if remaining_h is None or window_h <= 0:
        return 0.0
    overshoot = max(remaining_h - window_h, 0.0)
    return -min(overshoot * 0.2, 2.0)


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
    # OpenCritic removed in v3 (see PROJECT_STATE.md). Metacritic and Steam
    # review % are the only critic/quality signals; we don't substitute when
    # Metacritic is missing — letting the field be empty is the right call.
    parts: list[str] = []
    if game.metacritic_score is not None and game.metacritic_score >= 85:
        parts.append(f"MC {game.metacritic_score}")
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
    return " · ".join(parts) if parts else None


def _is_high_quality(game) -> bool:
    if game.metacritic_score is not None and game.metacritic_score >= 85:
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
