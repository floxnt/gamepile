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


def compute_cliff_metric(achievements: list) -> Optional[float]:
    """Return the largest pct-point gap between consecutive achievements
    (sorted by percent descending) after discarding the top N as launch-
    achievement noise.

    None if fewer than _CLIFF_MIN_POST_DISCARD entries remain after
    discard — too few to identify a meaningful drop.
    """
    if not achievements:
        return None
    # Sort defensively in case the caller passed in arbitrary order.
    sorted_aches = sorted(achievements, key=lambda a: -a["percent"])
    discard = _discard_count_for(len(sorted_aches))
    remaining = sorted_aches[discard:]
    if len(remaining) < _CLIFF_MIN_POST_DISCARD:
        return None

    largest_gap = 0.0
    for i in range(len(remaining) - 1):
        gap = remaining[i]["percent"] - remaining[i + 1]["percent"]
        if gap > largest_gap:
            largest_gap = gap
    return largest_gap


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
