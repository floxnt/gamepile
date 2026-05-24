"""Engagement texture probe — offline empirical test.

Tests whether median_achievement_unlock_pct + stickiness_ratio +
cliff_metric/cliff_position compose into a signal that correlates
with actual user engagement behavior (playtime vs HLTB main).

This is NOT a hook-point resurrection. It tests a DIFFERENT hypothesis:
engagement texture estimation WITHOUT completion identification.

Read-only against the live DB (no schema changes, no sync changes).
Fetches raw achievement + review data from Steam APIs for cliff and
stickiness recomputation, caches results in scripts/.cache/.

Run with: uv run python scripts/engagement_texture_probe.py
"""

import asyncio
import json
import logging
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from app import database as db
from app.fetchers.steam import fetch_review_data
from app.fetchers.steam_achievements import fetch_achievements_with_metadata

# Reuse the validated pure-functional computations from the dormant
# pipeline. The retirement was about integration with unreliable
# completion_rate, not about stickiness_ratio or cliff computations
# themselves — those were the strongest signals in the original work.
from app.hook_metrics import (
    compute_cliff_metric,
    compute_cliff_position,
    compute_stickiness_ratio,
)

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / ".cache" / "engagement_texture_probe"


def _rank(values: list) -> list:
    """Assign fractional ranks to values (handles ties via averaging)."""
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j - 1) / 2.0 + 1
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def _spearman_rho(x: list, y: list) -> float:
    """Spearman rank correlation. No scipy dependency."""
    n = len(x)
    if n < 3:
        return 0.0
    rx = _rank(x)
    ry = _rank(y)
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    cov = sum((a - mean_rx) * (b - mean_ry) for a, b in zip(rx, ry))
    std_x = math.sqrt(sum((a - mean_rx) ** 2 for a in rx))
    std_y = math.sqrt(sum((b - mean_ry) ** 2 for b in ry))
    if std_x == 0 or std_y == 0:
        return 0.0
    return cov / (std_x * std_y)

# =========================================================================
# PREDECLARED FORMULA — locked before any data is seen
# =========================================================================
#
# Three signals, NO completion_rate, NO completion identification.
# Score range: -3.2 to +2.2 (asymmetric — cliff only pushes negative).
#
# 1. Stickiness ratio (weight 1.5)
#    Strongest dormant signal per original Phase 1c findings. Measures
#    what fraction of reviewers played deep into the game relative to
#    its length (0.5 × HLTB main hours, or flat 20h fallback).
#    Thresholds: >= 0.90 → +1, <= 0.50 → -1, between → 0.
#
# 2. Cliff composite (weight 1.0)
#    Large cliff (>= 20pp) in early/mid position (< 0.70) indicates
#    early-game abandonment pattern. Late cliffs are completionist
#    gates, not abandonment. Cliff never contributes positive.
#    >= 20pp AND position < 0.70 → -1, everything else → 0.
#
# 3. Median achievement unlock % (weight 0.7)
#    Uses P25/P75 of the Cohort A distribution as cut points.
#    Measured from live DB (355 Cohort A games with data):
#      P25 = 7.3%, P75 = 24.1%
#    >= 24.1 → +1 (high retention), <= 7.3 → -1 (high attrition), between → 0.
#
# Bucket thresholds (predeclared):
#   score >= +1.5  → "Engaged retention"
#   score in [+0.5, +1.5) → "Above average"
#   score in [-0.5, +0.5) → "Standard"
#   score in [-1.5, -0.5) → "Below average"
#   score <= -1.5  → "Early attrition"
#   < 2 non-NULL signals → "Limited"

WEIGHT_STICKINESS = 1.5
WEIGHT_CLIFF = 1.0
WEIGHT_MEDIAN_UNLOCK = 0.7

STICKINESS_STICKY_THRESHOLD = 0.90
STICKINESS_FILTERS_THRESHOLD = 0.50

CLIFF_SIZE_THRESHOLD = 20.0
CLIFF_LATE_POSITION = 0.70

