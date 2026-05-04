"""Distribution report for v3 Phase 1a engagement metrics + game-type
cross-tabulation. Read-only against the live DB.

Run with: uv run python tests/report_phase1a_distribution.py

Run AFTER force refresh has populated the Phase 1a fields. Reports:

  1. Per-metric coverage + min/max/median/mean
  2. Confidence breakdown for completion_rate (high/low/NULL)
  3. Cliff_metric histogram in 10-pp buckets
  4. Stickiness threshold-path split (HLTB-relative vs flat fallback)
  5. Cross-tabulation: 11 game types × 5 metrics, % populated per cell
  6. Diagnostic: linear/mixed games where ALL 5 metrics are NULL
"""

import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import database as db
from app.game_type import ALL_GAME_TYPES


def _fmt(x, fmt=".4f"):
    if x is None:
        return "—"
    return format(x, fmt)


def _summarize(values: list, label: str, *, value_fmt=".4f", scale: float = 1.0) -> None:
    if not values:
        print(f"  {label}: 0 games populated")
        return
    scaled = [v * scale for v in values]
    print(
        f"  {label}: n={len(values)}  "
        f"min={_fmt(min(scaled), value_fmt)}  "
        f"max={_fmt(max(scaled), value_fmt)}  "
        f"median={_fmt(statistics.median(scaled), value_fmt)}  "
        f"mean={_fmt(statistics.mean(scaled), value_fmt)}"
    )


