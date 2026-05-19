"""
Hook-point Phase 1a — pure-functional engagement-signal computations.

RETAINED DORMANT (v0.7.0). The hook-point / stickiness signal was removed
from the live UI in v0.7.0 because it overpromised on low-confidence
inference: ~90% of stored completion_rate values self-labeled as "low"
confidence (see the empirical achievement-signal probe). This module is
preserved in place — pipeline functions, badge taxonomy, threshold
constants — but is no longer called by any live code path (sync, routes,
templates). Data already in the DB (completion_rate / cliff_metric /
cliff_position / review_playtime_median / stickiness_ratio /
playtime_median_avg_ratio / stickiness_badge_manual /
completion_achievement_name_manual) is preserved intact via upsert
COALESCE; columns are unchanged. See SPEC_HOOK_RETIREMENT.md for the
removal rationale and the reevaluation intent (~v1.0 when more data
exists). Tests that exercise these functions remain green as a
correctness guarantee — do NOT delete on a future cleanup pass.

Five metrics, all per-game, all nullable when the underlying data is
insufficient. Phase 1a stores the raw numbers; Phase 1b/1c combined them
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


def pick_completion_achievement(
    achievements: list,
    manual_name: Optional[str],
) -> Optional[dict]:
    """Locate a specific achievement by its internal `name` in a list
    returned by fetch_achievements_with_metadata.

    Returns the matching achievement dict, or None when manual_name is
    None / empty, or when no entry in the list matches. Used by
    app/sync.py's achievement phase and by the Game Detail completion-
    override route to resolve a user-chosen achievement to its current
    unlock percent.

    The internal name is the stable Steam ID (not displayName, which can
    be themed or change with localisation).
    """
    if not manual_name or not achievements:
        return None
    for ach in achievements:
        if ach.get("name") == manual_name:
            return ach
    return None


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
# Phase 1c — weighted-score categorical signal
# ---------------------------------------------------------------------------

# Per-metric thresholds. cliff/stickiness numbers carried over from
# Phase 1b (still empirically tuned to the live distribution); completion
# thresholds recalibrated against published Steam-population data
# (Bailey & Miyata 2019: median completion ≈10%, mean ≈14%, high-completion
# outliers reach 50–60%) — the Phase 1b 0.15 sticky threshold was
# "marginally above mean," not actually sticky.
CLIFF_FILTERS_THRESHOLD = 20.0
STICKINESS_STICKY_THRESHOLD = 0.90
STICKINESS_FILTERS_THRESHOLD = 0.50
COMPLETION_STICKY_THRESHOLD = 0.25
COMPLETION_FILTERS_THRESHOLD = 0.05

# Cliff position bands. Position ≤ 0.30 = early-game cliff (filter
# signal); ≥ 0.70 = late-game / completionist gate (NOT a filter — these
# are rare endgame achievements, not abandonment patterns).
CLIFF_EARLY_POSITION_MAX = 0.30
CLIFF_LATE_POSITION_MIN = 0.70

# Score weights. Stickiness is the most-populated and most-reliable
# signal. Cliff carries position information and is moderately
# trustworthy. Completion gets a haircut at low confidence — the
# heuristic match isn't certain, so the signal contributes less.
WEIGHT_STICKINESS = 1.5
WEIGHT_CLIFF = 1.0
WEIGHT_COMPLETION_HIGH = 0.7
WEIGHT_COMPLETION_LOW = 0.3

# Composite-score thresholds. Hooks remains symmetric to stickiness +1
# weight (+1.5 reachable from stickiness alone). Filters threshold is
# asymmetric at -1.0 because cliff is structurally one-sided — it only
# pushes toward filters (late large cliffs route to neutral, never
# positive). Lowering the negative threshold lets a -1 cliff stand on
# its own at score -1.0, parallel to stickiness +1 being sufficient
# for Hooks. The size + position guards inside signal_value_cliff
# already filter for "meaningful signal" (≥ 20pp drop, early or mid
# position) before -1 is emitted.
SCORE_HOOKS_THRESHOLD = 1.5
SCORE_FILTERS_THRESHOLD = -1.0

# Sub-bands inside the middle score range for splitting Standard by lean.
# Used only when no qualifying strong signal is present (Mixed signals
# precedence still applies). The asymmetry mirrors SCORE_FILTERS_THRESHOLD:
# +0.5 sits at one-third of the +1.5 Hooks threshold; -0.5 sits at half of
# the -1.0 Filters threshold. Score exactly at ±0.5 lands in the lean
# bucket — Standard is reserved for the truly-neutral middle.
SCORE_USUALLY_HOOKS_MIN = 0.5
SCORE_OFTEN_FILTERS_MAX = -0.5

# Marathon thresholds. High engagement + low confirmed completion =
# open-ended / sandbox-y games people play forever without finishing.
# Restricted to high-confidence completion so sparse-data games can't
# earn the label off noisy heuristic estimates.
MARATHON_PLAYTIME_MIN_HOURS = 50.0
MARATHON_COMPLETION_MAX = 0.10

# Combined-badge values surfaced to the user.
BADGE_HOOKS_PLAYERS = "hooks_players"
BADGE_USUALLY_HOOKS = "usually_hooks"
BADGE_FILTERS_EARLY = "filters_early"
BADGE_OFTEN_FILTERS = "often_filters"
BADGE_MARATHON = "marathon"
BADGE_MIXED_SIGNALS = "mixed_signals"
BADGE_STANDARD_ENGAGEMENT = "standard_engagement"
BADGE_LIMITED_DATA = "limited_data"

# Badges valid for manual override. Limited_data is excluded — manually
# asserting "no signal" is meaningless; clearing the override is the right
# way to revert. The Game Detail route validates against this set before
# persisting. Order is also the dropdown order (library + game detail
# override picker): each lean slots next to its strong category.
ACTIVE_BADGES = (
    BADGE_HOOKS_PLAYERS,
    BADGE_USUALLY_HOOKS,
    BADGE_FILTERS_EARLY,
    BADGE_OFTEN_FILTERS,
    BADGE_MARATHON,
    BADGE_MIXED_SIGNALS,
    BADGE_STANDARD_ENGAGEMENT,
)

BADGE_LABELS = {
    BADGE_HOOKS_PLAYERS:       "Hooks players",
    BADGE_USUALLY_HOOKS:       "Usually hooks",
    BADGE_FILTERS_EARLY:       "Filters early",
    BADGE_OFTEN_FILTERS:       "Often filters",
    BADGE_MARATHON:            "Marathon",
    BADGE_MIXED_SIGNALS:       "Mixed signals",
    BADGE_STANDARD_ENGAGEMENT: "Standard engagement",
    BADGE_LIMITED_DATA:        "Limited data",
}

BADGE_TOOLTIPS = {
    BADGE_HOOKS_PLAYERS:
        "Most reviewers play deep into the game; cliff patterns suggest engagement holds",
    BADGE_USUALLY_HOOKS:
        "Leans positive — engagement signals tilt sticky but not strong enough for Hooks players",
    BADGE_FILTERS_EARLY:
        "Many players abandon in the early or mid-game",
    BADGE_OFTEN_FILTERS:
        "Leans negative — engagement signals tilt toward filtering but not strong enough for Filters early",
    BADGE_MARATHON:
        "High engagement, low completion — the kind of game people play forever without finishing",
    BADGE_MIXED_SIGNALS:
        "Strong signals in different directions — taste-dependent",
    BADGE_STANDARD_ENGAGEMENT:
        "Middle-of-the-road metrics across the board",
    BADGE_LIMITED_DATA:
        "Not enough data to characterise engagement",
}


# ---------------------------------------------------------------------------
# Per-signal value helpers — each returns -1 / 0 / +1
# ---------------------------------------------------------------------------

def signal_value_stickiness(stickiness_ratio: Optional[float]) -> int:
    """+1 above sticky threshold, -1 at-or-below filters, 0 between or NULL."""
    if stickiness_ratio is None:
        return 0
    if stickiness_ratio >= STICKINESS_STICKY_THRESHOLD:
        return 1
    if stickiness_ratio <= STICKINESS_FILTERS_THRESHOLD:
        return -1
    return 0


def signal_value_cliff(
    cliff_metric: Optional[float],
    cliff_position: Optional[float],
) -> int:
    """Cliff signal is position-aware. Large cliff (≥ 20pp) in early or
    mid position counts as a filter (-1). Large cliff in late position
    is a completionist gate, not abandonment — neutral (0). Small cliff
    or NULL: 0. Cliff never contributes positive signal."""
    if cliff_metric is None or cliff_metric < CLIFF_FILTERS_THRESHOLD:
        return 0
    # Position is None only when cliff_metric is None — populated
    # envelope is shared. Defensive guard for the impossible case:
    if cliff_position is None:
        return 0
    if cliff_position >= CLIFF_LATE_POSITION_MIN:
        return 0
    return -1


def signal_value_completion(completion_rate: Optional[float]) -> int:
    """+1 / -1 / 0 by completion threshold. Same thresholds for both
    confidence levels — weight differs at the score-aggregation layer."""
    if completion_rate is None:
        return 0
    if completion_rate >= COMPLETION_STICKY_THRESHOLD:
        return 1
    if completion_rate <= COMPLETION_FILTERS_THRESHOLD:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Composite signal — weighted score + sub-classified middle bucket
# ---------------------------------------------------------------------------

def _stickiness_description(value: int, ratio: Optional[float]) -> str:
    if ratio is None:
        return "no data"
    if value == 1:
        return "high"
    if value == -1:
        return "low"
    return "neutral"


def _cliff_description(
    value: int,
    metric: Optional[float],
    position: Optional[float],
) -> str:
    if metric is None:
        return "no data"
    size_word = "large" if metric >= CLIFF_FILTERS_THRESHOLD else "small"
    if position is None:
        return f"{size_word}, position unknown"
    if position <= CLIFF_EARLY_POSITION_MAX:
        pos_word = "early"
    elif position >= CLIFF_LATE_POSITION_MIN:
        pos_word = "late"
    else:
        pos_word = "mid"
    return f"{pos_word} & {size_word}"


def _completion_description(
    value: int,
    rate: Optional[float],
    confidence: Optional[str],
) -> str:
    if rate is None:
        return "no data"
    conf_label = confidence if confidence in ("high", "low") else "no-conf"
    if value == 1:
        return f"{conf_label}-conf, sticky"
    if value == -1:
        return f"{conf_label}-conf, filters"
    return f"{conf_label}-conf, neutral"


def _qualifies_strong_signal(
    stickiness_value: int,
    cliff_value: int,
    high_conf_completion_value: int,
) -> bool:
    """Strong signal for the Mixed-vs-Standard split: stickiness, cliff,
    or HIGH-confidence completion at ±1. Low-confidence completion
    contributes to the score but is excluded here — too noisy to be the
    sole promoter from Standard to Mixed."""
    return any(v != 0 for v in (stickiness_value, cliff_value, high_conf_completion_value))


def compute_stickiness_signal(game) -> tuple:
    """Composite categorical signal. Returns (badge, score, breakdown).

    badge: one of the BADGE_* constants (hooks_players / usually_hooks /
        filters_early / often_filters / marathon / mixed_signals /
        standard_engagement / limited_data).
    score: float — sum of weighted per-signal contributions.
    breakdown: dict {signal_name: {value, weight, contribution, description}}.
        Used by Game Detail to render the per-signal score breakdown line.

    Honors the game-type display rules:
      - Types with categorical_badge_eligible=False (beta_playtest,
        early_access, unknown, software) → BADGE_LIMITED_DATA.
      - Types with show_completion_rate / show_cliff_metric=False
        (multiplayer, mmo, no_endpoint, sandbox) → stickiness-only
        single-signal path; score = 1.5 × stickiness.
      - Eligible types (linear, mixed, expansion) → all three signals
        contribute, with low-confidence completion weighted at 0.3 vs
        0.7 for high-confidence.

    Order of evaluation: limited_data → hooks_players → filters_early →
    marathon → usually_hooks → often_filters → mixed_signals →
    standard_engagement. Marathon stays on top of every middle-band
    label per spec. Lean buckets sit above Mixed signals so a game whose
    strong signals net to a clear directional lean (e.g., stickiness +1
    + cliff -1 = +0.5) lands in Usually hooks rather than Mixed —
    Mixed is reserved for the truly-balanced case where strong signals
    cancel out to a near-zero score.
    """
    from app.game_type import engagement_display_rules, resolve_type

    gt = game.game_type or resolve_type(game)
    rules = engagement_display_rules(gt)

    if not rules["categorical_badge_eligible"]:
        return (BADGE_LIMITED_DATA, 0.0, {})

    breakdown: dict = {}
    score = 0.0
    populated_count = 0
    contributing_count = 0

    if rules["show_stickiness_ratio"]:
        contributing_count += 1
        sv = signal_value_stickiness(game.stickiness_ratio)
        contribution = sv * WEIGHT_STICKINESS
        score += contribution
        breakdown["stickiness"] = {
            "value": sv,
            "weight": WEIGHT_STICKINESS,
            "contribution": contribution,
            "description": _stickiness_description(sv, game.stickiness_ratio),
        }
        if game.stickiness_ratio is not None:
            populated_count += 1

    if rules["show_cliff_metric"]:
        contributing_count += 1
        cv = signal_value_cliff(game.cliff_metric, game.cliff_position)
        contribution = cv * WEIGHT_CLIFF
        score += contribution
        breakdown["cliff"] = {
            "value": cv,
            "weight": WEIGHT_CLIFF,
            "contribution": contribution,
            "description": _cliff_description(cv, game.cliff_metric, game.cliff_position),
        }
        if game.cliff_metric is not None:
            populated_count += 1

    high_conf_completion_value = 0
    if rules["show_completion_rate"]:
        contributing_count += 1
        # Completion contributes regardless of confidence — only the
        # weight differs. NULL rate gives signal value 0 and 0
        # contribution, which is functionally absent from the score.
        cv_compl = signal_value_completion(game.completion_rate)
        is_high = (game.completion_rate_confidence == "high")
        weight = WEIGHT_COMPLETION_HIGH if is_high else WEIGHT_COMPLETION_LOW
        contribution = cv_compl * weight
        score += contribution
        breakdown["completion"] = {
            "value": cv_compl,
            "weight": weight,
            "contribution": contribution,
            "description": _completion_description(
                cv_compl, game.completion_rate, game.completion_rate_confidence,
            ),
        }
        if game.completion_rate is not None:
            populated_count += 1
        # Track high-confidence completion separately for the Mixed-vs-
        # Standard strong-signal check (low-conf doesn't qualify there).
        if is_high:
            high_conf_completion_value = cv_compl

    if contributing_count == 0:
        # Should not happen for a categorical-eligible type, but defensive.
        return (BADGE_LIMITED_DATA, 0.0, breakdown)

    # 1. Limited data — majority of contributing signals are NULL. Low-
    # confidence completion still counts as populated for this gate per
    # spec — the soft signal stays useful on sparse-data games.
    no_data_count = contributing_count - populated_count
    threshold = (contributing_count + 1) // 2
    if no_data_count >= threshold:
        return (BADGE_LIMITED_DATA, score, breakdown)

    # 2/3. Hooks players / Filters early — composite-score thresholds.
    if score >= SCORE_HOOKS_THRESHOLD:
        return (BADGE_HOOKS_PLAYERS, score, breakdown)
    if score <= SCORE_FILTERS_THRESHOLD:
        return (BADGE_FILTERS_EARLY, score, breakdown)

    # 4. Marathon — high engagement + low confirmed completion. Requires
    # high-confidence completion specifically; sparse-data games with
    # low-conf completion aren't trusted enough for this label.
    rpm = game.review_playtime_median
    if (rpm is not None
        and rpm >= MARATHON_PLAYTIME_MIN_HOURS * 60
        and game.completion_rate_confidence == "high"
        and game.completion_rate is not None
        and game.completion_rate < MARATHON_COMPLETION_MAX):
        return (BADGE_MARATHON, score, breakdown)

    # 5. Lean buckets — score has a clear directional lean within the
    # middle band. Wins over Mixed signals: a game with strong signals
    # that net positive (e.g., stickiness +1 + cliff -1 = +0.5) goes here
    # rather than Mixed, because the lean is the more informative read.
    # Mixed signals is reserved for the truly-balanced case where strong
    # signals genuinely cancel out to near zero.
    if score >= SCORE_USUALLY_HOOKS_MIN:
        return (BADGE_USUALLY_HOOKS, score, breakdown)
    if score <= SCORE_OFTEN_FILTERS_MAX:
        return (BADGE_OFTEN_FILTERS, score, breakdown)

    # 6. Mixed signals — score lands in [-0.5, +0.5] AND at least one
    # strong signal (stickiness / cliff / high-conf completion) is
    # present. Low-conf completion alone doesn't promote here per spec.
    stickiness_value = breakdown.get("stickiness", {}).get("value", 0)
    cliff_value = breakdown.get("cliff", {}).get("value", 0)
    if _qualifies_strong_signal(stickiness_value, cliff_value, high_conf_completion_value):
        return (BADGE_MIXED_SIGNALS, score, breakdown)

    # 7. Standard engagement — middle band, no strong contributor, no lean.
    return (BADGE_STANDARD_ENGAGEMENT, score, breakdown)


# ---------------------------------------------------------------------------
# Phase 4 — Manual stickiness badge override (display layer)
# ---------------------------------------------------------------------------


def compute_stickiness_signal_display(game) -> tuple:
    """Override-aware wrapper for compute_stickiness_signal.

    Returns (badge, auto_badge, score, breakdown, is_overridden).

      - badge: what to display (manual override if set, else auto)
      - auto_badge: what compute_stickiness_signal returned regardless
      - score: composite score from the auto path (informational)
      - breakdown: per-signal contribution dict from the auto path
      - is_overridden: True when game.stickiness_badge_manual is populated

    The auto values are always computed so Game Detail can show
    "Auto would say: <auto_badge> (score X.X)" alongside the override.
    For ineligible game types (software / beta_playtest / early_access /
    unknown), the auto path returns (BADGE_LIMITED_DATA, 0.0, {}); the
    template uses an empty breakdown to render "Auto: not computed for
    this type" instead of an itemised breakdown.

    Used by: library_row.html, game_card.html, game_detail_engagement.html,
    and app/routes/library.py for sort-key resolution. All four call
    sites must use this helper rather than compute_stickiness_signal
    directly so the override surfaces consistently.
    """
    auto_badge, score, breakdown = compute_stickiness_signal(game)
    manual = getattr(game, "stickiness_badge_manual", None)
    if manual:
        return (manual, auto_badge, score, breakdown, True)
    return (auto_badge, auto_badge, score, breakdown, False)
