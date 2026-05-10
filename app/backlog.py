"""
Backlog view — pure domain logic.

Builds the sectioned, filtered, sorted view that powers /backlog from a
list of GameWithState. Also owns the action-set authority for the
overflow menu (valid_actions_for_status), derived from docs/STATE_MACHINE.md.

No FastAPI / DB imports — keep this layer testable in isolation.

v3 revision (post-shipping): the single "Barely touched" bucket
(played_unclassified) is split into four ratio-based subsections, plus a
hidden-by-default "Forever games" section for titles with no defined end
state (multiplayer-only, completionist >> main, missing HLTB main).
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from app.affinity import compute_affinity_score
# Game-type classification logic moved to app.game_type — re-exported below
# so existing import paths (`from app.backlog import is_forever_game` /
# `compute_game_type`) keep working. The four-type label/tooltip dicts are
# also re-exported for templates_config callers that haven't been switched.
from app.game_type import (  # noqa: F401
    GAME_TYPE_LINEAR,
    GAME_TYPE_MIXED,
    GAME_TYPE_MULTIPLAYER,
    GAME_TYPE_NO_ENDPOINT,
    GAME_TYPE_LABELS,
    GAME_TYPE_TOOLTIPS,
    classify_game as compute_game_type,
    is_forever_game,
)
from app.models import GameStatus, GameWithState


# Section keys — used in URL params, template loops, and dict lookups.
SECTION_IN_PROGRESS = "in_progress"
SECTION_LIKELY_FINISHED = "likely_finished"
SECTION_FINISHING_LATE = "finishing_late"
SECTION_IN_PROGRESS_UNCONFIRMED = "in_progress_unconfirmed"
SECTION_BARELY_TOUCHED = "barely_touched"
SECTION_NEVER_PLAYED = "never_played"
SECTION_FOREVER_GAMES = "forever_games"
SECTION_DROPPED_SOFT = "dropped_soft"

# Render order (top → bottom) for sections that pass filters.
ALL_SECTIONS = (
    SECTION_IN_PROGRESS,
    SECTION_LIKELY_FINISHED,
    SECTION_FINISHING_LATE,
    SECTION_IN_PROGRESS_UNCONFIRMED,
    SECTION_BARELY_TOUCHED,
    SECTION_NEVER_PLAYED,
    SECTION_FOREVER_GAMES,
    SECTION_DROPPED_SOFT,
)

# Sections counted in the header band stats. Forever and dropped-soft are
# excluded from the "X games in backlog" tally so the headline number reflects
# the visible-by-default backlog.
DEFAULT_SECTIONS = (
    SECTION_IN_PROGRESS,
    SECTION_LIKELY_FINISHED,
    SECTION_FINISHING_LATE,
    SECTION_IN_PROGRESS_UNCONFIRMED,
    SECTION_BARELY_TOUCHED,
    SECTION_NEVER_PLAYED,
)

SECTION_TITLES = {
    SECTION_IN_PROGRESS: "Picking up where you left off",
    SECTION_LIKELY_FINISHED: "Likely finished",
    SECTION_FINISHING_LATE: "In case you haven't finished yet",
    SECTION_IN_PROGRESS_UNCONFIRMED: "In progress (unconfirmed)",
    SECTION_BARELY_TOUCHED: "Barely touched",
    SECTION_NEVER_PLAYED: "Never played",
    SECTION_FOREVER_GAMES: "Forever games",
    SECTION_DROPPED_SOFT: "Bounced off",
}

# Time-fit chip → (lo_h, hi_h or None for unbounded). Special: "unknown" → HLTB null.
TIME_FIT_BUCKETS: dict = {
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

# Status chips. Values are intentionally NOT 1:1 with section keys — the
# "played_unclassified" chip umbrellas all four ratio subsections.
STATUS_CHIP_KEYS = ("in_progress", "played_unclassified", "never_played", "dropped_soft")
STATUS_CHIP_LABELS = {
    "in_progress": "In progress",
    "played_unclassified": "Barely touched",
    "never_played": "Never played",
    "dropped_soft": "Bounced off",
}
STATUS_CHIP_TO_SECTIONS = {
    "in_progress": (SECTION_IN_PROGRESS,),
    "played_unclassified": (
        SECTION_LIKELY_FINISHED,
        SECTION_FINISHING_LATE,
        SECTION_IN_PROGRESS_UNCONFIRMED,
        SECTION_BARELY_TOUCHED,
    ),
    "never_played": (SECTION_NEVER_PLAYED,),
    "dropped_soft": (SECTION_DROPPED_SOFT,),
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

# Ratio thresholds for played_unclassified subdivision.
RATIO_LIKELY_FINISHED = 1.5
RATIO_FINISHING_LATE = 0.7
RATIO_BARELY_TOUCHED = 0.1

# Legacy constant kept for any external callers that imported it directly.
# The new app/game_type.py uses substring-based detection instead of an
# exact-match set, so this is informational rather than load-bearing.
FOREVER_USER_TAGS = frozenset({"roguelike", "roguelite", "sandbox", "open world"})


@dataclass
class BacklogFilters:
    time_fit: frozenset = field(default_factory=frozenset)
    tags: frozenset = field(default_factory=frozenset)
    statuses: frozenset = field(default_factory=frozenset)
    has_hltb: bool = False
    include_bounced: bool = False
    show_forever: bool = False
    sort_keys: dict = field(default_factory=dict)
    # v3.5 polish — Dashboard pill click navigates here with one of these
    # set. Single-value (no multi-select per dimension; that's what the
    # chip system above is for). All three filter case-insensitively.
    genre: str = ""
    tag_pill: str = ""
    developer: str = ""

    def is_active(self) -> bool:
        return bool(
            self.time_fit or self.tags or self.statuses
            or self.has_hltb or self.include_bounced or self.show_forever
            or self.genre or self.tag_pill or self.developer
        )

    def pill_active(self) -> bool:
        """True when any Dashboard-pill-driven filter is set. Drives the
        active-filter indicator above the chip bar."""
        return bool(self.genre or self.tag_pill or self.developer)

    def pill_kind_value(self) -> tuple:
        """(kind, value) for the active pill filter, or (None, None).
        Pill filters are single-dimension by design (one navigation target
        per click); precedence when multiple are set defensively: genre >
        tag > developer."""
        if self.genre:
            return ("genre", self.genre)
        if self.tag_pill:
            return ("tag", self.tag_pill)
        if self.developer:
            return ("developer", self.developer)
        return (None, None)

    def chip_filters_active(self) -> bool:
        """True when any non-pill filter is set. Drives the empty-state
        copy: pill-only-empty gets the targeted message; combined-empty
        falls back to the existing 'No games match your current filters'."""
        return bool(
            self.time_fit or self.tags or self.statuses
            or self.has_hltb or self.include_bounced or self.show_forever
        )


@dataclass
class HeaderStats:
    total_count: int
    total_hours: float
    games_with_unknown_hltb: int
    in_progress_count: int
    likely_finished_count: int
    finishing_late_count: int
    in_progress_unconfirmed_count: int
    barely_touched_count: int
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
    # v3.5 polish — pill-driven filter context for the active-filter
    # indicator and the targeted empty-state copy.
    pill_filter_kind: Optional[str] = None    # "genre" / "tag" / "developer" / None
    pill_filter_value: Optional[str] = None   # display label, e.g. "Action"
    is_empty_pill_only: bool = False          # pill is the LONE active filter and result is empty


def playtime_ratio(gws: GameWithState) -> Optional[float]:
    """Return playtime / HLTB-main as a ratio. None if HLTB main missing."""
    h = gws.game.hltb_main_hours
    if not h or h <= 0:
        return None
    return gws.game.playtime_minutes / (h * 60.0)


# ---------------------------------------------------------------------------
# Overflow-menu action authority
# ---------------------------------------------------------------------------

def valid_actions_for_status(status: GameStatus) -> list:
    """Return [(action_key, label), ...] for the overflow menu of a backlog row.

    action_key matches the existing /games/{appid}/quick-action vocabulary,
    plus 'mark_in_progress', 'pick', and 'confirm_finished' which are
    backlog-specific. Labels can vary per source state — e.g. dropped-soft
    rows show 'Reconsider' for mark_in_progress.

    'confirm_finished' is used in place of 'finished' for played_unclassified,
    in_progress, and dropped — clicking from the backlog applies the +0.5
    affinity nudge that mere correction-of-history (already_completed for
    never_played) does not.
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
            ("confirm_finished", "Mark finished"),
            ("bounced", "Bounced off it"),
            ("not_my_thing", "Not my thing"),
            ("never_recommend", "Never recommend"),
        ]
    if status == GameStatus.in_progress:
        return [
            ("confirm_finished", "Mark finished"),
            ("bounced", "Bounced off it"),
            ("not_my_thing", "Not my thing"),
            ("never_recommend", "Never recommend"),
        ]
    if status == GameStatus.dropped:
        # Backlog only shows dropped (soft); strong drops are excluded entirely.
        return [
            ("mark_in_progress", "Reconsider"),
            ("confirm_finished", "Mark finished"),
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
    # Chip-driven multi-value tag filter. Renamed from `tag` → `tag_chip`
    # in v3.5 polish so the new pill-driven `?tag=` (single-value) doesn't
    # collide. The two systems layer as AND filters on top of each other.
    tags = frozenset(v.strip().lower() for v in qp.getlist("tag_chip") if v.strip())
    statuses = frozenset(v for v in qp.getlist("status") if v in STATUS_CHIP_KEYS)
    has_hltb = qp.get("has_hltb", "").lower() in ("true", "1", "yes", "on")
    include_bounced = qp.get("include_bounced", "").lower() in ("true", "1", "yes", "on")
    show_forever = qp.get("show_forever", "").lower() in ("true", "1", "yes", "on")

    # v3.5 polish — Dashboard pill click filters. Single-value via qp.get;
    # values are case-preserved as-typed (filtering is case-insensitive on
    # both sides). Empty / whitespace-only treated as unset.
    genre = (qp.get("genre") or "").strip()
    tag_pill = (qp.get("tag") or "").strip()
    developer = (qp.get("developer") or "").strip()

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
        show_forever=show_forever,
        sort_keys=sort_keys,
        genre=genre,
        tag_pill=tag_pill,
        developer=developer,
    )