def _histogram(values: list, bin_size: float, label: str, value_fmt=".0f") -> None:
    if not values:
        print(f"  {label}: empty")
        return
    buckets: Counter = Counter()
    for v in values:
        bucket_low = (int(v) // int(bin_size)) * bin_size
        buckets[bucket_low] += 1
    print(f"  {label} histogram (bin={bin_size}):")
    max_count = max(buckets.values())
    for low in sorted(buckets):
        count = buckets[low]
        bar = "█" * int(40 * count / max_count) if max_count else ""
        high = low + bin_size
        print(f"    [{format(low, value_fmt)}, {format(high, value_fmt)})  {count:4d}  {bar}")


def main():
    with db.get_db() as conn:
        rows = conn.execute("""
            SELECT name, appid, game_type, game_type_manual, app_type,
                   completion_rate, completion_rate_confidence,
                   cliff_metric, review_playtime_median, stickiness_ratio,
                   playtime_median_avg_ratio, hltb_main_hours,
                   steam_review_count
            FROM games
            WHERE is_active = 1
        """).fetchall()
    total = len(rows)
    print("=" * 78)
    print(f"  Phase 1a metric distribution — {total} active games")
    print("=" * 78)

    # ----- 1. Per-metric population + summary stats -----
    print()
    print("--- 1. Per-metric coverage ---")
    print()

    completion = [r["completion_rate"] for r in rows if r["completion_rate"] is not None]
    cliff = [r["cliff_metric"] for r in rows if r["cliff_metric"] is not None]
    review_median = [r["review_playtime_median"] for r in rows if r["review_playtime_median"] is not None]
    stickiness = [r["stickiness_ratio"] for r in rows if r["stickiness_ratio"] is not None]
    pt_ratio = [r["playtime_median_avg_ratio"] for r in rows if r["playtime_median_avg_ratio"] is not None]

    print(f"completion_rate populated: {len(completion)}/{total} ({100*len(completion)/total:.1f}%)")
    _summarize(completion, "  values", value_fmt=".4f")
    print()

    # Confidence breakdown
    high_conf = [r["completion_rate"] for r in rows if r["completion_rate_confidence"] == "high" and r["completion_rate"] is not None]
    low_conf = [r["completion_rate"] for r in rows if r["completion_rate_confidence"] == "low" and r["completion_rate"] is not None]
    null_conf = [r for r in rows if r["completion_rate_confidence"] is None]
    print(f"completion_rate_confidence breakdown:")
    print(f"  high: {len(high_conf)}  low: {len(low_conf)}  NULL: {len(null_conf)}")
    _summarize(high_conf, "  high-confidence values", value_fmt=".4f")
    _summarize(low_conf, "  low-confidence values", value_fmt=".4f")
    print()

    print(f"cliff_metric populated: {len(cliff)}/{total} ({100*len(cliff)/total:.1f}%)")
    _summarize(cliff, "  values (pct-points)", value_fmt=".1f")
    _histogram(cliff, 10.0, "cliff_metric", value_fmt=".0f")
    print()

    print(f"review_playtime_median populated: {len(review_median)}/{total} ({100*len(review_median)/total:.1f}%)")
    _summarize(review_median, "  values (minutes)", value_fmt=".0f")
    _summarize(review_median, "  values (hours)",   value_fmt=".1f", scale=1/60)
    print()

    print(f"stickiness_ratio populated: {len(stickiness)}/{total} ({100*len(stickiness)/total:.1f}%)")
    _summarize(stickiness, "  values (fraction)", value_fmt=".3f")
    _summarize(stickiness, "  values (percent)",  value_fmt=".1f", scale=100)
    hltb_relative = [r for r in rows if r["stickiness_ratio"] is not None and r["hltb_main_hours"]]
    flat_fallback = [r for r in rows if r["stickiness_ratio"] is not None and not r["hltb_main_hours"]]
    print(f"  threshold path:")
    print(f"    HLTB-relative (0.5×main): n={len(hltb_relative)}")
    print(f"    flat 20h (1200-min) fallback: n={len(flat_fallback)}")
    print()

    print(f"playtime_median_avg_ratio populated: {len(pt_ratio)}/{total} ({100*len(pt_ratio)/total:.1f}%)")
    _summarize(pt_ratio, "  values", value_fmt=".3f")
    print()

    # ----- 2. Cross-tabulation: game_type × metric coverage -----
    print()
    print("--- 2. Cross-tabulation: game_type × metric coverage ---")
    print()
    print(f"  {'type':16s} {'n':>5s}  {'completion':>12s} {'cliff':>10s} {'review_med':>11s} {'sticky':>8s} {'pt_ratio':>10s}")
    print("  " + "-" * 80)

    rows_by_type: dict = {gt: [] for gt in ALL_GAME_TYPES}
    rows_by_type["(NULL)"] = []
    for r in rows:
        gt = r["game_type"] or "(NULL)"
        rows_by_type.setdefault(gt, []).append(r)

    def pct_populated(rs, key):
        if not rs:
            return "—"
        n = sum(1 for r in rs if r[key] is not None)
        return f"{100*n/len(rs):.0f}% ({n})"

    type_order = list(ALL_GAME_TYPES) + ["(NULL)"]
    for gt in type_order:
        rs = rows_by_type.get(gt, [])
        if not rs:
            continue
        print(
            f"  {gt:16s} {len(rs):>5d}  "
            f"{pct_populated(rs, 'completion_rate'):>12s} "
            f"{pct_populated(rs, 'cliff_metric'):>10s} "
            f"{pct_populated(rs, 'review_playtime_median'):>11s} "
            f"{pct_populated(rs, 'stickiness_ratio'):>8s} "
            f"{pct_populated(rs, 'playtime_median_avg_ratio'):>10s}"
        )

    # ----- 3. Diagnostic: linear/mixed games where ALL metrics are NULL -----
    print()
    print("--- 3. Diagnostic: 'should have data' types where ALL 5 metrics are NULL ---")
    print()
    print(f"  Linear/mixed games are expected to populate at least some metrics.")
    print(f"  All-NULL means: no achievements OR <10 reviews fetched OR no SteamSpy data.")
    print(f"  Small count = expected for niche / unenriched games. Large count = bug.")
    print()
    diagnostics = []
    for r in rows:
        if r["game_type"] not in ("linear", "mixed"):
            continue
        if any(r[k] is not None for k in (
            "completion_rate", "cliff_metric", "review_playtime_median",
            "stickiness_ratio", "playtime_median_avg_ratio",
        )):
            continue
        diagnostics.append(r)
    print(f"  count: {len(diagnostics)}/{sum(1 for r in rows if r['game_type'] in ('linear','mixed'))} linear/mixed games are fully NULL")
    if diagnostics:
        print(f"  sample (first 20):")
        for r in diagnostics[:20]:
            reviews = r["steam_review_count"] or 0
            hltb = r["hltb_main_hours"] or "—"
            print(f"    appid={r['appid']:>10d}  type={r['game_type']:6s}  reviews={reviews:>7d}  hltb_main={hltb}  {r['name'][:50]!r}")


if __name__ == "__main__":
    main()
