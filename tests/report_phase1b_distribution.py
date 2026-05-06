"""Phase 1b categorical signal distribution across the live DB.
Read-only — no mutations.

Run with: uv run python tests/report_phase1b_distribution.py

Reports:
  1. Overall badge counts (Sticky / Average / Filters players hard / Insufficient data)
  2. Per-game-type breakdown — confirms the type-aware fallback paths
     (multiplayer/mmo/no_endpoint/sandbox single-signal eval, and
     beta_playtest/early_access/unknown/software always Insufficient)
  3. Diagnostic samples — first 5 games per badge for spot-checking
"""

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import database as db
from app.game_type import ALL_GAME_TYPES, resolve_type
from app.hook_metrics import (
    BADGE_AVERAGE,
    BADGE_FILTERS_HARD,
    BADGE_INSUFFICIENT_DATA,
    BADGE_STICKY,
    compute_stickiness_signal,
)


_BADGE_LABELS = {
    BADGE_STICKY:            "Sticky",
    BADGE_AVERAGE:           "Average",
    BADGE_FILTERS_HARD:      "Filters players hard",
    BADGE_INSUFFICIENT_DATA: "Insufficient data",
}

_BADGE_ORDER = (BADGE_STICKY, BADGE_AVERAGE, BADGE_FILTERS_HARD, BADGE_INSUFFICIENT_DATA)


def main():
    with db.get_db() as conn:
        gws_rows = db.get_games_with_state(conn, active_only=True)
    games = [g.game for g in gws_rows]
    total = len(games)

    print("=" * 78)
    print(f"  Phase 1b stickiness signal distribution — {total} active games")
    print("=" * 78)

    # ----- 1. Overall counts -----
    overall: Counter = Counter()
    by_type: dict = defaultdict(Counter)
    samples: dict = defaultdict(list)

    for game in games:
        badge, sticky_n, filters_n = compute_stickiness_signal(game)
        gt = resolve_type(game)
        overall[badge] += 1
        by_type[gt][badge] += 1
        if len(samples[badge]) < 5:
            samples[badge].append((game.appid, game.name, sticky_n, filters_n, gt))

    print()
    print("--- 1. Overall badge counts ---")
    print()
    for badge in _BADGE_ORDER:
        n = overall.get(badge, 0)
        pct = 100 * n / total if total else 0
        print(f"  {_BADGE_LABELS[badge]:<22s}  {n:>4d}  ({pct:5.1f}%)")

    # ----- 2. Per-game-type breakdown -----
    print()
    print("--- 2. Per-game-type breakdown ---")
    print()
    print(f"  {'type':<16s} {'n':>4s}   "
          f"{'Sticky':>6s}  {'Avg':>6s}  {'Filt':>6s}  {'Insuf':>6s}")
    print("  " + "-" * 60)

    for gt in ALL_GAME_TYPES:
        type_counts = by_type.get(gt) or Counter()
        n = sum(type_counts.values())
        if n == 0:
            continue
        s = type_counts.get(BADGE_STICKY, 0)
        a = type_counts.get(BADGE_AVERAGE, 0)
        f = type_counts.get(BADGE_FILTERS_HARD, 0)
        i = type_counts.get(BADGE_INSUFFICIENT_DATA, 0)
        print(f"  {gt:<16s} {n:>4d}   "
              f"{s:>6d}  {a:>6d}  {f:>6d}  {i:>6d}")

    # ----- 3. Diagnostic samples -----
    print()
    print("--- 3. Diagnostic samples (first 5 per badge) ---")
    print()
    for badge in _BADGE_ORDER:
        rows = samples.get(badge, [])
        if not rows:
            continue
        print(f"  {_BADGE_LABELS[badge]}:")
        for appid, name, sn, fn, gt in rows:
            print(f"    appid={appid:>10d}  type={gt:<14s}  "
                  f"sticky={sn} filters={fn}  {name[:48]!r}")
        print()

    # ----- 4. Sanity: ineligible types should always be Insufficient -----
    print("--- 4. Sanity check — ineligible types ---")
    print()
    ineligible = ("beta_playtest", "early_access", "unknown", "software")
    for gt in ineligible:
        counts = by_type.get(gt) or Counter()
        n = sum(counts.values())
        if n == 0:
            continue
        non_insuf = sum(v for k, v in counts.items() if k != BADGE_INSUFFICIENT_DATA)
        marker = "OK" if non_insuf == 0 else "VIOLATION"
        print(f"  {gt:<16s} n={n:>3d}  non-insufficient={non_insuf}  [{marker}]")


if __name__ == "__main__":
    main()
