Tonight's Pick — Spec v1 + v2
Overview
A local desktop app (single-user, runs on my gaming PC) that helps me decide what to play from my Steam library. V1 is a deterministic, time-filtered recommender. V2 layers in community consensus signals and lightweight taste learning from my own feedback. V1 ships first and gets used for at least two weeks before v2 work begins.
Stack & packaging (applies to both phases)

Language: Python 3.12+
Backend framework: FastAPI
Frontend: HTMX + Jinja2 server-rendered templates, vanilla CSS. No React, no build pipeline.
Database: SQLite, single file
Dependency management: uv during development
Distribution: PyInstaller-bundled single binary. Double-click to launch. Auto-opens default browser to localhost:<port> on startup. Data stored in ~/.local/share/tonights-pick/ (or $XDG_DATA_HOME/tonights-pick/).
Why not Docker: this is a single-user desktop utility, not a homelab service. No remote access needed, no other services to coordinate with.
Why not Rust: Python is the gentler learning curve, the workload is I/O-bound (Steam API calls, SQLite reads — not compute-bound), and the ecosystem fit is much better (httpx, fastapi, howlongtobeatpy all mature). Rust's strengths don't matter here.

Configuration
.env file in the data directory:

STEAM_API_KEY — Steam Web API key
STEAM_ID — 64-bit SteamID
PORT — defaults to 8765
REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT — v2 only, optional, app degrades gracefully without them


V1 — Ships first
Data model
games

appid (int, PK)
name (text)
playtime_minutes (int — Steam lifetime playtime)
last_played_steam (timestamp, nullable — from Steam)
installed (bool, nullable)
hltb_main_hours (float, nullable)
hltb_main_extra_hours (float, nullable)
hltb_completionist_hours (float, nullable)
genres (text — comma-separated, from Steam store)
tags (text — comma-separated, from Steam store user tags, top 10)
developer (text, nullable)
publisher (text, nullable)
metacritic_score (int, nullable)
opencritic_score (int, nullable — fetched in v1, used in v2)
steam_review_pct (int, nullable — % positive reviews)
steam_review_count (int, nullable)
last_refreshed (timestamp)

game_state

