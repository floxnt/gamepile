"""
Hook-point Phase 1a — pure-functional engagement-signal computations.

Five metrics, all per-game, all nullable when the underlying data is
insufficient. Phase 1a stores the raw numbers; Phase 1b will combine them
into a categorical "stickiness" badge after threshold tuning.

No DB / FastAPI imports — keep this layer testable in isolation.
"""

import statistics
from typing import Optional


# Permissive matching pattern set — substrings that count as a
# story-completion candidate. Match runs against BOTH the achievement's
# displayName (when schema-resolved) AND its internal name, so games that
# carry the signal in either field get caught.
STORY_COMPLETION_PATTERNS: tuple = (
    "complete",
    "finish",
    "ending",
    "credits",
    "the end",
    "epilogue",
    "final",
)

# Strict pattern set used to label completion_rate confidence as "high".
# Narrower than STORY_COMPLETION_PATTERNS — only patterns where a match
# is almost certainly a story endpoint, not collection completion or
# difficulty mastery. Checked against displayName only (where the
# user-visible meaning lives) and only when the unlock % is at or below
# STRONG_MATCH_PCT_CAP — anything higher is a launch / early achievement.
STRONG_COMPLETION_PATTERNS: tuple = (
    "ending",
    "endings",
    "credits",
    "the end",
    "epilogue",
    "finished the game",
    "beat the game",
)
STRONG_MATCH_PCT_CAP = 50.0

# Minimum reviews required for review-derived metrics to be meaningful.
MIN_REVIEWS_FOR_STATS = 10

# Stickiness threshold: reviewers with playtime_at_review at or above
# STICKY_HLTB_FRACTION × HLTB-main-in-minutes count as "sticky" — i.e.,
# they played at least half the main story before reviewing, which is a
# reasonable proxy for "informed opinion" that scales with game length.
# A flat 20-hour cutoff is arbitrary across very different games (a 4h
# walking sim's reviewer at 6h is sticky; a 200h CRPG's reviewer at 25h
# is barely past the prologue).
STICKY_HLTB_FRACTION = 0.5

# Fallback for games without HLTB main data (~9% of the library): use a
# flat 20-hour cutoff so we still emit a stickiness number rather than
# NULL.
STICKY_PLAYTIME_THRESHOLD_MIN = 1200

# Cliff-metric discard rules — discard top N achievements (which nearly
# everyone gets at game start) before computing the largest gap.
# (achievement_count_minimum, discard_count) — first matching entry wins.
_CLIFF_DISCARD_RULES = (
    (10, 3),  # ≥10 achievements: discard top 3
    (5, 2),   # 5-9 achievements: discard top 2
    (0, 0),   # <5 achievements: no discard
)
_CLIFF_MIN_POST_DISCARD = 4  # need at least 4 entries after discard to compute a gap


# ---------------------------------------------------------------------------
# Story completion + completion_rate
# ---------------------------------------------------------------------------

def _matches_any(text: str, patterns) -> bool:
    """Case-insensitive substring match — text contains any of the patterns."""
    text_lower = text.lower()
    return any(p in text_lower for p in patterns)


def find_story_completion_achievement(achievements: list) -> Optional[dict]:
    """Find the achievement that best represents story completion.

    Heuristic: case-insensitive substring match against
    STORY_COMPLETION_PATTERNS. Checks BOTH `displayName` (when present
    via GetSchemaForGame) AND the internal `name` — games that name a
    story-completion achievement descriptively in either field get
    caught. Hades's `AchReachedEpilogue` (display "One for the Ages")
    is the canonical case where internal-name matching matters.

    If multiple achievements match, return the one with the LOWEST
    unlock percent (the most-completion-y signal — fewer players get
    to the end).

    Returns the matching achievement dict, or None if no candidate
    matches and caller should use the lowest-overall-% fallback.
    """
    if not achievements:
        return None
    matches: list = []
    for ach in achievements:
        display = ach.get("displayName") or ""
        name = ach.get("name") or ""
        if _matches_any(display, STORY_COMPLETION_PATTERNS) or _matches_any(name, STORY_COMPLETION_PATTERNS):
            matches.append(ach)
    if not matches:
        return None
    return min(matches, key=lambda a: a["percent"])