# Percentile-based cuts from the live Cohort A distribution of
# median_achievement_unlock_pct (355 populated values, measured
# 2026-05-24 during predeclaration phase).
MEDIAN_UNLOCK_HIGH_THRESHOLD = 24.1  # P75
MEDIAN_UNLOCK_LOW_THRESHOLD = 7.3    # P25

SCORE_ENGAGED_RETENTION = 1.5
SCORE_ABOVE_AVERAGE = 0.5
SCORE_BELOW_AVERAGE = -0.5
SCORE_EARLY_ATTRITION = -1.5

BUCKET_ENGAGED = "Engaged retention"
BUCKET_ABOVE = "Above average"
BUCKET_STANDARD = "Standard"
BUCKET_BELOW = "Below average"
BUCKET_ATTRITION = "Early attrition"
BUCKET_LIMITED = "Limited"

BUCKET_ORDER = [
    BUCKET_ENGAGED, BUCKET_ABOVE, BUCKET_STANDARD,
    BUCKET_BELOW, BUCKET_ATTRITION, BUCKET_LIMITED,
]

# Cohort definitions
COHORT_A_TYPES = ("linear", "mixed", "expansion")
COHORT_B_TYPES = ("sandbox", "no_endpoint", "mmo", "multiplayer")
COHORT_C_TYPES = ("software", "beta_playtest", "early_access", "unknown")

# Behavior gate: bounce/continued thresholds
BOUNCE_MIN_PLAYTIME_MINUTES = 30
BOUNCE_MAX_HOURS = 2.0
BOUNCE_MAX_HLTB_FRACTION = 0.15
CONTINUED_MIN_HOURS = 5.0
CONTINUED_MIN_HLTB_FRACTION = 0.35


# =========================================================================
# Signal computation
# =========================================================================

def signal_stickiness(ratio: Optional[float]) -> int:
    if ratio is None:
        return 0
    if ratio >= STICKINESS_STICKY_THRESHOLD:
        return 1
    if ratio <= STICKINESS_FILTERS_THRESHOLD:
        return -1
    return 0


def signal_cliff(metric: Optional[float], position: Optional[float]) -> int:
    if metric is None or metric < CLIFF_SIZE_THRESHOLD:
        return 0
    if position is None:
        return 0
    if position >= CLIFF_LATE_POSITION:
        return 0
    return -1


def signal_median_unlock(pct: Optional[float]) -> int:
    if pct is None:
        return 0
    if pct >= MEDIAN_UNLOCK_HIGH_THRESHOLD:
        return 1
    if pct <= MEDIAN_UNLOCK_LOW_THRESHOLD:
        return -1
    return 0


def compute_score(
    stickiness_ratio: Optional[float],
    cliff_metric: Optional[float],
    cliff_position: Optional[float],
    median_unlock_pct: Optional[float],
) -> tuple:
    """Returns (bucket, score, populated_count, signal_details)."""
    populated = 0
    if stickiness_ratio is not None:
        populated += 1
    if cliff_metric is not None:
        populated += 1
    if median_unlock_pct is not None:
        populated += 1

    if populated < 2:
        return (BUCKET_LIMITED, 0.0, populated, {})

    sv = signal_stickiness(stickiness_ratio)
    cv = signal_cliff(cliff_metric, cliff_position)
    mv = signal_median_unlock(median_unlock_pct)

    score = (
        sv * WEIGHT_STICKINESS
        + cv * WEIGHT_CLIFF
        + mv * WEIGHT_MEDIAN_UNLOCK
    )

    details = {
        "stickiness": {"value": sv, "weight": WEIGHT_STICKINESS, "contribution": sv * WEIGHT_STICKINESS, "raw": stickiness_ratio},
        "cliff": {"value": cv, "weight": WEIGHT_CLIFF, "contribution": cv * WEIGHT_CLIFF, "raw_metric": cliff_metric, "raw_position": cliff_position},
        "median_unlock": {"value": mv, "weight": WEIGHT_MEDIAN_UNLOCK, "contribution": mv * WEIGHT_MEDIAN_UNLOCK, "raw": median_unlock_pct},
    }

    if score >= SCORE_ENGAGED_RETENTION:
        return (BUCKET_ENGAGED, score, populated, details)
    if score >= SCORE_ABOVE_AVERAGE:
        return (BUCKET_ABOVE, score, populated, details)
    if score > SCORE_BELOW_AVERAGE:
        return (BUCKET_STANDARD, score, populated, details)
    if score > SCORE_EARLY_ATTRITION:
        return (BUCKET_BELOW, score, populated, details)
    return (BUCKET_ATTRITION, score, populated, details)


