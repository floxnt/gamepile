"""One-shot force refresh runner — populates Phase 1a metrics on the live DB.

Run with: uv run python tests/run_force_refresh.py

Imports app.sync.run_refresh(force=True) directly rather than going
through the FastAPI server, since this is a one-time data-population
job (not a normal user-triggered refresh). Writes progress to stdout
every 10 seconds so we can monitor without blocking.

Per the design contract: refresh writes data to the live DB. This is
NOT a test mutation — it's the deliberate population of the new
Phase 1a fields. Running it on the live DB is the intended deployment
of the feature.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from app import sync  # noqa: E402


async def _print_progress():
    """Periodically dump progress while the refresh runs."""
    while sync.progress.running:
        p = sync.progress
        print(
            f"[{int(time.monotonic())}s] phase={p.phase!r} "
            f"game={p.current_game!r} ({p.current_index}/{p.total_games}) "
            f"hltb_matched={p.hltb_matched} ach_fetched={p.achievements_fetched} "
            f"ach_no_data={p.achievements_no_data} ach_skipped={p.achievements_skipped}",
            flush=True,
        )
        await asyncio.sleep(10)


async def main():
    print("Starting force refresh — Phase 1a metrics will populate.", flush=True)
    refresh_task = asyncio.create_task(sync.run_refresh(force=True))
    monitor_task = asyncio.create_task(_print_progress())
    await refresh_task
    monitor_task.cancel()
    p = sync.progress
    print()
    print("=" * 70)
    print("Refresh complete.")
    print(f"  elapsed: {p.elapsed_seconds:.1f}s")
    print(f"  games_added={p.games_added} games_updated={p.games_updated}")
    print(f"  HLTB: matched={p.hltb_matched} missed={p.hltb_missed} skipped={p.hltb_skipped}")
    print(f"  achievements: fetched={p.achievements_fetched} no_data={p.achievements_no_data} skipped={p.achievements_skipped}")
    print(f"  errors: {len(p.errors)}")


if __name__ == "__main__":
    asyncio.run(main())
