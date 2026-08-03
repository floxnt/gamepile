"""First-run sync status tests.

Run with: uv run python tests/test_sync_status.py

The setup page polls /setup/sync-status every 2s and decides from the
response whether to keep polling, redirect into the app, or stop. Two
bugs lived in that decision:

  1. run_refresh's finally block stamps completed_at unconditionally, so
     a crashed sync satisfied "completed_at is not None" and rendered as
     success — redirecting the user into a half-populated app with no
     indication anything went wrong.

  2. If the task died before reaching that finally (collected mid-flight
     because asyncio only held a weak reference, or cancelled), nothing
     was stamped at all and the page polled "Starting…" forever with no
     error and no way out.

These tests pin both, plus the escape hatch that keeps a wedged first
run from being a dead end.

No pytest dependency — pure assertions. Nothing here touches a database.
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient

from app import sync
from app.main import app
from app.routes import setup as setup_routes


_client = TestClient(app)


def _set_state(task=None, **progress_kw) -> None:
    sync.progress = sync.RefreshProgress(**progress_kw)
    setup_routes._sync_task = task


def _render() -> str:
    return _client.get("/setup/sync-status").text


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --- failure detection ------------------------------------------------

def test_fatal_error_is_reported():
    _set_state(running=False, completed_at=datetime(2026, 1, 1),
               fatal_error="Steam API unreachable")
    assert setup_routes._sync_failure() == "Steam API unreachable"


def test_healthy_completion_is_not_a_failure():
    _set_state(running=False, completed_at=datetime(2026, 1, 1))
    assert setup_routes._sync_failure() is None


def test_running_sync_is_not_a_failure():
    _set_state(running=True, started_at=datetime(2026, 1, 1))
    assert setup_routes._sync_failure() is None


def test_not_yet_started_is_not_a_failure():
    """Between create_task and the coroutine's first step, progress is
    still at its defaults. That must read as 'starting', not 'failed'."""
    _set_state()
    assert setup_routes._sync_failure() is None


def test_task_that_raised_is_reported():
    async def scenario():
        async def die():
            raise RuntimeError("connection reset")
        task = asyncio.create_task(die())
        try:
            await task
        except RuntimeError:
            pass
        _set_state(task=task)
        return setup_routes._sync_failure()
    assert _run(scenario()) == "connection reset", _run(scenario())


def test_task_that_returned_without_finishing_is_reported():
    """Died before run_refresh's finally — nothing stamped completed_at."""
    async def scenario():
        async def noop():
            return None
        task = asyncio.create_task(noop())
        await task
        _set_state(task=task)
        return setup_routes._sync_failure()
    result = _run(scenario())
    assert result and "stopped unexpectedly" in result, result


def test_cancelled_task_is_reported():
    async def scenario():
        async def slow():
            await asyncio.sleep(30)
        task = asyncio.create_task(slow())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        _set_state(task=task)
        return setup_routes._sync_failure()
    result = _run(scenario())
    assert result and "cancelled" in result, result


def test_unfinished_task_does_not_mask_a_running_sync():
    async def scenario():
        async def slow():
            await asyncio.sleep(30)
        task = asyncio.create_task(slow())
        _set_state(task=task, running=True)
        out = setup_routes._sync_failure()
        task.cancel()
        return out
    assert _run(scenario()) is None


# --- rendered branches ------------------------------------------------

def test_crashed_sync_does_not_render_as_success():
    """The regression that mattered most: a fatal error used to render
    the success panel and auto-redirect."""
    _set_state(running=False, completed_at=datetime(2026, 1, 1),
               fatal_error="Steam API unreachable",
               errors=["Fatal: Steam API unreachable"])
    body = _render()
    assert "setup-sync-status--complete" not in body, "crashed sync rendered as complete"
    assert "window.location.href" not in body, "crashed sync auto-redirected"
    assert "setup-sync-status--failed" in body


def test_failed_branch_surfaces_the_reason():
    _set_state(running=False, completed_at=datetime(2026, 1, 1),
               fatal_error="Steam API unreachable")
    assert "Steam API unreachable" in _render()


def test_failed_branch_stops_polling():
    """Re-polling a dead sync just animates a spinner over a corpse."""
    _set_state(running=False, completed_at=datetime(2026, 1, 1),
               fatal_error="boom")
    assert 'hx-get="/setup/sync-status"' not in _render()


def test_failed_branch_offers_escape_and_retry():
    _set_state(running=False, completed_at=datetime(2026, 1, 1),
               fatal_error="boom")
    body = _render()
    assert "Continue to app anyway" in body
    assert "/setup/start-sync" in body, "no retry offered"


def test_running_branch_keeps_polling_and_offers_escape():
    """A sync wedged at running=True never reaches the failed branch —
    nothing can tell it from a slow one — so the way out lives here too."""
    _set_state(running=True, phase="Fetching library",
               started_at=datetime(2026, 1, 1))
    body = _render()
    assert 'hx-get="/setup/sync-status"' in body
    assert "Continue to app anyway" in body


def test_successful_sync_still_redirects():
    _set_state(running=False, completed_at=datetime(2026, 1, 1))
    body = _render()
    assert "setup-sync-status--complete" in body
    assert "window.location.href" in body
    assert "setup-sync-status--failed" not in body


TESTS = [
    test_fatal_error_is_reported,
    test_healthy_completion_is_not_a_failure,
    test_running_sync_is_not_a_failure,
    test_not_yet_started_is_not_a_failure,
    test_task_that_raised_is_reported,
    test_task_that_returned_without_finishing_is_reported,
    test_cancelled_task_is_reported,
    test_unfinished_task_does_not_mask_a_running_sync,
    test_crashed_sync_does_not_render_as_success,
    test_failed_branch_surfaces_the_reason,
    test_failed_branch_stops_polling,
    test_failed_branch_offers_escape_and_retry,
    test_running_branch_keeps_polling_and_offers_escape,
    test_successful_sync_still_redirects,
]


def main() -> int:
    print(f"Running {len(TESTS)} test(s)…")
    failures = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  ✗ {fn.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ✗ {fn.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\n{failures}/{len(TESTS)} failed")
        return 1
    print(f"\nAll {len(TESTS)} tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
