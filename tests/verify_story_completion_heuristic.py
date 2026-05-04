"""Throwaway: surface achievement-list heuristic results for human review.

Run with: uv run python tests/verify_story_completion_heuristic.py

For each of 10 named games, fetches Steam global achievement percentages,
prints the full sorted list, and marks which achievement the heuristic
identified as story-completion (vs the lowest-percent fallback).

Self-flags any game where the heuristic-picked achievement looks wrong:
  - it's a launch achievement (>50% unlock — way too common)
  - the lowest-percent achievement isn't the one matched and would
    arguably be a better completion proxy

Used as a one-shot gate before committing checkpoint 1 of v3 hook-point
Phase 1a. Delete after the heuristic is approved.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from app.fetchers.steam_achievements import fetch_achievements_with_metadata
from app.hook_metrics import (
    STORY_COMPLETION_PATTERNS,
    compute_completion_rate,
    compute_completion_rate_confidence,
    find_story_completion_achievement,
)


# Games to verify — user-specified. Search by name in the live library so
# we don't hardcode appids that might shift between users.
TARGET_NAMES = [
    "Persona 5 Royal",
    "DARK SOULS™ III",
    "Hades",
    "The Witcher 3",
    "Portal 2",
    "Disco Elysium",
    "Mass Effect",   # ME2 also fine
    "The Talos Principle",
    "The Stanley Parable",
    "Firewatch",
]


def _resolve_appids():
    """Match each TARGET_NAMES entry against the live library by substring."""
    from app import database as db
    with db.get_db() as conn:
        rows = conn.execute("SELECT appid, name FROM games").fetchall()
    by_name = [(r["appid"], r["name"]) for r in rows]
    resolved: list = []
    for needle in TARGET_NAMES:
        nl = needle.lower()
        matches = [(appid, name) for appid, name in by_name if nl in name.lower()]
        if matches:
            # Prefer exact-ish match (shortest name often wins for franchises).
            matches.sort(key=lambda x: len(x[1]))
            resolved.append(matches[0])
        else:
            resolved.append((None, needle + " (NOT IN LIBRARY)"))
    return resolved


def _label(ach: dict) -> str:
    """Display label for an achievement: prefer displayName, fall back to internal name."""
    return ach.get("displayName") or ach.get("name", "?")


def _flag_suspicious(picked: dict, achievements: list, used_fallback: bool) -> list:
    """Self-flag potentially-wrong heuristic picks. Returns a list of warnings."""
    warnings: list = []
    if picked is None:
        return warnings
    if picked["percent"] > 50.0:
        warnings.append(
            f"⚠ matched achievement has {picked['percent']:.1f}% unlock — likely a launch achievement, not endgame"
        )
    if not used_fallback:
        lowest = min(achievements, key=lambda a: a["percent"])
        if lowest["percent"] < picked["percent"] - 5.0:
            # Significant gap below the heuristic match. If the lower one
            # ALSO matches story-completion keywords (in either displayName
            # or name), the heuristic correctly preferred it via lowest-among-
            # matches. Otherwise the pattern set may be missing whatever
            # word the lower achievement uses.
            lowest_label_lower = _label(lowest).lower()
            lowest_matches = any(p in lowest_label_lower for p in STORY_COMPLETION_PATTERNS)
            if not lowest_matches:
                warnings.append(
                    f"⚠ lowest-% achievement is {_label(lowest)!r} at {lowest['percent']:.1f}% "
                    f"— heuristic picked a higher-% match ({_label(picked)!r} at {picked['percent']:.1f}%); "
                    f"if the lower one is the actual credits/end, pattern set may need updating"
                )
    if used_fallback and picked["percent"] > 10.0:
        # Fallback chosen because no pattern matched; if the lowest is high,
        # the metric is probably noise (no real "rare endgame" achievement).
        warnings.append(
            f"⚠ fallback (lowest-%) is {picked['percent']:.1f}% — high enough that "
            f"this metric is probably noise for this game (no clear endgame achievement)"
        )
    return warnings


async def main():
    targets = _resolve_appids()
    async with httpx.AsyncClient() as client:
        for appid, name in targets:
            print()
            print("=" * 78)
            print(f"  {name} (appid={appid})")
            print("=" * 78)
            if appid is None:
                print("  not in library — skipping")
                continue

            achievements = await fetch_achievements_with_metadata(client, appid)
            if achievements is None:
                print("  no achievements data (game has no achievements or fetch failed)")
                continue

            schema_resolved = sum(1 for a in achievements if a.get("displayName") and a["displayName"] != a["name"])
            schema_status = (
                f"schema OK ({schema_resolved}/{len(achievements)} display names resolved)"
                if schema_resolved else
                "schema unavailable — falling back to internal IDs"
            )

            picked = find_story_completion_achievement(achievements)
            used_fallback = picked is None
            if used_fallback:
                picked = min(achievements, key=lambda a: a["percent"])

            rate = compute_completion_rate(achievements)
            confidence = compute_completion_rate_confidence(achievements)
            mode_label = "FALLBACK (lowest %)" if used_fallback else "HEURISTIC MATCH"

            print(f"  total achievements: {len(achievements)} | {schema_status}")
            print(f"  pick: {_label(picked)!r} @ {picked['percent']:.2f}% [{mode_label}]")
            if _label(picked) != picked["name"]:
                print(f"        (internal name: {picked['name']})")
            print(f"  → completion_rate = {rate:.4f}" if rate is not None else "  → completion_rate = None")
            print(f"  → confidence      = {confidence}")

            for w in _flag_suspicious(picked, achievements, used_fallback):
                print(f"  {w}")

            # Print the full list (sorted by percent desc) using displayName when available.
            print()
            print("  full achievement list (percent desc):")
            for ach in achievements:
                marker = "  → " if ach["name"] == picked["name"] else "    "
                print(f"  {marker}{ach['percent']:6.2f}%  {_label(ach)}")


if __name__ == "__main__":
    asyncio.run(main())
