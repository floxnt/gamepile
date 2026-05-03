"""
Backlog view — pure domain logic.

Builds the sectioned, filtered, sorted view that powers /backlog from a
list of GameWithState. Also owns the action-set authority for the
overflow menu (valid_actions_for_status), derived from docs/STATE_MACHINE.md.

No FastAPI / DB imports — keep this layer testable in isolation.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from app.affinity import compute_affinity_score
from app.models import GameStatus, GameWithState


# Section keys — used in URL params, template loops, and dict lookups.
SECTION_IN_PROGRESS = "in_progress"
SECTION_PLAYED_UNCLASSIFIED = "played_unclassified"
SECTION_NEVER_PLAYED = "never_played"
SECTION_DROPPED_SOFT = "dropped_soft"

DEFAULT_SECTIONS = (SECTION_IN_PROGRESS, SECTION_PLAYED_UNCLASSIFIED, SECTION_NEVER_PLAYED)
ALL_SECTIONS = (*DEFAULT_SECTIONS, SECTION_DROPPED_SOFT)

SECTION_TITLES = {
    SECTION_IN_PROGRESS: "Picking up where you left off",
    SECTION_PLAYED_UNCLASSIFIED: "Barely touched",
    SECTION_NEVER_PLAYED: "Never played",
    SECTION_DROPPED_SOFT: "Bounced off",
}

# Time-fit chip → (lo_h, hi_h or None for unbounded). Special: "unknown" → HLTB null.
TIME_FIT_BUCKETS: dict[str, Optional[tuple[float, Optional[float]]]] = {
    "short": (0.0, 5.0),
    "medium": (5.0, 15.0),
    "long": (15.0, 50.0),
    "very_long": (50.0, None),
    "unknown": None,
}

TIME_FIT_LABELS = {
    "short": "Short (<5h)",
    "medium": "Medium (5–15h)",
    "long": "Long (15–50h)",
    "very_long": "Very long (50+h)",
    "unknown": "Unknown",
}

STATUS_CHIP_LABELS = {
    SECTION_IN_PROGRESS: "In progress",
    SECTION_PLAYED_UNCLASSIFIED: "Barely touched",
    SECTION_NEVER_PLAYED: "Never played",
    SECTION_DROPPED_SOFT: "Bounced off",
}

# User-selectable sort keys (per spec). Internal-only keys ("completion",
# "recently_dropped") are dispatched when the user selects "default".
VALID_SORT_KEYS = ("default", "title", "hltb_main", "playtime", "recently_added", "affinity")
SORT_LABELS = {
    "default": "Default",
    "title": "Title",
    "hltb_main": "HLTB main",
    "playtime": "Playtime",
    "recently_added": "Recently added",
    "affinity": "Affinity score",
}


@dataclass
class BacklogFilters:
    time_fit: frozenset = field(default_factory=frozenset)
    tags: frozenset = field(default_factory=frozenset)
    statuses: frozenset = field(default_factory=frozenset)
    has_hltb: bool = False
    include_bounced: bool = False
    sort_keys: dict = field(default_factory=dict)

    def is_active(self) -> bool:
        return bool(
            self.time_fit or self.tags or self.statuses
            or self.has_hltb or self.include_bounced
        )


@dataclass
class HeaderStats:
    total_count: int
    total_hours: float
    games_with_unknown_hltb: int
    in_progress_count: int
    played_unclassified_count: int
    never_played_count: int


@dataclass
class BacklogSection:
    key: str
    title: str
    rows: list
    sort_key: str


@dataclass
class BacklogView:
    sections: list
    header_stats: HeaderStats
    top_tags: list
    contradictory_filter_warning: Optional[str]
    is_empty_no_filters: bool
    is_empty_due_to_filters: bool


# ---------------------------------------------------------------------------
# Overflow-menu action authority
# ---------------------------------------------------------------------------

def valid_actions_for_status(status: GameStatus) -> list:
    """Return [(action_key, label), ...] for the overflow menu of a backlog row.

    action_key matches the existing /games/{appid}/quick-action vocabulary,
    plus the new 'mark_in_progress' and 'pick' keys. Labels can vary per source
    state — e.g. dropped-soft rows show 'Reconsider' for mark_in_progress.
    """
    if status == GameStatus.never_played:
        return [
            ("pick", "I picked this"),
            ("already_completed", "Already completed"),
            ("never_recommend", "Never recommend"),
        ]
    if status == GameStatus.played_unclassified:
        return [
            ("mark_in_progress", "Mark in progress"),
            ("finished", "Mark finished"),
            ("bounced", "Bounced off it"),
            ("not_my_thing", "Not my thing"),
            ("never_recommend", "Never recommend"),
        ]
    if status == GameStatus.in_progress:
        return [
            ("finished", "Mark finished"),
            ("bounced", "Bounced off it"),
            ("not_my_thing", "Not my thing"),
            ("never_recommend", "Never recommend"),
        ]
    if status == GameStatus.dropped:
        # Backlog only shows dropped (soft); strong drops are excluded entirely.
        return [
            ("mark_in_progress", "Reconsider"),
            ("finished", "Mark finished"),
            ("not_my_thing", "Not my thing"),
            ("never_recommend", "Never recommend"),
        ]
    return []


# ---------------------------------------------------------------------------
# Query parsing
# ---------------------------------------------------------------------------

def parse_backlog_query(qp) -> BacklogFilters:
    """Parse a starlette QueryParams into BacklogFilters. Unknown values silently dropped."""
    time_fit = frozenset(v for v in qp.getlist("time_fit") if v in TIME_FIT_BUCKETS)
    tags = frozenset(v.strip().lower() for v in qp.getlist("tag") if v.strip())
    statuses = frozenset(v for v in qp.getlist("status") if v in ALL_SECTIONS)
    has_hltb = qp.get("has_hltb", "").lower() in ("true", "1", "yes", "on")
    include_bounced = qp.get("include_bounced", "").lower() in ("true", "1", "yes", "on")

    sort_keys = {}
    for section in ALL_SECTIONS:
        v = qp.get(f"{section}_sort")
        if v in VALID_SORT_KEYS:
            sort_keys[section] = v

    return BacklogFilters(
        time_fit=time_fit,
        tags=tags,
        statuses=statuses,
        has_hltb=has_hltb,
        include_bounced=include_bounced,
        sort_keys=sort_keys,
    )


# ---------------------------------------------------------------------------
# Eligibility / filtering
# ---------------------------------------------------------------------------

def _is_globally_excluded(gws: GameWithState) -> bool:
    """Backlog never shows: blacklisted, finished, strong-dropped, not_interested."""
    state = gws.state
    if state.blacklisted:
        return True
    if state.status == GameStatus.dropped and state.dropped_strength == "strong":
        return True
    if state.status in (GameStatus.finished, GameStatus.not_interested):
        return True
    return False


def _section_for_game(gws: GameWithState) -> Optional[str]:
    s = gws.state.status
    if s == GameStatus.in_progress:
        return SECTION_IN_PROGRESS
    if s == GameStatus.played_unclassified:
        return SECTION_PLAYED_UNCLASSIFIED
    if s == GameStatus.never_played:
        return SECTION_NEVER_PLAYED
    if s == GameStatus.dropped and gws.state.dropped_strength == "soft":
        return SECTION_DROPPED_SOFT
    return None


def _passes_time_fit(gws: GameWithState, time_fit: frozenset) -> bool:
    if not time_fit:
        return True
    h = gws.game.hltb_main_hours
    for bucket in time_fit:
        if bucket == "unknown":
            if h is None:
                return True
            continue
        rng = TIME_FIT_BUCKETS[bucket]
        if rng is None or h is None:
            continue
        lo, hi = rng
        if hi is None and h >= lo:
            return True
        if hi is not None and lo <= h <= hi:
            return True
    return False


def _passes_tags(gws: GameWithState, tags: frozenset) -> bool:
    if not tags:
        return True
    game_tags = {t.lower() for t in gws.game.user_tags_list()}
    return bool(tags & game_tags)


def _passes_has_hltb(gws: GameWithState, has_hltb: bool) -> bool:
    if not has_hltb:
        return True
    return gws.game.hltb_main_hours is not None


def _section_after_filters(gws: GameWithState, filters: BacklogFilters) -> Optional[str]:
    """Return the section a game belongs to after all filters, or None to exclude."""
    section = _section_for_game(gws)
    if section is None:
        return None

    # Dropped-soft section: only included when the user opts in via toggle or chip.
    if section == SECTION_DROPPED_SOFT:
        if not (filters.include_bounced or SECTION_DROPPED_SOFT in filters.statuses):
            return None

    # Status chips override default scope: when set, only matching sections appear.
    if filters.statuses and section not in filters.statuses:
        return None

    if not _passes_time_fit(gws, filters.time_fit):
        return None
    if not _passes_tags(gws, filters.tags):
        return None
    if not _passes_has_hltb(gws, filters.has_hltb):
        return None

    return section


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

def completion_pct(gws: GameWithState) -> float:
    """Estimated completion through main story (0.0–1.0). 0 if HLTB unknown."""
    h = gws.game.hltb_main_hours
    if not h or h <= 0:
        return 0.0
    if gws.state.hours_played_manual is not None:
        played_h = gws.state.hours_played_manual
    else:
        played_h = gws.game.playtime_minutes / 60.0
    return min(played_h / h, 1.0)


def _default_sort_key(section: str) -> str:
    return {
        SECTION_IN_PROGRESS: "completion",
        SECTION_PLAYED_UNCLASSIFIED: "playtime",
        SECTION_NEVER_PLAYED: "affinity",
        SECTION_DROPPED_SOFT: "recently_dropped",
    }[section]


def _sort_rows(rows: list, section: str, sort_key: str, affinities: dict) -> list:
    """Return rows sorted per the chosen sort key. 'default' → section-specific."""
    if sort_key == "default":
        sort_key = _default_sort_key(section)

    if sort_key == "title":
        return sorted(rows, key=lambda g: g.game.name.lower())
    if sort_key == "hltb_main":
        # Unknown HLTB → bottom: tuple sorts (False, ...) < (True, ...).
        return sorted(rows, key=lambda g: (g.game.hltb_main_hours is None, -(g.game.hltb_main_hours or 0)))
    if sort_key == "playtime":
        return sorted(rows, key=lambda g: -g.game.playtime_minutes)
    if sort_key == "recently_added":
        # Proxy: state.updated_at — set on insert, only changed by user actions.
        return sorted(rows, key=lambda g: g.state.updated_at, reverse=True)
    if sort_key == "affinity":
        return sorted(rows, key=lambda g: -compute_affinity_score(g.game, affinities))
    if sort_key == "completion":
        return sorted(rows, key=lambda g: -completion_pct(g))
    if sort_key == "recently_dropped":
        return sorted(rows, key=lambda g: g.state.updated_at, reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Header stats / top tags
# ---------------------------------------------------------------------------

def compute_header_stats(games: list) -> HeaderStats:
    total = len(games)
    in_p = sum(1 for g in games if g.state.status == GameStatus.in_progress)
    played_u = sum(1 for g in games if g.state.status == GameStatus.played_unclassified)
    never_p = sum(1 for g in games if g.state.status == GameStatus.never_played)
    total_hours = 0.0
    unknown = 0
    for g in games:
        h = g.game.hltb_main_hours
        if h is None:
            unknown += 1
        else:
            total_hours += h
    return HeaderStats(
        total_count=total,
        total_hours=total_hours,
        games_with_unknown_hltb=unknown,
        in_progress_count=in_p,
        played_unclassified_count=played_u,
        never_played_count=never_p,
    )


def top_user_tags(games: list, n: int = 15) -> list:
    counter: Counter = Counter()
    for g in games:
        for tag in g.game.user_tags_list():
            counter[tag] += 1
    return [t for t, _ in counter.most_common(n)]


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def build_backlog_view(games: list, filters: BacklogFilters, affinities: dict) -> BacklogView:
    show_dropped = filters.include_bounced or SECTION_DROPPED_SOFT in filters.statuses

    # Pre-filter pool: backlog-eligible games regardless of filter narrowing.
    # Used for header stats and top-tag chips so users see global backlog state.
    default_eligible = []
    for g in games:
        if _is_globally_excluded(g):
            continue
        section = _section_for_game(g)
        if section in DEFAULT_SECTIONS:
            default_eligible.append(g)

    stats = compute_header_stats(default_eligible)
    top_tags = top_user_tags(default_eligible, n=15)

    # Apply filters and bucket into sections.
    section_buckets: dict = {key: [] for key in ALL_SECTIONS}
    for g in games:
        if _is_globally_excluded(g):
            continue
        section = _section_after_filters(g, filters)
        if section is None:
            continue
        section_buckets[section].append(g)

    sections: list = []
    for key in ALL_SECTIONS:
        rows = section_buckets[key]
        if not rows:
            continue
        sort_key = filters.sort_keys.get(key, "default")
        rows = _sort_rows(rows, key, sort_key, affinities)
        sections.append(BacklogSection(
            key=key,
            title=SECTION_TITLES[key],
            rows=rows,
            sort_key=sort_key,
        ))

    warning = None
    if "unknown" in filters.time_fit and filters.has_hltb:
        warning = (
            "Note: 'Time fit: Unknown' and 'Has HLTB data' are contradictory — "
            "clear one to see results."
        )

    return BacklogView(
        sections=sections,
        header_stats=stats,
        top_tags=top_tags,
        contradictory_filter_warning=warning,
        is_empty_no_filters=(stats.total_count == 0 and not show_dropped),
        is_empty_due_to_filters=(not sections and stats.total_count > 0),
    )
