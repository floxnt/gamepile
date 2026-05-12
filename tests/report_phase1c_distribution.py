"""Phase 1c categorical signal distribution + target comparison.

Run with: uv run python tests/report_phase1c_distribution.py

Read-only against the live DB. Reports:
  1. Overall badge counts under the Phase 1c weighted-score model.
  2. Per-game-type breakdown — confirms type-aware routing.
  3. Phase 1b → Phase 1c shift (Phase 1b numbers loaded from
     PROJECT_STATE.md historical record).
  4. Target envelope check: 10–25% Hooks, 10–20% Filters early,
     ≤ 10% Marathon, Mixed + Standard ≤ 50%, ≤ 20% Limited.
  5. Diagnostic samples (3 per badge).
"""

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import database as db
from app.game_type import ALL_GAME_TYPES, resolve_type
from app.hook_metrics import (
    BADGE_FILTERS_EARLY,
    BADGE_HOOKS_PLAYERS,
    BADGE_LIMITED_DATA,
    BADGE_MARATHON,
    BADGE_MIXED_SIGNALS,
    BADGE_OFTEN_FILTERS,
    BADGE_STANDARD_ENGAGEMENT,
    BADGE_USUALLY_HOOKS,
    compute_stickiness_signal,
)


_BADGE_LABELS = {
    BADGE_HOOKS_PLAYERS:       "Hooks players",
    BADGE_USUALLY_HOOKS:       "Usually hooks",
    BADGE_FILTERS_EARLY:       "Filters early",
    BADGE_OFTEN_FILTERS:       "Often filters",
    BADGE_MARATHON:            "Marathon",
    BADGE_MIXED_SIGNALS:       "Mixed signals",
    BADGE_STANDARD_ENGAGEMENT: "Standard engagement",
    BADGE_LIMITED_DATA:        "Limited data",
}

_BADGE_ORDER = (
    BADGE_HOOKS_PLAYERS,
    BADGE_USUALLY_HOOKS,
    BADGE_FILTERS_EARLY,
    BADGE_OFTEN_FILTERS,
    BADGE_MARATHON,
    BADGE_MIXED_SIGNALS,
    BADGE_STANDARD_ENGAGEMENT,
    BADGE_LIMITED_DATA,
)

# Phase 1b shipped distribution from PROJECT_STATE.md (session 2026-05-05).
# Frozen — these numbers don't change. The 4-bucket Phase 1b model has
# been retired; this is its historical record for the migration diff.
_PHASE_1B_DISTRIBUTION = {
    "Sticky":               (27, "→ Phase 1c Hooks players"),
    "Filters players hard": (11, "→ Phase 1c Filters early"),
    "Average":              (377, "→ split into Marathon / Mixed / Standard"),
    "Insufficient data":    (219, "→ Phase 1c Limited data"),
}
_PHASE_1B_TOTAL = sum(n for n, _ in _PHASE_1B_DISTRIBUTION.values())


# Target envelope per Phase 1c spec.
def _target_check(label: str, count: int, total: int) -> tuple:
    pct = 100 * count / total if total else 0
    # Target bands refined after Phase 1c initial-ship measurement: the
    # Filters band reflects the post-threshold-tuning expected count;
    # Limited-data ceiling acknowledges the ~12.7% floor from ineligible
    # game types alone (beta_playtest + early_access + unknown +
    # software) plus sparse-review older catalog. Mixed+Standard ceiling
    # accepts the library's actual middle-of-the-road shape rather than
    # forcing it down via ad-hoc threshold gymnastics.
    targets = {
        BADGE_HOOKS_PLAYERS:       (10, 25, "10–25%"),
        BADGE_FILTERS_EARLY:       (5, 15, "5–15%"),
        BADGE_MARATHON:            (None, 10, "≤ 10%"),
        BADGE_LIMITED_DATA:        (None, 30, "≤ 30%"),
    }
    if label not in targets:
        return (pct, None, None)
    lo, hi, target_str = targets[label]
    in_band = (lo is None or pct >= lo) and (hi is None or pct <= hi)
    return (pct, target_str, in_band)