# ---------------------------------------------------------------------------
# Eligibility / classification
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
    """Determine which section a game lives in, ignoring user filters.

    Order of resolution (precedence):
      forever wins over everything — Backlog is a progression surface and a
                                     forever game has no progress to track.
                                     This holds even for in_progress (the
                                     user already engages with active
                                     forever games via Steam recent activity
                                     and the game's own UI) and dropped-soft.
      dropped (soft) → its own section
      in_progress    → its own section
      played_unclassified → ratio-based subdivision
      never_played   → its own section
    """
    if is_forever_game(gws.game):
        return SECTION_FOREVER_GAMES

    s = gws.state.status

    if s == GameStatus.dropped and gws.state.dropped_strength == "soft":
        return SECTION_DROPPED_SOFT

    if s == GameStatus.in_progress:
        return SECTION_IN_PROGRESS

    if s == GameStatus.played_unclassified:
        ratio = playtime_ratio(gws)
        # ratio is None only if HLTB main is missing — but is_forever_game
        # already caught that above. Defensive: bucket as Barely Touched.
        if ratio is None:
            return SECTION_BARELY_TOUCHED
        if ratio >= RATIO_LIKELY_FINISHED:
            return SECTION_LIKELY_FINISHED
        if ratio >= RATIO_FINISHING_LATE:
            return SECTION_FINISHING_LATE
        if ratio >= RATIO_BARELY_TOUCHED:
            return SECTION_IN_PROGRESS_UNCONFIRMED
        return SECTION_BARELY_TOUCHED

    if s == GameStatus.never_played:
        return SECTION_NEVER_PLAYED

    return None


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

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


