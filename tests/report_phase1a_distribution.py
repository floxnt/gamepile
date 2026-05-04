"""Distribution report for v3 Phase 1a engagement metrics.

Run with: uv run python tests/report_phase1a_distribution.py

After checkpoint 4's force refresh populates the new fields, this
prints per-metric coverage + min/max/median/mean. Used for Phase 1b
threshold tuning. Read-only — pure SELECTs against the live DB.
"""

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import database as db


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


def main():
    with db.get_db() as conn:
        rows = conn.execute("""
            SELECT name, completion_rate, completion_rate_confidence,
                   cliff_metric, review_playtime_median, stickiness_ratio,
                   playtime_median_avg_ratio, hltb_main_hours
            FROM games
            WHERE is_active = 1
        """).fetchall()
    total = len(rows)
    print(f"=" * 70)
    print(f"  Phase 1a metric distribution — {total} active games")
    print(f"=" * 70)
    print()

    # Per-metric population counts.
    completion = [r["completion_rate"] for r in rows if r["completion_rate"] is not None]
    cliff = [r["cliff_metric"] for r in rows if r["cliff_metric"] is not None]
    review_median = [r["review_playtime_median"] for r in rows if r["review_playtime_median"] is not None]
    stickiness = [r["stickiness_ratio"] for r in rows if r["stickiness_ratio"] is not None]
    pt_ratio = [r["playtime_median_avg_ratio"] for r in rows if r["playtime_median_avg_ratio"] is not None]

    no_completion = total - len(completion)
    print(f"completion_rate populated: {len(completion)}/{total} ({100*len(completion)/total:.0f}%)")
    print(f"  no_data (game has no achievements / fetch failed): {no_completion}")
    _summarize(completion, "  values", value_fmt=".4f")
    print()

    # Confidence breakdown for completion_rate.
    high = sum(1 for r in rows if r["completion_rate_confidence"] == "high")
    low = sum(1 for r in rows if r["completion_rate_confidence"] == "low")
    null_conf = sum(1 for r in rows if r["completion_rate_confidence"] is None)
    print(f"completion_rate_confidence breakdown:")
    print(f"  high: {high}  low: {low}  NULL: {null_conf}")
    print(f"  high-confidence completion_rate values:")
    high_vals = [r["completion_rate"] for r in rows if r["completion_rate_confidence"] == "high" and r["completion_rate"] is not None]
    _summarize(high_vals, "    ", value_fmt=".4f")
    print(f"  low-confidence completion_rate values:")
    low_vals = [r["completion_rate"] for r in rows if r["completion_rate_confidence"] == "low" and r["completion_rate"] is not None]
    _summarize(low_vals, "    ", value_fmt=".4f")
    print()

    print(f"cliff_metric populated: {len(cliff)}/{total} ({100*len(cliff)/total:.0f}%)")
    print(f"  (subset of completion_rate games — needs ≥4 post-discard achievements)")
    _summarize(cliff, "  values (pct-points)", value_fmt=".1f")
    print()

    print(f"review_playtime_median populated: {len(review_median)}/{total} ({100*len(review_median)/total:.0f}%)")
    print(f"  (subset with ≥10 reviews fetched in the 100-review sample)")
    _summarize(review_median, "  values (minutes)", value_fmt=".0f")
    _summarize(review_median, "  values (hours)", value_fmt=".1f", scale=1/60)
    print()

    print(f"stickiness_ratio populated: {len(stickiness)}/{total} ({100*len(stickiness)/total:.0f}%)")
    _summarize(stickiness, "  values (fraction)", value_fmt=".3f")
    _summarize(stickiness, "  values (percent)", value_fmt=".1f", scale=100)
    # Split by HLTB-present vs HLTB-null since the threshold differs.
    hltb_relative = [r["stickiness_ratio"] for r in rows
                     if r["stickiness_ratio"] is not None and r["hltb_main_hours"]]
    flat_fallback = [r["stickiness_ratio"] for r in rows
                     if r["stickiness_ratio"] is not None and not r["hltb_main_hours"]]
    print(f"  by threshold path:")
    print(f"    HLTB-relative (0.5×main): n={len(hltb_relative)}")
    print(f"    flat 20h fallback:        n={len(flat_fallback)}")
    print()

    print(f"playtime_median_avg_ratio populated: {len(pt_ratio)}/{total} ({100*len(pt_ratio)/total:.0f}%)")
    _summarize(pt_ratio, "  values", value_fmt=".3f")
    print()

    # Sample concrete games at the extremes of each metric — useful for sanity.
    print(f"-" * 70)
    print(f"  Sample extremes (top + bottom 3 by each metric)")
    print(f"-" * 70)
    for label, key, fmt in [
        ("completion_rate (lowest = rarest endpoint)", "completion_rate", ".4f"),
        ("cliff_metric (largest gap)", "cliff_metric", ".1f"),
        ("review_playtime_median (highest)", "review_playtime_median", ".0f"),
        ("stickiness_ratio (highest)", "stickiness_ratio", ".3f"),
        ("playtime_median_avg_ratio (highest = most even)", "playtime_median_avg_ratio", ".3f"),
    ]:
        non_null = [r for r in rows if r[key] is not None]
        if not non_null:
            continue
        non_null.sort(key=lambda r: r[key])
        print(f"\n{label}:")
        print(f"  bottom 3:")
        for r in non_null[:3]:
            print(f"    {format(r[key], fmt):>12}  {r['name']!r}")
        print(f"  top 3:")
        for r in non_null[-3:]:
            print(f"    {format(r[key], fmt):>12}  {r['name']!r}")


if __name__ == "__main__":
    main()
