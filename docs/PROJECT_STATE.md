# Project State — GamePile

## What this app is

A local desktop app that helps the user manage their Steam backlog
and decide what to play. The Shortlist feature (the "suggest 5 games"
recommender, formerly "Tonight's Pick") is the headline interactive
mode, but the broader purpose is backlog management and progress
tracking.

Single user, single machine, no central server, no cloud sync.

Distribution intent: by v5/v6, friend-shareable as a portable
binary on Windows and Linux.

Differentiation from existing tools (e.g., Backloggd): GamePile is
action-oriented — what should you play next? — rather than logging-
oriented. The weighted recommendation engine and personalized taste
learning are the moat.

## Naming

- **GamePile** — the app
- **Shortlist** — the recommender feature/tab inside GamePile

## Stack

- Python 3.12+, FastAPI backend
- HTMX + Jinja2 templates, vanilla CSS, no JS frameworks
- SQLite at `~/.local/share/gamepile/gamepile.db`
  (legacy paths to migrate from: `~/.local/share/tonights-pick/`,
  `~/.local/share/game-roulette/`)
- pywebview for native window (replaces browser-as-UI)
- uv for development dependency management
- PyInstaller (--onedir mode) planned for v5 binary distribution

## Current architecture

Modules:
- `app/main.py` — FastAPI app, startup, browser launch, /healthz
- `app/config.py` — env loading, settings
- `app/database.py` — schema, raw sqlite3, no ORM
- `app/models.py` — dataclasses for DB rows
- `app/recommender.py` — Shortlist scoring algorithms, mode-specific logic
- `app/affinity.py` — taste learning read + write paths
- `app/sync.py` — refresh orchestration
- `app/prompt_state.py` — in-session feedback prompt dismissal
- `app/routes/{pick,library,refresh,feedback}.py` — route handlers
  (note: `pick.py` will be renamed to `shortlist.py` as part of v2.5
  rename work, with route prefix changing from `/` to `/shortlist`)
- `app/fetchers/{steam,hltb,steamspy}.py` — external API clients
- `app/templates/` + `app/static/` — UI

## What's built (v1 + v2)

- Steam library refresh with HLTB, SteamSpy enrichment (OpenCritic removed in v3)
- Shortlist recommendations across modes
- Pick history with full context (mode, candidates, time window)
- Affinity tracking (genres, user tags, developers)
- Structured 4-step feedback flow + did-not-play sub-branch
- Recent-picks tracker view as the home page default
- Card actions: I picked this / Not feeling it / Already completed /
  Never recommend / Finished / Bounced off it / Not my thing
- Hard blacklist via `game_state.blacklisted`
- Soft vs strong drop signal via `game_state.dropped_strength`
- HLTB caching with 30-day TTL
- Tiebreaker randomization (±1.0) on Shortlist scoring
- Recent-picks dedup by appid

## In-flight work (v2.5)

- Project rename to GamePile (app) and Shortlist (feature) across
  codebase, docs, config paths, route prefixes
- Status inference fix: add `played_unclassified` state, properly
  classify games with non-zero Steam playtime instead of leaving
  them as `never_played`
- Five user-intent mode labels replacing the four algorithm-named modes
- "Comfort pick" mode added with playtime-driven scoring
- Library view column refinement (drop AppID, drop genres in favor of
  tags, drop redundant Steam Tier column, add Status and Metacritic columns)

## Known issues / not yet fixed

- Various design refinements from testing (collected in bug rundown)

## Deferred to v3

- Backlog view as a top-level page (separate from Library)
- Dashboard view with aggregate stats
- Per-game detail page with notes, pick history, manual overrides
- Data model refactor: separate tables for Steam facts, external
  metadata, user state, taste model
- HLTB game type column added to Library (linear / multiplayer focus
  / no defined endpoint / mixed)
- Hook-point and stickiness signals (see below)

## v3 — Hook-point and stickiness signals

Compose multiple sources into a "Hook point" range and a "Stickiness"
categorical signal per game. Each phase ships independently.

**Phase 1 — Stickiness signals (always available):**
For every game, compute:
- `completion_rate` from achievements (where identifiable; lowest unlock %
  matching story-completion patterns; fallback: lowest unlock % overall)
- `cliff_metric` (largest single drop between consecutive achievements
  when sorted by unlock %, after discarding top 2-3 to filter out launch
  achievements)
- `review_playtime_median` (median hours-at-review across all Steam reviews;
  data already fetched in v2, currently discarded)
- `stickiness_ratio` (% of reviewers with 20+ hours played at time of review)
- `playtime_median_avg_ratio` (SteamSpy median_forever / average_forever;
  high ratio = even engagement, low ratio = long-tail bounce pattern).
  Already pulling SteamSpy for tags — adding these fields is essentially free.

Combine into a categorical stickiness signal:
- "Sticky" — high completion or small cliff or high stickiness ratio
- "Filters players hard" — low completion or large cliff or low stickiness ratio
- "Average" — middle of the road
- "Insufficient data" — game has too few reviews and no achievements

Surface on cards as a one-line categorical badge.

**Phase 2 — Hour-mapped curve from progression achievements:**
- Identify progression achievements via name patterns ("Chapter X",
  "Act X", "Episode X", "Mission X", "Part X", boss names)
- Where derivable, compute curve: each progression achievement gets
  estimated_hour = (index / total_progressions) * hltb_main_hours
- Pair with unlock %, identify sharpest drop-off, surface as hour range
- For games where pattern matching fails: skip to Phase 1 only