appid (int, PK, FK to games)
status (enum: never_played, in_progress, finished, dropped, not_interested)
hours_played_manual (float, nullable — user's own estimate for in-progress games)
notes (text, nullable)
updated_at (timestamp)

refresh_log

id (int, PK)
started_at (timestamp)
completed_at (timestamp, nullable)
games_added (int)
games_updated (int)
errors (text, nullable)

Routes
GET / — Tonight's Pick page

Inputs:

"How much time tonight?" — number input, minutes, default 90
Mode toggle: Short-term (fits in tonight's window) | Long-term (suggest a game worth committing to over multiple sessions, even if total time exceeds tonight)
Optional toggles: "include unplayed", "include in-progress", "installed only"


Output: 3-5 game cards. Each card shows cover art, name, status, estimated total hours, estimated remaining hours, last played, Metacritic/OpenCritic/Steam %, and two buttons: "I picked this" and "Not feeling it"
"I picked this" sets status to in_progress and records the pick in pick_history (table introduced in v2; in v1, just update status)

Recommendation algorithm — Short-term mode:

Filter by toggles
Estimate remaining time per game: if in_progress and hours_played_manual set → max(hltb_main_hours - hours_played_manual, 0.5); else → hltb_main_hours
Filter to games where remaining time fits within ±50% of the requested window
Score each candidate:

+2 if in_progress
+1 if last_played_steam is null or older than 30 days
+1 if Metacritic ≥ 85 OR OpenCritic ≥ 85 OR Steam review % ≥ 90 with ≥ 1000 reviews
-1 if same primary genre as another candidate already picked (variety bonus)


Sort by score descending, take top 5

Recommendation algorithm — Long-term mode:

Filter by toggles
Filter to games where hltb_main_hours ≥ 8 (worth committing to)
Score each candidate:

+3 if Metacritic ≥ 85 OR OpenCritic ≥ 85
+2 if Steam review % ≥ 90 with ≥ 1000 reviews
+1 if in_progress (continuation bias)
-2 if dropped (don't re-suggest games I bounced off)


Sort by score descending, take top 5

GET /library — Sortable/filterable table view

Columns: name, status, lifetime playtime, HLTB main, last played, Metacritic, OpenCritic, Steam %
Filter by status, genre, installed
Click row to edit state inline (HTMX swap)

POST /games/{appid}/state — Update state, returns swapped HTML row
POST /refresh — Manual refresh

Fetches owned games from Steam (IPlayerService/GetOwnedGames)
For each game, fetches store details (appdetails) for genres, tags, Metacritic, developer, publisher
For each game, fetches HLTB data via howlongtobeatpy
For each game, fetches OpenCritic score via OpenCritic's free API (api.opencritic.com/api/game/search)
Fetches Steam review summary via appreviews/{appid}?json=1&num_per_page=0
Returns HTMX-polled status page until complete
Handles rate limits (sleep between Steam calls), failures (log, continue), removed games (mark, don't delete state)

GET /healthz — {"status": "ok"}
V1 done criteria

Single binary launches, opens browser to working app
Manual refresh populates library from Steam, HLTB, OpenCritic, Steam reviews
Can mark games as in-progress/finished/dropped/etc.
/ returns sensible recommendations in both Short-term and Long-term modes
README documents setup and recommendation logic


V2 — Follows after v1 is in daily use for 2+ weeks
Premise
V1 makes recommendations based on static data and your status flags. V2 adds two things: a feedback loop that learns your taste from picks/drops, and (if Reddit credentials are configured) light scraping of Reddit for "hook point" mentions on dropped games. V2 must not break v1 — all existing v1 features keep working without v2 features enabled.
New tables
pick_history

id (int, PK)
appid (int, FK to games)
picked_at (timestamp)
time_window_minutes (int — the "how much time tonight" value when picked)
mode (enum: short_term, long_term)
outcome (enum: pending, completed, dropped, still_playing, nullable)
outcome_recorded_at (timestamp, nullable)
hours_played_when_recorded (float, nullable)

tag_affinity

tag (text, PK — a genre, user tag, or developer name)
tag_type (enum: genre, tag, developer)
weight (float — starts at 0, adjusts up/down based on feedback)
updated_at (timestamp)

hook_point_data (only populated if Reddit creds present)

appid (int, PK)
hook_hour (float, nullable — extracted "game gets good around hour X" estimate)
confidence (enum: low, medium, high — based on how many sources agree)
source_summary (text — brief notes on what was found)
last_checked (timestamp)

Feedback loop
After picking a game from /, the next time the user opens the app, surface a small "How did it go?" prompt for the most recent pending pick:

"Still playing" → no affinity change yet
"Finished it" → +2 to all affinities for that game's genres/tags/developer
"Loved it, kept playing" → +1 to all affinities (catches "I'm 20 hours in and not done")
"Dropped it" → prompt for hours played, then -1 to all affinities, weighted by how early they dropped (dropped at 1 hour = -2, dropped at 10 hours = -0.5)
"Skip" → no change

Affinity weights are clamped to [-10, +10] so a single bad experience doesn't poison a category permanently.
Updated scoring (both modes)
Add to the existing v1 score:

For each of the game's genres/tags/developer: add tag_affinity.weight × multiplier (multiplier = 0.3 for tags, 0.5 for genres, 0.7 for developer — developer signal is strongest because it's more specific)
Cap the affinity contribution at ±5 per game so taste learning doesn't completely override community consensus and time-fit

Hook point integration (Reddit-dependent, optional)
When a game is dropped via the feedback prompt:

If Reddit creds present and hook_point_data for this appid is missing or > 90 days old, queue a background lookup
Background task uses praw to search /r/{game_subreddit} and /r/patientgamers for posts containing the game name + phrases like "gets good", "hook", "worth it after", "stick with it"
Pulls top 10 matching posts, sends them to Claude Haiku 4.5 (or DeepSeek V3.2) with a prompt: "Based on these Reddit posts, at roughly what hour mark does this game become engaging? Respond with a single number or 'unclear'."
Stores result in hook_point_data

When showing a recommendation in /, if hook_point_data exists for that game, surface it on the card: "Community consensus: clicks around hour 4." For dropped games where the user dropped before the hook point, surface a gentle prompt on the library page: "You dropped this at 2 hours. Most players say it clicks around hour 5 — want to reconsider?"
Constraints on Reddit/LLM use:

Free Reddit API tier limits to 100 queries per minute per OAuth client. App enforces a hard 30 queries/minute internal cap to stay well under.
LLM calls cost roughly $0.001 per game looked up. Cache aggressively (90-day refresh). User can disable the feature entirely in config.
If Reddit creds aren't configured, the entire hook-point system is dormant — no errors, no degraded UX, just no hook point data on cards.

V2 done criteria

Pick history is recorded and feedback prompts surface correctly
Tag affinities adjust based on outcomes and visibly affect future recommendations (you should be able to inspect the tag_affinity table and see why a recommendation was made)
If Reddit creds are configured, hook point data is fetched and displayed
All v1 functionality continues working unchanged


Out of scope (both phases)
User accounts, mobile UI, cross-platform game tracking (Steam only), scheduled background jobs (manual refresh only — Reddit lookups are opportunistic, triggered by drop events), any LLM use beyond the optional hook-point extraction in v2.
Code style constraints

Readable by someone learning Python — favor clarity over cleverness
Reasonable module organization (don't dump everything in main.py)
Each phase committed as a clearly tagged release — v1.0 ships before any v2 work begins
v2 features should be feature-flagged so they can be disabled without code changes