def _passes_genre(gws: GameWithState, genre: str) -> bool:
    """Case-insensitive match against game.genre_list. Empty filter passes
    everything; non-empty matches games whose comma-separated Steam
    genres include the value (any-of semantics for multi-genre games)."""
    if not genre:
        return True
    needle = genre.lower()
    return any(g.lower() == needle for g in gws.game.genre_list())


def _passes_tag_pill(gws: GameWithState, tag_pill: str) -> bool:
    """Single-value tag filter from a Dashboard pill click. Distinct from
    the multi-value chip-tags filter above — both layer as AND. Same
    case-insensitive comparison the chip filter uses."""
    if not tag_pill:
        return True
    needle = tag_pill.lower()
    return any(t.lower() == needle for t in gws.game.user_tags_list())


def _passes_developer(gws: GameWithState, developer: str) -> bool:
    """Case-insensitive equality match against game.developer."""
    if not developer:
        return True
    return (gws.game.developer or "").lower() == developer.lower()


def _passes_filters(gws: GameWithState, section: str, filters: BacklogFilters) -> bool:
    """Return True if this (gws, section) survives the user's filters.

    Visibility gates (forever / dropped) are checked first, then chip-based
    section narrowing, then attribute filters.
    """
    if section == SECTION_FOREVER_GAMES and not filters.show_forever:
        return False
    if section == SECTION_DROPPED_SOFT:
        if not (filters.include_bounced or "dropped_soft" in filters.statuses):
            return False

    if filters.statuses:
        allowed = set()
        for chip in filters.statuses:
            allowed.update(STATUS_CHIP_TO_SECTIONS.get(chip, ()))
        if section not in allowed:
            return False

    if not _passes_time_fit(gws, filters.time_fit):
        return False
    if not _passes_tags(gws, filters.tags):
        return False
    if not _passes_has_hltb(gws, filters.has_hltb):
        return False
    if not _passes_genre(gws, filters.genre):
        return False
    if not _passes_tag_pill(gws, filters.tag_pill):
        return False
    if not _passes_developer(gws, filters.developer):
        return False
    return True


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
        SECTION_LIKELY_FINISHED: "completion",
        SECTION_FINISHING_LATE: "completion",
        SECTION_IN_PROGRESS_UNCONFIRMED: "completion",
        SECTION_BARELY_TOUCHED: "playtime",
        SECTION_NEVER_PLAYED: "affinity",
        SECTION_FOREVER_GAMES: "playtime",
        SECTION_DROPPED_SOFT: "recently_dropped",
    }[section]