# =========================================================================
# Behavior outcome labels
# =========================================================================

def classify_behavior(playtime_minutes: int, hltb_main_hours: Optional[float]) -> str:
    playtime_hours = playtime_minutes / 60.0
    if playtime_minutes < BOUNCE_MIN_PLAYTIME_MINUTES:
        return "unplayed"

    if hltb_main_hours and hltb_main_hours > 0:
        bounce_cap = min(BOUNCE_MAX_HOURS, BOUNCE_MAX_HLTB_FRACTION * hltb_main_hours)
        continued_floor = min(CONTINUED_MIN_HOURS, CONTINUED_MIN_HLTB_FRACTION * hltb_main_hours)
    else:
        bounce_cap = BOUNCE_MAX_HOURS
        continued_floor = CONTINUED_MIN_HOURS

    if playtime_hours < bounce_cap:
        return "bounced"
    if playtime_hours >= continued_floor:
        return "continued"
    return "unclear"


def normalized_engagement(playtime_minutes: int, hltb_main_hours: Optional[float]) -> Optional[float]:
    playtime_hours = playtime_minutes / 60.0
    if hltb_main_hours and hltb_main_hours > 0:
        return playtime_hours / hltb_main_hours
    return math.log1p(playtime_hours)


# =========================================================================
# Data fetching with caching
# =========================================================================

def _cache_path(appid: int, kind: str) -> Path:
    return CACHE_DIR / f"{appid}_{kind}.json"


