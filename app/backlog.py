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

# User-tag set treated as "forever-style" when HLTB main is missing.
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

    def is_active(self) -> bool:
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


# ---------------------------------------------------------------------------
# Forever-game detection
# ---------------------------------------------------------------------------

def is_forever_game(game) -> bool:
    """Detect games with no defined end state.

    Conditions (any one triggers):
      1. HLTB main is null or zero — no story to finish
      2. HLTB completionist > 7x HLTB main — game is mostly side content / grind.
         Tuned up from 5x: at 5x, structurally finishable games with plentiful
         optional content (e.g. Aimlabs at 5.3x) got swept in; 7x catches the
         truly endless games while leaving those alone.
      3. User tags include a forever-style tag AND HLTB main is missing
         (subsumed by #1 in practice; kept for spec literalness)
      4. Steam categories show Multi-player without Single-player
    """
    h = game.hltb_main_hours
    if h is None or h <= 0:
        return True

    if game.hltb_completionist_hours and h > 0:
        if game.hltb_completionist_hours > 7 * h:
            return True

    user_tags = {t.lower() for t in game.user_tags_list()}
    if (FOREVER_USER_TAGS & user_tags) and game.hltb_main_hours is None:
        return True

    cats = {t.lower() for t in game.tag_list()}
    has_multiplayer = "multi-player" in cats
    has_singleplayer = "single-player" in cats
    if has_multiplayer and not has_singleplayer:
        return True

    return False


# Game-type values used by compute_game_type and the Library Type column.
# String constants instead of an enum so templates can dispatch via dict
# lookup without importing the enum.
GAME_TYPE_LINEAR = "linear"
GAME_TYPE_MULTIPLAYER = "multiplayer"
GAME_TYPE_NO_ENDPOINT = "no_endpoint"
GAME_TYPE_MIXED = "mixed"

# Short labels used by the Library Type column. Long forms ("Multiplayer
# focus", "No defined endpoint") are reserved for hover tooltips so the
# column doesn't crowd at narrow widths.
GAME_TYPE_LABELS = {
    GAME_TYPE_LINEAR: "Linear",
    GAME_TYPE_MULTIPLAYER: "Multiplayer",
    GAME_TYPE_NO_ENDPOINT: "No endpoint",
    GAME_TYPE_MIXED: "Mixed",
}

GAME_TYPE_TOOLTIPS = {
    GAME_TYPE_LINEAR: "Linear / story-driven — has a defined main path",
    GAME_TYPE_MULTIPLAYER: "Multiplayer focus — no single-player campaign",
    GAME_TYPE_NO_ENDPOINT: "No defined endpoint — sandbox, roguelike, or open-ended",
    GAME_TYPE_MIXED: "Mixed — single-player campaign with multiplayer modes (Halo, Borderlands)",
}

# Tag substrings that classify a game as no_endpoint on their own,
# independent of HLTB signals. Roguelike / Roguelite games are
# definitionally endless by structure (procedural, infinite runs);
# their tags vary in form ("Roguelike" / "Rogue-like" / "Rogue-lite"
# / "Action Roguelike"), so we substring-match against "rogue".
#
# Open World, Sandbox, and MMO are deliberately NOT here: they describe
# games with clear endpoints (Witcher 3, Elden Ring, Skyrim) often
# enough that treating them as standalone no_endpoint signals
# misclassifies linear games. The HLTB-null and completionist-ratio
# standalone rules already capture the genuinely-endless cases (Stardew
# at 7x+ completionist, MMOs typically HLTB-null), so Open World /
# Sandbox / MMO tags don't need their own trigger.
_STANDALONE_NO_ENDPOINT_TAG_SUBSTRINGS = ("rogue",)


def _has_standalone_no_endpoint_tag(game) -> bool:
    for tag in game.user_tags_list():
        tag_lower = tag.lower()
        for needle in _STANDALONE_NO_ENDPOINT_TAG_SUBSTRINGS:
            if needle in tag_lower:
                return True
    return False


def compute_game_type(game) -> str:
    """Classify a game as linear / multiplayer / no_endpoint.

    Rules:
      multiplayer  — Multi-player Steam category AND no Single-player category
      no_endpoint  — any of:
                     - HLTB main is missing/zero (no main story to track)
                     - HLTB completionist > 7x HLTB main (definitionally endless;
                       same 7x threshold as is_forever_game)
                     - user_tags include a Roguelike/Roguelite variant
                       (substring "rogue" — definitionally endless by structure)
      linear       — fall-through: HLTB main exists, ratio under 7x, no rogue
                     tag. Multiplayer Steam category alongside Single-player
                     does NOT push to a separate "mixed" bucket — Steam fires
                     Multi-player for almost any game with co-op or leaderboards
                     (Elden Ring, Witcher 3, DS3 all have it), so a separate
                     Mixed bucket would just collect mostly-linear games.

    The GAME_TYPE_MIXED constant is preserved (it has CSS / template
    bindings) but compute_game_type no longer returns it. If a future
    refinement re-introduces Mixed, the bindings are ready to use.

    Open World, Sandbox, and MMO tags do NOT trigger no_endpoint on their
    own (refinement after Elden Ring / Witcher 3 / Skyrim were misclassified).
    Genuinely-endless games carrying those tags hit the HLTB-null or
    completionist-ratio rules anyway (Stardew at 7x+ completionist; MMOs
    are typically HLTB-null). Roguelike/Roguelite stay standalone because
    they're definitionally endless regardless of HLTB.
    """
    cats = {t.lower() for t in game.tag_list()}
    has_mp = "multi-player" in cats
    has_sp = "single-player" in cats

    if has_mp and not has_sp:
        return GAME_TYPE_MULTIPLAYER

    h = game.hltb_main_hours
    if h is None or h <= 0:
        return GAME_TYPE_NO_ENDPOINT
    if game.hltb_completionist_hours and game.hltb_completionist_hours > 7 * h:
        return GAME_TYPE_NO_ENDPOINT
    if _has_standalone_no_endpoint_tag(game):
        return GAME_TYPE_NO_ENDPOINT

    return GAME_TYPE_LINEAR


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
    tags = frozenset(v.strip().lower() for v in qp.getlist("tag") if v.strip())
    statuses = frozenset(v for v in qp.getlist("status") if v in STATUS_CHIP_KEYS)
    has_hltb = qp.get("has_hltb", "").lower() in ("true", "1", "yes", "on")
    include_bounced = qp.get("include_bounced", "").lower() in ("true", "1", "yes", "on")
    show_forever = qp.get("show_forever", "").lower() in ("true", "1", "yes", "on")

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

    return BacklogView(
        sections=sections,
        header_stats=stats,
        top_tags=top_tags,
        contradictory_filter_warning=warning,
        is_empty_no_filters=is_empty_no_filters,
        is_empty_due_to_filters=(not sections and stats.total_count > 0),
    )