def compute_completion_rate(achievements: list) -> Optional[float]:
    """Return completion_rate in [0.0, 1.0], or None for empty list.

    Prefers the heuristic story-completion match; falls back to the
    lowest-percent achievement overall when no pattern matches (every
    game has *some* lowest achievement, and that's a reasonable proxy
    for "rare endgame thing" when we can't identify the credits one).
    """
    if not achievements:
        return None
    match = find_story_completion_achievement(achievements)
    if match is not None:
        return match["percent"] / 100.0
    # Fallback: lowest percent overall.
    lowest = min(achievements, key=lambda a: a["percent"])
    return lowest["percent"] / 100.0


def compute_completion_rate_confidence(achievements: list) -> Optional[str]:
    """Return 'high' / 'low' / None for the completion_rate confidence flag.

    None when achievements is empty (no completion_rate computed).
    'high' when the heuristic matched an achievement whose displayName
    contains a STRONG_COMPLETION_PATTERNS word AND the unlock percent is
    at or below STRONG_MATCH_PCT_CAP (anything higher is a launch
    achievement, not a story endpoint).
    'low' otherwise — fallback was used, the match's displayName uses
    only weak words ("complete" / "final"), or the unlock % is too high.

    Phase 1b will use this to weight 'high'-confidence completion_rate
    differently from 'low' when computing the categorical stickiness
    signal — strong matches contribute, weak/fallback matches get
    discounted or ignored.
    """
    if not achievements:
        return None
    match = find_story_completion_achievement(achievements)
    if match is None:
        # Fallback path — completion_rate from lowest-overall — never high.
        return "low"
    if match["percent"] > STRONG_MATCH_PCT_CAP:
        # Above the cap, even a strong-pattern match is a launch/early
        # achievement, not a story endpoint. Downgrade to low.
        return "low"
    # Strong-pattern check on displayName specifically — that's where the
    # user-visible meaning lives. Internal-ID matches (e.g. Hades's
    # "AchReachedEpilogue" with displayName "One for the Ages") give
    # 'low' confidence by design: we caught it but the user-facing text
    # doesn't carry an unambiguous completion signal.
    display = match.get("displayName") or ""
    if _matches_any(display, STRONG_COMPLETION_PATTERNS):
        return "high"
    return "low"


# ---------------------------------------------------------------------------
# Cliff metric
# ---------------------------------------------------------------------------

def _discard_count_for(n: int) -> int:
    for threshold, discard in _CLIFF_DISCARD_RULES:
        if n >= threshold:
            return discard
    return 0


def _largest_cliff(achievements: list):
    """Locate the largest cliff in the post-discard achievements list.

    Returns (cliff_index, gap_size, n_remaining) or None if data is
    insufficient. Single source of truth for compute_cliff_metric and
    compute_cliff_position so they always agree on the populated
    envelope. Tie-break: when multiple cliffs share the largest size,
    the earliest index wins (gap > strict comparison).
    """
    if not achievements:
        return None
    sorted_aches = sorted(achievements, key=lambda a: -a["percent"])
    discard = _discard_count_for(len(sorted_aches))
    remaining = sorted_aches[discard:]
    n = len(remaining)
    if n < _CLIFF_MIN_POST_DISCARD:
        return None
    best_i = 0
    best_gap = 0.0
    for i in range(n - 1):
        gap = remaining[i]["percent"] - remaining[i + 1]["percent"]
        if gap > best_gap:
            best_gap = gap
            best_i = i
    return (best_i, best_gap, n)


def compute_cliff_metric(achievements: list) -> Optional[float]:
    """Return the largest pct-point gap between consecutive achievements
    (sorted by percent descending) after discarding the top N as launch-
    achievement noise.

    None if fewer than _CLIFF_MIN_POST_DISCARD entries remain after
    discard — too few to identify a meaningful drop.
    """
    result = _largest_cliff(achievements)
    if result is None:
        return None
    _, gap, _ = result
    return gap


def compute_cliff_position(achievements: list) -> Optional[float]:
    """Position of the largest cliff in the sorted achievement list,
    normalized to [0.0, 1.0].

    0.0 = first cliff (top of the sorted-descending list, highest unlock
    percent side, early-game). 1.0 = last cliff (bottom of the list,
    least-unlocked, endgame side). Mirrors compute_cliff_metric's
    populated envelope exactly — both functions return None for the
    same inputs.

    Formula: i / (n - 2) where i is the index of the largest cliff in
    the post-discard list and n is the post-discard length. With the
    minimum n=4 entries, possible positions are 0.0, 0.5, 1.0; larger
    n yields finer granularity.
    """
    result = _largest_cliff(achievements)
    if result is None:
        return None
    i, _, n = result
    if n - 2 <= 0:
        # _CLIFF_MIN_POST_DISCARD=4 makes this branch unreachable today;
        # keep the guard so a future minimum-discard relaxation can't
        # introduce a divide-by-zero.
        return None
    return i / (n - 2)