def main():
    with db.get_db() as conn:
        gws_rows = db.get_games_with_state(conn, active_only=True)
    games = [g.game for g in gws_rows]
    total = len(games)

    print("=" * 78)
    print(f"  Phase 1c stickiness signal distribution — {total} active games")
    print("=" * 78)

    overall: Counter = Counter()
    by_type: dict = defaultdict(Counter)
    samples: dict = defaultdict(list)
    for game in games:
        badge, score, _ = compute_stickiness_signal(game)
        gt = resolve_type(game)
        overall[badge] += 1
        by_type[gt][badge] += 1
        if len(samples[badge]) < 3:
            samples[badge].append((game.appid, game.name, score, gt))

    # ----- 1. Overall counts -----
    print()
    print("--- 1. Overall badge counts (Phase 1c) ---")
    print()
    for badge in _BADGE_ORDER:
        n = overall.get(badge, 0)
        pct = 100 * n / total if total else 0
        print(f"  {_BADGE_LABELS[badge]:<22s}  {n:>4d}  ({pct:5.1f}%)")

    # ----- 2. Per-game-type breakdown -----
    print()
    print("--- 2. Per-game-type breakdown ---")
    print()
    print(f"  {'type':<16s} {'n':>4s}  "
          f"{'Hooks':>5s} {'Usu':>3s}  {'Filt':>4s} {'Oft':>3s}  "
          f"{'Mara':>4s}  {'Mixed':>5s}  {'Std':>4s}  {'Lim':>4s}")
    print("  " + "-" * 72)
    for gt in ALL_GAME_TYPES:
        type_counts = by_type.get(gt) or Counter()
        n = sum(type_counts.values())
        if n == 0:
            continue
        h = type_counts.get(BADGE_HOOKS_PLAYERS, 0)
        u = type_counts.get(BADGE_USUALLY_HOOKS, 0)
        f = type_counts.get(BADGE_FILTERS_EARLY, 0)
        o = type_counts.get(BADGE_OFTEN_FILTERS, 0)
        m = type_counts.get(BADGE_MARATHON, 0)
        x = type_counts.get(BADGE_MIXED_SIGNALS, 0)
        s = type_counts.get(BADGE_STANDARD_ENGAGEMENT, 0)
        l = type_counts.get(BADGE_LIMITED_DATA, 0)
        print(f"  {gt:<16s} {n:>4d}  "
              f"{h:>5d} {u:>3d}  {f:>4d} {o:>3d}  "
              f"{m:>4d}  {x:>5d}  {s:>4d}  {l:>4d}")

    # ----- 3. Phase 1b → Phase 1c shift -----
    print()
    print("--- 3. Phase 1b → Phase 1c migration ---")
    print()
    print(f"  {'Phase 1b badge':<22s}  {'count':>5s}  notes")
    print("  " + "-" * 70)
    for label, (n, note) in _PHASE_1B_DISTRIBUTION.items():
        pct = 100 * n / _PHASE_1B_TOTAL
        print(f"  {label:<22s}  {n:>5d} ({pct:.1f}%)  {note}")
    print()
    print(f"  {'Phase 1c badge':<22s}  {'count':>5s}  delta vs Phase 1b natural twin")
    print("  " + "-" * 70)
    twins = (
        ("Hooks players",       BADGE_HOOKS_PLAYERS,       "Sticky"),
        ("Filters early",       BADGE_FILTERS_EARLY,       "Filters players hard"),
        ("Limited data",        BADGE_LIMITED_DATA,        "Insufficient data"),
    )
    for c1c_label, c1c_badge, c1b_label in twins:
        n_now = overall.get(c1c_badge, 0)
        n_then, _ = _PHASE_1B_DISTRIBUTION.get(c1b_label, (0, ""))
        delta = n_now - n_then
        sign = "+" if delta >= 0 else ""
        print(f"  {c1c_label:<22s}  {n_now:>5d}        was {n_then} ({sign}{delta})")
    avg_split = (
        overall.get(BADGE_MARATHON, 0)
        + overall.get(BADGE_MIXED_SIGNALS, 0)
        + overall.get(BADGE_USUALLY_HOOKS, 0)
        + overall.get(BADGE_OFTEN_FILTERS, 0)
        + overall.get(BADGE_STANDARD_ENGAGEMENT, 0)
    )
    n_avg, _ = _PHASE_1B_DISTRIBUTION["Average"]
    delta_avg = avg_split - n_avg
    sign = "+" if delta_avg >= 0 else ""
    print(f"  Mar+Mix+Usu+Oft+Std    {avg_split:>5d}        was Average={n_avg} ({sign}{delta_avg})")

    # ----- 4. Target envelope check -----
    print()
    print("--- 4. Target envelope ---")
    print()
    print(f"  {'badge':<22s}  {'actual':>7s}  {'target':>10s}  {'verdict':>8s}")
    print("  " + "-" * 60)
    for badge in (BADGE_HOOKS_PLAYERS, BADGE_FILTERS_EARLY, BADGE_MARATHON, BADGE_LIMITED_DATA):
        n = overall.get(badge, 0)
        pct, target_str, in_band = _target_check(badge, n, total)
        verdict = "OK" if in_band else "MISS"
        print(f"  {_BADGE_LABELS[badge]:<22s}  {pct:>6.1f}%  {target_str:>10s}  {verdict:>8s}")

    mixed_std = (
        overall.get(BADGE_MIXED_SIGNALS, 0) + overall.get(BADGE_STANDARD_ENGAGEMENT, 0)
    )
    pct_ms = 100 * mixed_std / total if total else 0
    in_band = pct_ms <= 60
    verdict = "OK" if in_band else "MISS"
    print(f"  {'Mixed + Standard':<22s}  {pct_ms:>6.1f}%  {'≤ 60%':>10s}  {verdict:>8s}")

    # Standard-alone threshold check — flagged by the workbook brief as
    # the signal that score-formula revisiting may be needed if the lean
    # split doesn't pull enough games out of Standard.
    n_std = overall.get(BADGE_STANDARD_ENGAGEMENT, 0)
    pct_std = 100 * n_std / total if total else 0
    std_verdict = "OK" if pct_std <= 30 else "FLAG"
    print(f"  {'Standard (alone)':<22s}  {pct_std:>6.1f}%  {'≤ 30%':>10s}  {std_verdict:>8s}")

    # ----- 5. Diagnostic samples -----
    print()
    print("--- 5. Diagnostic samples (3 per badge) ---")
    print()
    for badge in _BADGE_ORDER:
        rows = samples.get(badge, [])
        if not rows:
            continue
        print(f"  {_BADGE_LABELS[badge]}:")
        for appid, name, score, gt in rows:
            print(f"    appid={appid:>10d}  type={gt:<14s}  "
                  f"score={score:+5.2f}  {name[:48]!r}")
        print()


if __name__ == "__main__":
    main()