def _load_cached(appid: int, kind: str):
    path = _cache_path(appid, kind)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _save_cache(appid: int, kind: str, data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(appid, kind)
    path.write_text(json.dumps(data))


async def fetch_game_signals(
    client: httpx.AsyncClient,
    appid: int,
    hltb_main_hours: Optional[float],
) -> dict:
    """Fetch and compute stickiness_ratio, cliff_metric, cliff_position for one game."""
    result = {
        "stickiness_ratio": None,
        "cliff_metric": None,
        "cliff_position": None,
    }

    # --- Achievements (for cliff) ---
    cached_ach = _load_cached(appid, "achievements")
    if cached_ach is not None:
        achievements = cached_ach if cached_ach != "NONE" else None
    else:
        achievements = await fetch_achievements_with_metadata(client, appid)
        _save_cache(appid, "achievements", achievements if achievements is not None else "NONE")

    if achievements:
        result["cliff_metric"] = compute_cliff_metric(achievements)
        result["cliff_position"] = compute_cliff_position(achievements)

    # --- Reviews (for stickiness) ---
    cached_rev = _load_cached(appid, "reviews")
    if cached_rev is not None:
        playtimes = cached_rev if isinstance(cached_rev, list) else []
    else:
        review_data = await fetch_review_data(client, appid)
        playtimes = (review_data or {}).get("playtimes", [])
        _save_cache(appid, "reviews", playtimes)

    if playtimes:
        result["stickiness_ratio"] = compute_stickiness_ratio(playtimes, hltb_main_hours)

    return result


# =========================================================================
# Main probe
# =========================================================================

async def run_probe():
    with db.get_db() as conn:
        all_games = conn.execute("""
            SELECT appid, name, game_type, playtime_minutes,
                   hltb_main_hours, median_achievement_unlock_pct,
                   steam_review_count
            FROM games
            WHERE is_active = 1
        """).fetchall()

    cohort_a = [g for g in all_games if g["game_type"] in COHORT_A_TYPES]
    cohort_b = [g for g in all_games if g["game_type"] in COHORT_B_TYPES]
    cohort_c = [g for g in all_games if g["game_type"] in COHORT_C_TYPES]

    print(f"Cohort A (finite/progression): {len(cohort_a)} games")
    print(f"Cohort B (open-ended): {len(cohort_b)} games")
    print(f"Cohort C (excluded): {len(cohort_c)} games")
    print()

    # --- Fetch signals for Cohort A ---
    print("Fetching achievement + review data for Cohort A...")
    game_signals: dict = {}

    async with httpx.AsyncClient() as client:
        for i, g in enumerate(cohort_a):
            appid = g["appid"]
            if (i + 1) % 50 == 0:
                print(f"  ...{i+1}/{len(cohort_a)}")
            signals = await fetch_game_signals(client, appid, g["hltb_main_hours"])
            game_signals[appid] = signals

    # --- Compute scores for Cohort A ---
    print()
    print("Computing engagement texture scores...")

    results_a: list = []
    for g in cohort_a:
        appid = g["appid"]
        signals = game_signals[appid]
        bucket, score, populated, details = compute_score(
            stickiness_ratio=signals["stickiness_ratio"],
            cliff_metric=signals["cliff_metric"],
            cliff_position=signals["cliff_position"],
            median_unlock_pct=g["median_achievement_unlock_pct"],
        )

        behavior = classify_behavior(g["playtime_minutes"], g["hltb_main_hours"])
        norm_eng = normalized_engagement(g["playtime_minutes"], g["hltb_main_hours"])

        results_a.append({
            "appid": appid,
            "name": g["name"],
            "game_type": g["game_type"],
            "bucket": bucket,
            "score": score,
            "populated": populated,
            "details": details,
            "playtime_minutes": g["playtime_minutes"],
            "hltb_main_hours": g["hltb_main_hours"],
            "median_unlock_pct": g["median_achievement_unlock_pct"],
            "stickiness_ratio": signals["stickiness_ratio"],
            "cliff_metric": signals["cliff_metric"],
            "cliff_position": signals["cliff_position"],
            "behavior": behavior,
            "norm_engagement": norm_eng,
        })

    # --- Distribution report ---
    bucket_counts = Counter(r["bucket"] for r in results_a)
    total_a = len(results_a)
    non_limited = total_a - bucket_counts.get(BUCKET_LIMITED, 0)

    print()
    print("=" * 78)
    print(f"  COHORT A DISTRIBUTION — {total_a} games")
    print("=" * 78)
    print()
    for b in BUCKET_ORDER:
        n = bucket_counts.get(b, 0)
        pct = 100 * n / total_a if total_a else 0
        bar = "█" * int(40 * n / max(bucket_counts.values())) if bucket_counts else ""
        print(f"  {b:<22s}  {n:>4d}  ({pct:5.1f}%)  {bar}")

    # --- Distribution gate checks ---
    print()
    print("--- DISTRIBUTION GATES ---")
    print()

    limited_n = bucket_counts.get(BUCKET_LIMITED, 0)
    limited_pct = 100 * limited_n / total_a

    gate1 = non_limited >= 0.60 * total_a
    print(f"  Gate 1: >= 60% non-limited:  {100*non_limited/total_a:.1f}%  {'PASS' if gate1 else 'FAIL'}")

    outside_band = sum(bucket_counts.get(b, 0) for b in [BUCKET_ENGAGED, BUCKET_ABOVE, BUCKET_BELOW, BUCKET_ATTRITION])
    outside_pct = 100 * outside_band / total_a
    gate2 = 20 <= outside_pct <= 45
    print(f"  Gate 2: 20-45% outside neutral: {outside_pct:.1f}%  {'PASS' if gate2 else 'FAIL'}")

    max_bucket_pct = max(100 * v / total_a for v in bucket_counts.values()) if bucket_counts else 0
    max_bucket_name = max(bucket_counts, key=bucket_counts.get) if bucket_counts else "N/A"
    gate3 = max_bucket_pct <= 45
    print(f"  Gate 3: no bucket > 45%: max={max_bucket_name} at {max_bucket_pct:.1f}%  {'PASS' if gate3 else 'FAIL'}")

    gate4 = limited_pct <= 35
    print(f"  Gate 4: limited <= 35%: {limited_pct:.1f}%  {'PASS' if gate4 else 'FAIL'}")

    dist_pass = gate1 and gate2 and gate3 and gate4

    # --- Behavior gate checks ---
    print()
    print("--- BEHAVIOR GATES ---")
    print()

    played = [r for r in results_a if r["playtime_minutes"] >= BOUNCE_MIN_PLAYTIME_MINUTES and r["bucket"] != BUCKET_LIMITED]

    top_bucket_games = [r for r in played if r["bucket"] == BUCKET_ENGAGED]
    bottom_bucket_games = [r for r in played if r["bucket"] == BUCKET_ATTRITION]

    # If top or bottom bucket is too small, fall back to Above/Below
    if len(top_bucket_games) < 5:
        top_bucket_games = [r for r in played if r["bucket"] in (BUCKET_ENGAGED, BUCKET_ABOVE)]
        top_label = "Engaged+Above"
    else:
        top_label = "Engaged"
    if len(bottom_bucket_games) < 5:
        bottom_bucket_games = [r for r in played if r["bucket"] in (BUCKET_ATTRITION, BUCKET_BELOW)]
        bottom_label = "Attrition+Below"
    else:
        bottom_label = "Attrition"

    print(f"  Played games (>= 30min, non-Limited): {len(played)}")
    print(f"  Top bucket ({top_label}): {len(top_bucket_games)} games")
    print(f"  Bottom bucket ({bottom_label}): {len(bottom_bucket_games)} games")
    print()

    # Gate B1: median normalized engagement ratio
    top_engagements = [r["norm_engagement"] for r in top_bucket_games if r["norm_engagement"] is not None]
    bottom_engagements = [r["norm_engagement"] for r in bottom_bucket_games if r["norm_engagement"] is not None]

    if top_engagements and bottom_engagements:
        top_median_eng = statistics.median(top_engagements)
        bottom_median_eng = statistics.median(bottom_engagements)
        eng_ratio = top_median_eng / bottom_median_eng if bottom_median_eng > 0 else float("inf")
        gate_b1 = eng_ratio >= 1.75
        print(f"  Gate B1: top median engagement / bottom median >= 1.75x")
        print(f"    top median={top_median_eng:.3f}, bottom median={bottom_median_eng:.3f}, ratio={eng_ratio:.2f}x  {'PASS' if gate_b1 else 'FAIL'}")
    else:
        gate_b1 = False
        print(f"  Gate B1: INSUFFICIENT DATA (top={len(top_engagements)}, bottom={len(bottom_engagements)})")

    # Gate B2: bounce rate difference
    top_bounced = sum(1 for r in top_bucket_games if r["behavior"] == "bounced")
    bottom_bounced = sum(1 for r in bottom_bucket_games if r["behavior"] == "bounced")
    top_bounce_rate = 100 * top_bounced / len(top_bucket_games) if top_bucket_games else 0
    bottom_bounce_rate = 100 * bottom_bounced / len(bottom_bucket_games) if bottom_bucket_games else 0
    bounce_diff = bottom_bounce_rate - top_bounce_rate
    gate_b2 = bounce_diff >= 20
    print(f"  Gate B2: bottom bounce rate >= 20pp higher than top")
    print(f"    top bounce={top_bounce_rate:.1f}%, bottom bounce={bottom_bounce_rate:.1f}%, diff={bounce_diff:.1f}pp  {'PASS' if gate_b2 else 'FAIL'}")

    # Gate B3: Spearman or pairwise
    scored_played = [r for r in played if r["norm_engagement"] is not None]
    if len(scored_played) >= 20:
        scores_list = [r["score"] for r in scored_played]
        eng_list = [r["norm_engagement"] for r in scored_played]
        corr = _spearman_rho(scores_list, eng_list)
        gate_b3 = corr >= 0.25
        print(f"  Gate B3: Spearman(score, norm_engagement) >= 0.25")
        print(f"    rho={corr:.4f}, n={len(scored_played)}  {'PASS' if gate_b3 else 'FAIL'}")
    else:
        # Pairwise fallback
        wins = 0
        pairs = 0
        high_scored = [r for r in scored_played if r["score"] >= SCORE_ABOVE_AVERAGE]
        low_scored = [r for r in scored_played if r["score"] <= SCORE_BELOW_AVERAGE]
        for h in high_scored:
            for l in low_scored:
                if h["hltb_main_hours"] and l["hltb_main_hours"]:
                    ratio = max(h["hltb_main_hours"], l["hltb_main_hours"]) / max(min(h["hltb_main_hours"], l["hltb_main_hours"]), 0.1)
                    if ratio > 3:
                        continue
                pairs += 1
                if (h["norm_engagement"] or 0) > (l["norm_engagement"] or 0):
                    wins += 1
        win_rate = wins / pairs if pairs else 0
        gate_b3 = win_rate >= 0.65 and pairs >= 10
        print(f"  Gate B3 (pairwise fallback): high-score wins >= 65%")
        print(f"    wins={wins}/{pairs} ({100*win_rate:.1f}%)  {'PASS' if gate_b3 else 'FAIL'}")

    behavior_pass = gate_b1 and gate_b2 and gate_b3

    # --- Diagnostic samples ---
    print()
    print("--- DIAGNOSTIC SAMPLES (Cohort A) ---")
    print()
    for b in BUCKET_ORDER:
        samples = [r for r in results_a if r["bucket"] == b]
        samples.sort(key=lambda r: -abs(r["score"]))
        print(f"  {b} ({len(samples)} games):")
        for r in samples[:5]:
            pt_h = r["playtime_minutes"] / 60.0
            hltb = r["hltb_main_hours"] or 0
            norm = r["norm_engagement"]
            norm_s = f"{norm:.2f}" if norm is not None else "—"
            sticky = r["stickiness_ratio"]
            sticky_s = f"{sticky:.2f}" if sticky is not None else "—"
            cliff_s = f"{r['cliff_metric']:.1f}pp@{r['cliff_position']:.2f}" if r["cliff_metric"] else "—"
            med_s = f"{r['median_unlock_pct']:.1f}%" if r["median_unlock_pct"] is not None else "—"
            print(f"    {r['name'][:40]:<42s} score={r['score']:+5.2f}  "
                  f"pt={pt_h:.0f}h/{hltb:.0f}h  norm={norm_s}  "
                  f"behav={r['behavior']}  sticky={sticky_s}  cliff={cliff_s}  med={med_s}")
        print()

    # --- Cohort B exploratory ---
    print()
    print("=" * 78)
    print(f"  COHORT B EXPLORATORY — {len(cohort_b)} games")
    print("=" * 78)
    print()

    # Fetch signals for Cohort B
    print("Fetching data for Cohort B...")
    results_b: list = []
    async with httpx.AsyncClient() as client:
        for i, g in enumerate(cohort_b):
            appid = g["appid"]
            signals = await fetch_game_signals(client, appid, g["hltb_main_hours"])
            bucket, score, populated, details = compute_score(
                stickiness_ratio=signals["stickiness_ratio"],
                cliff_metric=signals["cliff_metric"],
                cliff_position=signals["cliff_position"],
                median_unlock_pct=g["median_achievement_unlock_pct"],
            )
            results_b.append({
                "appid": appid,
                "name": g["name"],
                "game_type": g["game_type"],
                "bucket": bucket,
                "score": score,
                "playtime_minutes": g["playtime_minutes"],
                "hltb_main_hours": g["hltb_main_hours"],
                "stickiness_ratio": signals["stickiness_ratio"],
            })

    bucket_counts_b = Counter(r["bucket"] for r in results_b)
    for b in BUCKET_ORDER:
        n = bucket_counts_b.get(b, 0)
        pct = 100 * n / len(cohort_b) if cohort_b else 0
        print(f"  {b:<22s}  {n:>4d}  ({pct:5.1f}%)")

    print()
    print("  Per game type:")
    for gt in COHORT_B_TYPES:
        gt_games = [r for r in results_b if r["game_type"] == gt]
        if not gt_games:
            continue
        gt_counts = Counter(r["bucket"] for r in gt_games)
        engaged_n = sum(gt_counts.get(b, 0) for b in [BUCKET_ENGAGED, BUCKET_ABOVE])
        filtered_n = sum(gt_counts.get(b, 0) for b in [BUCKET_ATTRITION, BUCKET_BELOW])
        print(f"    {gt:<14s} n={len(gt_games):>3d}  engaged={engaged_n}  standard={gt_counts.get(BUCKET_STANDARD,0)}  filtered={filtered_n}  limited={gt_counts.get(BUCKET_LIMITED,0)}")

    # --- Cohort C ---
    print()
    print(f"  COHORT C (hard excluded): {len(cohort_c)} games")
    for gt in COHORT_C_TYPES:
        n = sum(1 for g in cohort_c if g["game_type"] == gt)
        if n:
            print(f"    {gt}: {n}")

    # --- Final verdict ---
    print()
    print("=" * 78)
    print("  FINAL VERDICT")
    print("=" * 78)
    print()
    print(f"  Distribution gates: {'ALL PASS' if dist_pass else 'FAIL'}")
    print(f"    Gate 1 (>= 60% non-limited):  {'PASS' if gate1 else 'FAIL'}")
    print(f"    Gate 2 (20-45% outside band):  {'PASS' if gate2 else 'FAIL'}")
    print(f"    Gate 3 (no bucket > 45%):      {'PASS' if gate3 else 'FAIL'}")
    print(f"    Gate 4 (limited <= 35%):        {'PASS' if gate4 else 'FAIL'}")
    print()
    print(f"  Behavior gates: {'ALL PASS' if behavior_pass else 'FAIL'}")
    print(f"    Gate B1 (engagement ratio):    {'PASS' if gate_b1 else 'FAIL'}")
    print(f"    Gate B2 (bounce rate diff):    {'PASS' if gate_b2 else 'FAIL'}")
    print(f"    Gate B3 (correlation):         {'PASS' if gate_b3 else 'FAIL'}")
    print()

    overall = dist_pass and behavior_pass
    print(f"  OVERALL: {'PASS — move to product framing discussion' if overall else 'FAIL — permanent retirement justified'}")
    print()
    print("  (Smell test on diagnostic samples is a manual judgment overlay — see report)")

    # --- Save full results for report generation ---
    report_data = {
        "cohort_a_total": total_a,
        "cohort_b_total": len(cohort_b),
        "cohort_c_total": len(cohort_c),
        "bucket_counts": dict(bucket_counts),
        "dist_gates": {"gate1": gate1, "gate2": gate2, "gate3": gate3, "gate4": gate4, "all_pass": dist_pass},
        "behavior_gates": {"gate_b1": gate_b1, "gate_b2": gate_b2, "gate_b3": gate_b3, "all_pass": behavior_pass},
        "overall": overall,
        "results_a": results_a,
        "results_b": results_b,
    }
    _save_cache(0, "report_data", report_data)

    return report_data


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    data = asyncio.run(run_probe())
    return data


if __name__ == "__main__":
    main()
