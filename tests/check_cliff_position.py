"""Phase 1c commit 1 spot-check + read-only distribution.

Run with: uv run python tests/check_cliff_position.py

One-shot verification that cliff_position values landed correctly.
Not committed — used to gate commit 1.

Outputs:
  1. Populated-envelope sanity: cliff_position populated count must
     equal cliff_metric populated count.
  2. Bucket distribution: early (≤0.30) / mid (between) / late (≥0.70).
  3. Spot-check: 6 games stratified by position with their full Phase 1a
     metric context — name, type, percent values, position, size.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import database as db


def main():
    with db.get_db() as conn:
        rows = conn.execute("""
            SELECT name, appid, game_type, completion_rate,
                   completion_rate_confidence, cliff_metric, cliff_position,
                   stickiness_ratio, review_playtime_median
            FROM games
            WHERE is_active = 1
        """).fetchall()
    total = len(rows)

    print("=" * 78)
    print(f"  cliff_position spot-check — {total} active games")
    print("=" * 78)

    cliff_populated = [r for r in rows if r["cliff_metric"] is not None]
    pos_populated = [r for r in rows if r["cliff_position"] is not None]

    print()
    print("--- 1. Populated-envelope sanity ---")
    print(f"  cliff_metric populated:   {len(cliff_populated)}/{total}")
    print(f"  cliff_position populated: {len(pos_populated)}/{total}")
    if len(cliff_populated) != len(pos_populated):
        print("  [VIOLATION] envelopes diverge — _largest_cliff disagreement")
    else:
        print("  [OK] envelopes match")

    # Cross-check: every game with cliff_metric should have cliff_position
    metric_appids = {r["appid"] for r in cliff_populated}
    pos_appids = {r["appid"] for r in pos_populated}
    diff = metric_appids ^ pos_appids
    if diff:
        print(f"  [VIOLATION] {len(diff)} appids in symmetric difference: {list(diff)[:5]}…")
    else:
        print("  [OK] same appid set populated for both")

    # ----- 2. Bucket distribution -----
    early = [r for r in pos_populated if r["cliff_position"] <= 0.30]
    mid = [r for r in pos_populated if 0.30 < r["cliff_position"] < 0.70]
    late = [r for r in pos_populated if r["cliff_position"] >= 0.70]

    print()
    print("--- 2. cliff_position bucket distribution ---")
    n = len(pos_populated)
    if n:
        print(f"  early (≤ 0.30):  {len(early):>4d}  ({100*len(early)/n:.1f}%)")
        print(f"  mid  (0.30, 0.70): {len(mid):>4d}  ({100*len(mid)/n:.1f}%)")
        print(f"  late (≥ 0.70):   {len(late):>4d}  ({100*len(late)/n:.1f}%)")

    # ----- 3. Spot-check: 2 from each bucket -----
    print()
    print("--- 3. Spot-check samples (2 per bucket) ---")
    print()
    for label, rows_subset in (("EARLY", early), ("MID", mid), ("LATE", late)):
        print(f"  {label}:")
        # Sort by cliff_metric desc within bucket — pick the most extreme
        # examples since the cliff is most informative there.
        sorted_subset = sorted(rows_subset, key=lambda r: -r["cliff_metric"])
        for r in sorted_subset[:2]:
            conf = r["completion_rate_confidence"] or "—"
            cr = f"{r['completion_rate']:.3f}" if r["completion_rate"] is not None else "—"
            print(f"    {r['name'][:42]!r:44s}  "
                  f"type={r['game_type'] or '?':14s}  "
                  f"size={r['cliff_metric']:5.1f}pp  "
                  f"pos={r['cliff_position']:.3f}  "
                  f"compl={cr} ({conf})")
        print()


if __name__ == "__main__":
    main()
