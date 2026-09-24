"""Browser smoke test: click the real UI in Chromium against a live server.

TestClient posts requests directly, so it can't catch bugs that only exist
in the browser: HTMX attribute mistakes, or the Origin header a webview
derives from the page's Referrer-Policy. Both shipped in 1.1.0 with every
suite green. This script is the automated half of the "look at it in the
running app" rule; it doesn't replace the native Windows/CachyOS check.

Not collected by run_tests.py (the name doesn't start with test_). Run:

    uv run --locked --with playwright python -m playwright install chromium
    uv run --locked --with playwright python tests/browser_smoke.py

Exit code 0 = all checks passed.
"""

import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright is not installed; see the usage note at the top of this file.")
    sys.exit(1)

import uvicorn

from app import credentials, database as db
from app.models import Game

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"


def _seed():
    now = datetime.utcnow()
    with db.get_db() as conn:
        for appid in (1, 2, 3):
            db.upsert_game(conn, Game(
                appid=appid, name=f"Smoke {appid}", playtime_minutes=600,
                last_played_steam=now - timedelta(days=2), installed=None,
                hltb_main_hours=20, hltb_main_extra_hours=30, hltb_completionist_hours=50,
                genres="Action", tags="", user_tags="Action", developer="Smoke Studio",
                publisher=None, metacritic_score=80, opencritic_score=None,
                steam_review_pct=90, steam_review_count=1000, last_refreshed=now,
                release_date=now - timedelta(days=800), game_type="linear",
            ))
            db.ensure_game_state(conn, appid, playtime_minutes=600,
                                 last_played_steam=now - timedelta(days=2))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gamepile-browser-"))
    failures = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    with patch.object(db, "DB_PATH", tmp / "smoke.db"), \
         patch.object(credentials, "has_complete_credentials", return_value=True):
        db.init_db()
        _seed()
        from app.main import app
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started:
            time.sleep(0.05)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            js_errors = []
            page.on("pageerror", lambda e: js_errors.append(str(e)))

            def attempt(name, fn):
                try:
                    fn()
                except Exception as exc:  # a dead button shows up as a timeout
                    check(name, False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:80]}")

            def post_click(selector):
                # Wait for the POST the click should send; a dead control times out.
                with page.expect_response(lambda r: r.request.method == "POST", timeout=5000):
                    page.click(selector)
                page.wait_for_timeout(200)  # let HTMX finish the swap

            # 1. A native (non-HTMX) form post must not be rejected.
            def setup_form():
                page.goto(BASE + "/setup/welcome")
                with page.expect_navigation():
                    page.click("form[action='/setup/welcome'] button")
                body = page.inner_text("body")
                check("setup form post accepted",
                      "Invalid origin" not in body and "Session expired" not in body, body[:60])
            attempt("setup form post accepted", setup_form)

            # 2. Decision Session buttons send requests, advance, and undo.
            def session():
                page.goto(BASE + "/backlog")
                with page.expect_navigation(url="**/backlog/session/**"):
                    page.click("button:has-text('Review')")
                before = page.inner_text(".session-progress")
                post_click("#session-content button:has-text('Skip')")
                after = page.inner_text(".session-progress")
                check("session Skip advances", before != after, f"{before!r} -> {after!r}")
                post_click("text=Undo last decision")
                now = page.inner_text(".session-progress")
                check("session Undo returns", now == before, f"{now!r} != {before!r}")
            attempt("session flow", session)

            # 3. Shortlist card actions render their result with Undo.
            def shortlist():
                page.goto(BASE + "/")
                page.click("#tab-find")
                with page.expect_response(lambda r: "/picks" in r.url):
                    page.click("text=Find Games")
                post_click("#main-content button:has-text('Not feeling it') >> nth=0")
                check("Not feeling it shows Undo",
                      page.query_selector(".action-result >> text=Undo") is not None)
            attempt("shortlist flow", shortlist)

            check("no uncaught JS errors", not js_errors, json.dumps(js_errors[:2]))
            browser.close()
        server.should_exit = True

    print("OK" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
