"""
Dashboard view — pure domain logic.

Builds the read-only data bundle that powers /dashboard from a list of
GameWithState, pick_history rows, and the affinity table. Mirrors the
backlog.py pattern: no FastAPI / DB imports, fully testable.

Three sections per spec:
  1. Top-line stats (backlog count, picks per week, finished this month)
  2. Time-since-last-pick callout (conditional, ≥7 days threshold)
  3. Affinity profile (pills across genres / tags / developers + negatives)
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from app.backlog import (
    DEFAULT_SECTIONS,
    _is_globally_excluded,
    _section_for_game,
    is_forever_game,
)
from app.models import GameStatus, GameWithState, PickHistory


# Status values that count as "in the backlog" for the picks-per-week filter.
_BACKLOG_STATUSES = frozenset({"never_played", "played_unclassified", "in_progress"})

# Affinity profile thresholds — see spec.
PILL_NEUTRAL_CUTOFF = 0.5      # |weight| ≤ this → too neutral to render
NEGATIVES_UNLOCK_BELOW = -1.0  # at least one entry < this unlocks the section
LOW_CONFIDENCE_PICKS = 3       # pick_count < this → render at reduced opacity
TOP_PER_CATEGORY = 5           # genres/tags/developers
TOP_NEGATIVES = 3              # cooler-on section

# Mode strings used for callout link query param.
_MODE_CONTINUE = "continue_something"
_MODE_TONIGHT = "i_only_have_tonight"


@dataclass
class Pill:
    label: str
    weight: float
    pick_count: int
    low_confidence: bool
    is_negative: bool


@dataclass
class AffinityProfile:
    genres: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    developers: list = field(default_factory=list)
    negatives: list = field(default_factory=list)
    is_empty: bool = False


@dataclass
class DashboardData:
    backlog_count: int
    picks_per_week: int
    finished_this_month: int
    days_since_last_pick: Optional[int]
    month_start_label: str             # e.g. "May 1"
    callout_visible: bool
    callout_mode: Optional[str]        # query-param value for the Shortlist link, or None
    affinity_profile: AffinityProfile


# ---------------------------------------------------------------------------
# Picks-per-week eligibility
# ---------------------------------------------------------------------------

def is_backlog_pick(pick: PickHistory) -> bool:
    """Was the picked game backlog-eligible at the moment of the pick?

    Spec rule: status_at_pick ∈ {never_played, played_unclassified, in_progress}
    AND was_forever_at_pick was False.

    NULL-as-include (charity rule): rows inserted before this column existed
    have NULL on both fields. We charitably treat them as backlog-eligible —
    the alternative (excluding) loses real history; the alternative
    (back-inferring from current state) is misleading because state has moved
    on since the pick. Current callers (mark_picked) always populate both
    fields, so this only affects historical data.
    """
    if pick.status_at_pick is None and pick.was_forever_at_pick is None:
        return True
    if pick.was_forever_at_pick:
        return False
    if pick.status_at_pick in _BACKLOG_STATUSES:
        return True
    return False


def compute_picks_per_week(picks: list, now: datetime) -> int:
    """Count picks in the trailing 7 days that pass is_backlog_pick.

    Caller is expected to pass picks already narrowed to picked_at >= now-7d
    via db.get_picks_since — but we re-check defensively so the function is
    self-contained and testable with arbitrary input.
    """
    cutoff = now - timedelta(days=7)
    return sum(
        1 for p in picks
        if p.picked_at >= cutoff and is_backlog_pick(p)
    )


# ---------------------------------------------------------------------------
# Finished this month
# ---------------------------------------------------------------------------

def _start_of_month(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def month_start_label(now: datetime) -> str:
    """Human label for the start of the current month: 'May 1'."""
    return f"{_MONTHS[now.month - 1]} 1"


def compute_finished_this_month(games: list, now: datetime) -> int:
    """Distinct count of games whose state was last updated to 'finished'
    on or after the start of the current calendar month.

    Each appid has at most one game_state row, so distinctness is automatic
    — the spec's deduplication requirement is structurally satisfied.
    """
    start = _start_of_month(now)
    return sum(
        1 for gws in games
        if gws.state.status == GameStatus.finished
        and gws.state.updated_at >= start
    )


# ---------------------------------------------------------------------------
# Days since last pick
# ---------------------------------------------------------------------------

def compute_days_since_last_pick(most_recent: Optional[PickHistory], now: datetime) -> Optional[int]:
    """Whole days between now and the most recent pick. None if no picks yet."""
    if most_recent is None:
        return None
    delta = now - most_recent.picked_at
    return max(delta.days, 0)


# ---------------------------------------------------------------------------
# Callout mode resolution (Dashboard-specific — excludes Forever)
# ---------------------------------------------------------------------------

def resolve_callout_mode(games: list) -> str:
    """Pick which Shortlist mode the time-since callout link should preselect.

    Spec: continue_something if any in_progress game (excluding Forever),
    else i_only_have_tonight. NOTE this differs from the existing
    default_mode_for_library, which treats Forever in_progress games as
    eligible and falls through via comfort_pick — Dashboard's logic
    intentionally does neither.
    """
    for gws in games:
        if _is_globally_excluded(gws):
            continue
        if gws.state.status != GameStatus.in_progress:
            continue
        if is_forever_game(gws.game):
            continue
        return _MODE_CONTINUE
    return _MODE_TONIGHT


# ---------------------------------------------------------------------------
# Affinity profile
# ---------------------------------------------------------------------------

def _make_pill(kind_value_lower, weight: float, pick_count: int) -> Pill:
    # affinities dict keys are (kind, value_lower) — but value_lower has been
    # lowercased; we don't have the original casing here. The user-facing
    # label uses title-case as a reasonable default; if the case-sensitive
    # original is needed, callers can pass the original separately. For the
    # Dashboard it's acceptable to title-case e.g. "souls-like" → "Souls-Like".
    return Pill(
        label=kind_value_lower[1].title() if kind_value_lower[0] != "developer" else kind_value_lower[1].title(),
        weight=round(weight, 1),
        pick_count=pick_count,
        low_confidence=pick_count < LOW_CONFIDENCE_PICKS,
        is_negative=weight < 0,
    )


def _top_n(entries: list, n: int) -> list:
    """Sort by weight descending, take top n."""
    return sorted(entries, key=lambda p: -p.weight)[:n]


def build_affinity_profile(affinities: dict) -> AffinityProfile:
    """affinities: {(kind, value_lower): (weight, pick_count)} from
    db.get_all_affinities. Builds three positive-pill lists (top 5 per kind
    by weight, only entries with |weight| > 0.5) and a negatives list (top
    3 of weight < -0.5, only rendered if at least one entry is < -1.0)."""
    if not affinities:
        return AffinityProfile(is_empty=True)

    by_kind: dict = {"genre": [], "tag": [], "developer": []}
    has_negatives_below_unlock = False

    for (kind, value_lower), (weight, pick_count) in affinities.items():
        if abs(weight) <= PILL_NEUTRAL_CUTOFF:
            continue
        if kind not in by_kind:
            continue
        pill = _make_pill((kind, value_lower), weight, pick_count)
        by_kind[kind].append(pill)
        if weight < NEGATIVES_UNLOCK_BELOW:
            has_negatives_below_unlock = True

    genres = _top_n([p for p in by_kind["genre"] if p.weight > 0], TOP_PER_CATEGORY)
    tags = _top_n([p for p in by_kind["tag"] if p.weight > 0], TOP_PER_CATEGORY)
    developers = _top_n([p for p in by_kind["developer"] if p.weight > 0], TOP_PER_CATEGORY)

    negatives: list = []
    if has_negatives_below_unlock:
        all_neg = (
            [p for p in by_kind["genre"] if p.weight < 0]
            + [p for p in by_kind["tag"] if p.weight < 0]
            + [p for p in by_kind["developer"] if p.weight < 0]
        )
        negatives = sorted(all_neg, key=lambda p: p.weight)[:TOP_NEGATIVES]

    is_empty = not (genres or tags or developers or negatives)
    return AffinityProfile(
        genres=genres,
        tags=tags,
        developers=developers,
        negatives=negatives,
        is_empty=is_empty,
    )


# ---------------------------------------------------------------------------
# Backlog count (reuse backlog domain logic)
# ---------------------------------------------------------------------------

def compute_backlog_count(games: list) -> int:
    """Total games in default-visible backlog sections, matching the count
    shown in the Backlog header band. Excludes Forever, dropped, finished,
    not_interested, blacklisted."""
    count = 0
    for gws in games:
        if _is_globally_excluded(gws):
            continue
        section = _section_for_game(gws)
        if section in DEFAULT_SECTIONS:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

CALLOUT_DAYS_THRESHOLD = 7


def build_dashboard_data(
    games: list,
    picks_last_7d: list,
    most_recent_pick: Optional[PickHistory],
    affinities: dict,
    now: datetime,
) -> DashboardData:
    backlog_count = compute_backlog_count(games)
    picks_per_week = compute_picks_per_week(picks_last_7d, now)
    finished = compute_finished_this_month(games, now)
    days_since = compute_days_since_last_pick(most_recent_pick, now)

    # Callout visibility: ≥7 days since last pick AND user has ever picked.
    callout_visible = days_since is not None and days_since >= CALLOUT_DAYS_THRESHOLD
    callout_mode = resolve_callout_mode(games) if callout_visible else None

    return DashboardData(
        backlog_count=backlog_count,
        picks_per_week=picks_per_week,
        finished_this_month=finished,
        days_since_last_pick=days_since,
        month_start_label=month_start_label(now),
        callout_visible=callout_visible,
        callout_mode=callout_mode,
        affinity_profile=build_affinity_profile(affinities),
    )