def _sort_rows(rows: list, section: str, sort_key: str, affinities: dict) -> list:
    if sort_key == "default":
        sort_key = _default_sort_key(section)

    if sort_key == "title":
        return sorted(rows, key=lambda g: g.game.name.lower())
    if sort_key == "hltb_main":
        return sorted(rows, key=lambda g: (g.game.hltb_main_hours is None, -(g.game.hltb_main_hours or 0)))
    if sort_key == "playtime":
        return sorted(rows, key=lambda g: -g.game.playtime_minutes)
    if sort_key == "recently_added":
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

def compute_header_stats(classified: list) -> HeaderStats:
    """classified: list of (gws, section_key) — pre-classified default-section games."""
    counts = {key: 0 for key in DEFAULT_SECTIONS}
    total_hours = 0.0
    unknown = 0
    for gws, section in classified:
        if section in DEFAULT_SECTIONS:
            counts[section] += 1
        h = gws.game.hltb_main_hours
        if h is None:
            unknown += 1
        elif section in DEFAULT_SECTIONS:
            total_hours += h
    return HeaderStats(
        total_count=sum(counts.values()),
        total_hours=total_hours,
        games_with_unknown_hltb=unknown,
        in_progress_count=counts[SECTION_IN_PROGRESS],
        likely_finished_count=counts[SECTION_LIKELY_FINISHED],
        finishing_late_count=counts[SECTION_FINISHING_LATE],
        in_progress_unconfirmed_count=counts[SECTION_IN_PROGRESS_UNCONFIRMED],
        barely_touched_count=counts[SECTION_BARELY_TOUCHED],
        never_played_count=counts[SECTION_NEVER_PLAYED],
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
    # Classify every game once. Skip globally-excluded ones.
    classified: list = []
    for g in games:
        if _is_globally_excluded(g):
            continue
        section = _section_for_game(g)
        if section is None:
            continue
        classified.append((g, section))

    # Header stats + top tags from default-visible sections only — Forever and
    # dropped-soft don't inflate the headline backlog count.
    default_classified = [(g, s) for g, s in classified if s in DEFAULT_SECTIONS]
    stats = compute_header_stats(default_classified)
    top_tags = top_user_tags([g for g, _ in default_classified], n=15)

    # Apply filters and bucket into sections.
    section_buckets: dict = {key: [] for key in ALL_SECTIONS}
    for gws, section in classified:
        if not _passes_filters(gws, section, filters):
            continue
        section_buckets[section].append(gws)

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

    is_empty_no_filters = (
        stats.total_count == 0
        and not filters.show_forever
        and not filters.include_bounced
    )

    pill_kind, pill_value = filters.pill_kind_value()
    # Pill-only-empty: the pill filter is the SOLE active filter, the
    # backlog itself isn't empty, and the result has zero matches. Drives
    # the targeted "No games match [genre: Action]" copy. When pill is
    # combined with chip filters, fall back to existing multi-filter
    # empty-state — same precedence pattern as Library badge filter.
    is_empty_pill_only = (
        not sections
        and stats.total_count > 0
        and pill_kind is not None
        and not filters.chip_filters_active()
    )

    return BacklogView(
        sections=sections,
        header_stats=stats,
        top_tags=top_tags,
        contradictory_filter_warning=warning,
        is_empty_no_filters=is_empty_no_filters,
        is_empty_due_to_filters=(not sections and stats.total_count > 0),
        pill_filter_kind=pill_kind,
        pill_filter_value=pill_value,
        is_empty_pill_only=is_empty_pill_only,
    )