# ---------------------------------------------------------------------------
# Review-derived metrics
# ---------------------------------------------------------------------------

def compute_review_playtime_median(playtimes_min: list) -> Optional[int]:
    """Median of per-review playtime (in minutes). None if < MIN_REVIEWS_FOR_STATS."""
    if len(playtimes_min) < MIN_REVIEWS_FOR_STATS:
        return None
    return int(statistics.median(playtimes_min))


def compute_stickiness_ratio(
    playtimes_min: list,
    hltb_main_hours: Optional[float] = None,
) -> Optional[float]:
    """Fraction of reviewers whose playtime_at_review meets the sticky threshold.

    Threshold scales with HLTB main when available:
      - hltb_main_hours present → STICKY_HLTB_FRACTION (0.5) × hltb_main_hours × 60
        ("reviewer played at least half the main story before reviewing")
      - hltb_main_hours None / 0 → fallback to STICKY_PLAYTIME_THRESHOLD_MIN (1200)
        (flat 20-hour cutoff; the metric stays useful for the ~9% of games
         we lack HLTB main for, rather than returning NULL)

    None when fewer than MIN_REVIEWS_FOR_STATS reviews — too few to be
    meaningful regardless of threshold choice.
    """
    if len(playtimes_min) < MIN_REVIEWS_FOR_STATS:
        return None
    if hltb_main_hours and hltb_main_hours > 0:
        threshold_min = STICKY_HLTB_FRACTION * hltb_main_hours * 60
    else:
        threshold_min = STICKY_PLAYTIME_THRESHOLD_MIN
    sticky = sum(1 for p in playtimes_min if p >= threshold_min)
    return sticky / len(playtimes_min)


# ---------------------------------------------------------------------------
# SteamSpy ratio
# ---------------------------------------------------------------------------

def compute_playtime_median_avg_ratio(
    median_forever: Optional[int],
    average_forever: Optional[int],
) -> Optional[float]:
    """median / average from SteamSpy's lifetime-playtime stats.

    None if either is missing or zero. High ratio (~1.0) → even
    distribution. Low ratio (<<1) → long-tail bounce pattern (most
    players bounce off, hardcore minority pulls the average up).
    """
    if not median_forever or not average_forever:
        return None
    if average_forever <= 0:
        return None
    return median_forever / average_forever


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def qualitative_ratio_hint(ratio: Optional[float]) -> str:
    """Short descriptive text for the median:avg ratio. Phase 1b will
    replace this with the categorical stickiness signal."""
    if ratio is None:
        return ""
    if ratio < 0.4:
        return "long-tail pattern"
    if ratio < 0.7:
        return "uneven engagement"
    return "even engagement"


# ---------------------------------------------------------------------------
# Phase 1b — categorical stickiness signal
# ---------------------------------------------------------------------------

# Per-metric thresholds. Tuned from the Phase 1a distribution report:
# - cliff median 7.1pp, ~4% above 20pp → CLIFF_FILTERS_THRESHOLD picks
#   the long-tail
# - stickiness median 0.81 across the populated library → upper threshold
#   0.90 selects "very sticky" titles, lower threshold 0.50 selects
#   "reviewers commonly bounce" titles
# - completion median ~0.07 across high-confidence matches; 0.15 / 0.03
#   bracket the high-confidence distribution
CLIFF_FILTERS_THRESHOLD = 20.0
STICKINESS_STICKY_THRESHOLD = 0.90
STICKINESS_FILTERS_THRESHOLD = 0.50
COMPLETION_STICKY_THRESHOLD = 0.15
COMPLETION_FILTERS_THRESHOLD = 0.03

# Per-metric contribution values returned by categorize_*. The combined
# signal logic counts SIGNAL_STICKY / SIGNAL_FILTERS_HARD across the
# metrics that the game type's display rules surface.
SIGNAL_STICKY = "sticky"
SIGNAL_FILTERS_HARD = "filters_hard"
SIGNAL_NEUTRAL = "neutral"
SIGNAL_NO_DATA = "no_data"

# Combined badge values surfaced to the user via compute_stickiness_signal.
BADGE_STICKY = "sticky"
BADGE_AVERAGE = "average"
BADGE_FILTERS_HARD = "filters_hard"
BADGE_INSUFFICIENT_DATA = "insufficient_data"