**Phase 3 — Community signal aggregation (optional):**
- For games with significant critic/community discussion, optionally
  pull Metacritic/OpenCritic user reviews and Reddit posts
- LLM-extract hook-point mentions, weight as lowest-confidence source
- Combine with Phase 2 estimate into a confidence-weighted range
- If aggregate range exceeds ~6 hours wide OR confidence is low,
  hide the field rather than display garbage
- Reddit/LLM costs scale per-game, lookup throttled and cached 90+ days

**Phase 4 (out of scope unless Phase 1-3 prove valuable):**
- Community-sourced or user-supplied progression mappings for games
  where automated extraction fails

**Game-type detection for hook analysis:**
- "Multiplayer focus" — has multiplayer categories AND no Single-player.
  All hook analysis skipped. Display: "Multiplayer focus — no hook estimate."
- "No defined endpoint" — has Single-player but lacks campaign structure
  (high HLTB completionist:main ratio, sandbox/roguelike user tags).
  Phase 1 stickiness applies; Phase 2 hour range skipped.
- "Linear / story-driven" — both phases apply
- "Mixed" (e.g., Halo, Borderlands) — treat as Linear if HLTB main exists
  and progression achievements derivable; else as No defined endpoint

Display: clean range ("Hook point: hours 3-6") on cards;
per-source breakdown as expandable detail on game detail page.

## OpenCritic — possible future re-introduction (v3.5+)

OpenCritic was integrated in v1, broke in v2.5 (legacy api.opencritic.com
endpoint deprecated), and was removed in v3 after confirming the official
API now requires a paid RapidAPI subscription incompatible with the
friend-shareable distribution model.

The methodology argument for OpenCritic remains valid (equal-weight
aggregation, transparent scoring, "% recommended" metric not available
from Metacritic). If we want this signal back, the path is web scraping
opencritic.com/game/{slug} during refresh, similar to the Metacritic
user-score consideration. Tradeoffs: same ToS gray area, same fragility
when site structure changes, similar lift to implement.

The games.opencritic_score column is preserved nullable for an eventual
re-introduction. No data migration needed if we revisit.

Phase 3 hook-point work (LLM extraction from critic reviews) is a
separate concern that may use OpenCritic as a source via scraping
public review pages, distinct from numerical score fetching.

## Deferred to v4

- First-run setup wizard (replaces .env editing)
- OS keychain credential storage (via `keyring` library)
- Migration of existing .env users to keyring storage
- Per-game detail page improvements (notes, pick history surfacing)

## Deferred to v5

- Cross-platform binary distribution via GitHub Actions
  (windows-latest + ubuntu-latest runners)
- Windows portable .zip via PyInstaller --onedir
  (NOT --onefile — slower startup, AV false positives)
- Linux AppImage
- README for non-developer users
- Sign in with Steam (OpenID) for SteamID lookup as wizard nicety
  (note: OpenID provides only the SteamID, not API access — user
  still supplies their own API key)

## Out of scope (probably forever)

- Per-user achievement tracking dashboards
  (using global achievement % as a stickiness signal in v3 is different
  and IS in scope)
- LLM-powered features in the runtime app
  (one exception: optional v3 Phase 3 hook-point summarization, which
  is bounded and cacheable)
- Multi-user / multi-account support
- Cross-platform library sources (GOG, Epic, emulators)
- Mobile UI
- Analytics or telemetry
- Embedded developer Steam API key in distributed binaries
- Social/sharing features (the differentiator from Backloggd is
  action-oriented recommendation, not history logging)
- Aggregate per-user playtime distributions
  (Steam doesn't expose lists of game owners, making this technically
  infeasible; we use SteamSpy median/average forever data instead)

## Source of truth: each user supplies their own Steam API key

The app does NOT embed a developer API key in the binary. Each user
generates their own at steamcommunity.com/dev/apikey during the
first-run wizard (v4+). Stored locally via OS keyring (Windows
Credential Manager / macOS Keychain / Linux Secret Service).

Rationale:
- Steam API keys are read-only, public-data-only, cannot affect account
  security or initiate purchases
- Per-user keys mean abuse is per-user (one bad actor doesn't break
  the app for everyone)
- No central server / infrastructure burden
- No privacy intermediary responsibility

The "scary API key" UX is solved with a friendly wizard, not by
hiding the key behind a server.

## Rough phase plan (provisional, will be derailed by bugs)

**v2.5 (this week):**
- Rename to GamePile / Shortlist throughout
- played_unclassified status + inference fix
- Five user-intent modes with Comfort pick
- Library view column refinement
- Bug rundown from testing
- Codex category-stacking design ports

**v3 (next 1-2 weeks of focused sessions):**
- Backlog view as top-level nav item
- Dashboard view with aggregate stats
- Per-game detail page
- v3 hook-point Phase 1: stickiness signals
- HLTB game type column added to Library
- OpenCritic removal (done — see "OpenCritic — possible future re-introduction" below)
- Possibly: data table refactor

**v4:**
- v3 hook-point Phase 2: hour-mapped curves from progression achievements
- v3 hook-point Phase 3: community signal aggregation
- First-run setup wizard
- OS keychain credential storage
- Migration from .env to keyring

**v5:**
- Cross-platform binary distribution via GitHub Actions
- Windows portable .zip via PyInstaller --onedir
- Linux AppImage
- Sign in with Steam (OpenID) as wizard nicety
- README for non-developer users

**v6+ (only if v5 ships and gets used):**
- Manual curation overrides for hook-point progression mappings
- Whatever comes up in real use