def categorize_cliff(cliff_metric: Optional[float]) -> str:
    """Cliff has no SIGNAL_STICKY contribution by design — a small cliff
    isn't a positive signal on its own, just absence of a negative one."""
    if cliff_metric is None:
        return SIGNAL_NO_DATA
    if cliff_metric >= CLIFF_FILTERS_THRESHOLD:
        return SIGNAL_FILTERS_HARD
    return SIGNAL_NEUTRAL


def categorize_stickiness(stickiness_ratio: Optional[float]) -> str:
    if stickiness_ratio is None:
        return SIGNAL_NO_DATA
    if stickiness_ratio >= STICKINESS_STICKY_THRESHOLD:
        return SIGNAL_STICKY
    if stickiness_ratio <= STICKINESS_FILTERS_THRESHOLD:
        return SIGNAL_FILTERS_HARD
    return SIGNAL_NEUTRAL


def categorize_completion(
    completion_rate: Optional[float],
    confidence: Optional[str],
) -> str:
    """Only contributes a non-no_data value when confidence == 'high'.
    Low-confidence matches and fallback-derived values collapse to
    SIGNAL_NO_DATA — the heuristic isn't trusted enough to feed the
    categorical signal when the displayName doesn't carry an unambiguous
    completion word."""
    if completion_rate is None or confidence != "high":
        return SIGNAL_NO_DATA
    if completion_rate >= COMPLETION_STICKY_THRESHOLD:
        return SIGNAL_STICKY
    if completion_rate <= COMPLETION_FILTERS_THRESHOLD:
        return SIGNAL_FILTERS_HARD
    return SIGNAL_NEUTRAL


def compute_stickiness_signal(game) -> tuple:
    """Combine per-metric signals into the categorical badge.

    Returns (badge_label, sticky_count, filters_hard_count). The two
    counts cover the contributing metrics — the template uses them with
    engagement_display_rules to render "X of Y signals support this".

    Honors the game-type display rules:
      - Types with categorical_badge_eligible=False (beta_playtest,
        early_access, unknown, software) always return
        BADGE_INSUFFICIENT_DATA.
      - Types where show_completion_rate / show_cliff_metric are False
        (multiplayer, mmo, no_endpoint, sandbox) reduce to a stickiness-
        only single-signal evaluation.
      - Other eligible types (linear, mixed, expansion) evaluate all
        three.

    Imports app.game_type lazily to avoid a top-level cycle if game_type
    later picks up a hook_metrics dep.
    """
    from app.game_type import engagement_display_rules, resolve_type

    gt = game.game_type or resolve_type(game)
    rules = engagement_display_rules(gt)

    if not rules["categorical_badge_eligible"]:
        return (BADGE_INSUFFICIENT_DATA, 0, 0)

    contributions: list = []
    if rules["show_cliff_metric"]:
        contributions.append(categorize_cliff(game.cliff_metric))
    if rules["show_stickiness_ratio"]:
        contributions.append(categorize_stickiness(game.stickiness_ratio))
    if rules["show_completion_rate"]:
        contributions.append(categorize_completion(
            game.completion_rate, game.completion_rate_confidence,
        ))

    total = len(contributions)
    if total == 0:
        return (BADGE_INSUFFICIENT_DATA, 0, 0)

    sticky_count = sum(1 for c in contributions if c == SIGNAL_STICKY)
    filters_hard_count = sum(1 for c in contributions if c == SIGNAL_FILTERS_HARD)
    no_data_count = sum(1 for c in contributions if c == SIGNAL_NO_DATA)

    # Generalize spec's "2 of 3" to ceil(total/2). Single-signal path
    # (multiplayer/mmo/no_endpoint/sandbox) collapses cleanly: 1 of 1
    # no_data → Insufficient; 1 of 1 sticky/filters → that verdict.
    threshold = (total + 1) // 2

    if no_data_count >= threshold:
        return (BADGE_INSUFFICIENT_DATA, sticky_count, filters_hard_count)
    if sticky_count >= threshold and filters_hard_count == 0:
        return (BADGE_STICKY, sticky_count, filters_hard_count)
    if filters_hard_count >= threshold and sticky_count == 0:
        return (BADGE_FILTERS_HARD, sticky_count, filters_hard_count)

    return (BADGE_AVERAGE, sticky_count, filters_hard_count)
