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
- SteamSpy's free appdetails endpoint returns 0 for median_forever
  and average_forever as of this session. The playtime_median_avg_ratio
  metric is structurally dead in Phase 1a. Schema column preserved
  nullable for possible future revival via paid SteamSpy or alternate
  data source. Phase 1b drops this metric from categorical signal logic.
- Steam catalog games predating ~2012 sometimes return drastically
  reduced review counts via the appreviews API despite having thousands
  of total reviews. Approximately 5% of typical libraries fall into
  this pattern. These games will show "Insufficient data" for
  review-derived metrics regardless of threshold tuning. This is a
  Steam API limitation, not a GamePile bug.

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

## Phase 1b — shipped (session of 2026-05-05)

Categorical stickiness signal (Sticky / Average / Filters players hard /
Insufficient data) computed from cliff_metric, stickiness_ratio, and
high-confidence completion_rate. Surfaced on Game Detail (header line
with signal count), Library (sortable column with tooltips), and
Shortlist cards (inline pill). Backlog rows skipped per design decision.

Live distribution across 634 active games:
- Sticky: 27 (4.3%)
- Average: 377 (59.5%)
- Filters players hard: 11 (1.7%)
- Insufficient data: 219 (34.5%)

Observations from live data (no threshold changes made):
- Average bucket at 60% is broad — median stickiness (0.81) lands in
  the neutral band [0.51, 0.89]
- Mixed games show 0 Sticky — high-confidence completion is rare for
  that type, third signal collapses to no_data, locks them out of the
  2-of-3 majority rule
- All 11 Filters players hard cases are multiplayer titles (single-signal
  stickiness path is more lenient than 2-of-3)

Decision: ship at current thresholds. Tune from observed bad behavior
in real use rather than from distribution shape alone.

## Phase 1c — shipped (session of 2026-05-07)

Replaces the Phase 1b 2-of-3 voting model with a weighted-scoring
approach that pushed active signal density from 6.0% to 14.0% of the
library. Five concrete changes shipped in five commits:

1. New `cliff_position` Phase 1a metric — where in the sorted
   achievement list the largest cliff sits, normalized [0.0, 1.0].
   Lets Phase 1c distinguish early-game abandonment cliffs (genuinely
   filters) from late-game completionist gates (rare endgame
   achievements, not abandonment).
2. Weighted scoring — each signal returns -1/0/+1 with weights
   1.5 (stickiness) / 1.0 (cliff) / 0.7 (high-conf completion) / 0.3
   (low-conf completion). Composite score thresholded at +1.5 (Hooks)
   and -1.0 (Filters early).
3. Recalibrated completion thresholds against published Steam-population
   data (Bailey & Miyata 2019: median ~10%, mean ~14%) — sticky raised
   to 0.25 (was 0.15, "marginally above mean"), filters raised to 0.05
   (was 0.03).
4. Phase 1b "Average" split into Marathon (≥ 50h playtime + high-conf
   completion < 0.10), Mixed signals (in-band score with at least one
   strong contributor among stickiness / cliff / high-conf completion),
   and Standard engagement (in-band, no strong signals).
5. Asymmetric-threshold iteration after the initial ship — lowered
   SCORE_FILTERS_THRESHOLD from -1.5 to -1.0 to reflect that cliff is
   structurally one-sided (only ever pushes negative). Migration was
   exactly 4 games, all Mixed signals → Filters early, all with
   cliff -1 driving the score (Arma 3, How to Survive, KovaaK's,
   Escape From Duckov).

Final distribution across 636 active games:
- Hooks players: 65 (10.2%)
- Filters early: 24 (3.8%)
- Marathon: 4 (0.6%)
- Mixed signals: 63 (9.9%)
- Standard engagement: 291 (45.8%)
- Limited data: 189 (29.7%)

Phase 1b → Phase 1c headline:
- Active signal density 6.0% → 14.0% (Hooks + Filters + Marathon)
- Sticky 27 → Hooks players 65 (+2.4×)
- Filters players hard 11 → Filters early 24 (+2.2×)
- Insufficient data 219 → Limited data 189 (-30 — low-confidence
  completion now contributes at weight 0.3 instead of being dropped)

Refined target bands and verdicts:
- Hooks players 10–25%: 10.2% — in band
- Filters early 5–15%: 3.8% — under by 1.2pp (structural, see below)
- Marathon ≤ 10%: 0.6% — in band
- Limited data ≤ 30%: 29.7% — in band
- Mixed + Standard ≤ 60%: 55.7% — in band

The 1.2pp Filters early miss is structural rather than threshold-
tunable. Older catalog games with no stickiness data (pre-2012 Steam
appreviews API quirk — see api_quirks_steam_steamspy memory) and no
high-confidence completion match sit at exactly score -1.0 (cliff
alone). The inclusive ≤ -1.0 threshold already counts them; no further
data exists to push them deeper. Tightening would either require
dropping ≤ to < (kicks ~10 games back to Mixed — wrong direction) or
relaxing the cliff size/position guards (admits noise).

The Limited data ceiling at ≤ 30% acknowledges a 12.7% structural
floor: 81 games (beta_playtest 18 + early_access 32 + unknown 27 +
software 4) short-circuit to Limited regardless of any future signal
work. The remaining ~17pp comes from sparse-review older catalog where
2 of 3 signals genuinely lack data.

Marathon validation: the canonical "play forever without finishing"
cases all hit Marathon — Red Dead Redemption 2, Sid Meier's
Civilization VI, Total War: WARHAMMER II / III. The high-confidence-
completion requirement keeps sparse-data games from earning the label
off noisy heuristic estimates.

Decision: ship at current thresholds. Phase 1d, if ever needed,
addresses the structural Filters miss only via a new data source (e.g.,
review-burst pattern detection from the Steam reviews fetcher) rather
than further threshold tuning.

## Phase 4 — manual curation overrides (session of 2026-05-09)

Three Game Detail surfaces extending the manual-override pattern that
`game_type_manual`, `manually_set`, and `hours_played_manual` already
established. All single-game corrections, all following the same shape:
`*_manual` column shadows or feeds the auto-derived value, inline route
re-fetches / re-computes downstream values on save, Reset button clears
and re-runs the auto path. See `SPEC_V3_PHASE_4_OVERRIDES.md` for the
full per-surface design.

1. **Manual story-completion achievement.** `completion_achievement_name_manual`
   stores the chosen achievement's internal Steam ID; sync re-derives
   `completion_rate` from that achievement's current unlock percent
   every refresh with confidence forced to `'high'`. Picker is lazy-
   loaded via HTMX `hx-trigger="toggle once"` so the Steam API call
   is paid only when the user opens the override.
2. **Manual HLTB ID.** `hltb_id_manual` (integer); sync calls a new
   `fetch_hltb_by_id` (wraps `howlongtobeatpy.search_from_id` under
   `run_in_executor`) and bypasses name-search. Input accepts bare
   integer or `howlongtobeat.com/game/<id>` URL via `parse_hltb_id_input`.
   Save fetches inline; bad IDs surface an inline error without
   persisting. Reset re-runs name-search inline.
3. **Manual stickiness badge.** `stickiness_badge_manual` (one of the
   five `ACTIVE_BADGES` constants — `limited_data` excluded as a
   meaningless override). New `compute_stickiness_signal_display`
   helper returns `(badge, auto_badge, score, breakdown, is_overridden)`.
   All four call sites (`library_row.html`, `game_card.html`,
   `game_detail_engagement.html`, `routes/library.py` sort key)
   updated atomically. Override applies even on ineligible game types
   (software / beta_playtest / early_access / unknown) — engagement
   section becomes visible to surface the manual badge with the auto
   disclosure reading "Auto: not computed for this type."

48 unit tests across three new test files covering helper return
shapes, URL parsing edge cases, override-replaces-displayed-badge,
ineligible-type surfacing, breakdown preservation, sync fail-open on
removed achievements, and Library sort routing. Live API calls
(Steam achievements, HLTB by-ID) are exercised manually per surface
during commit-time smoke tests.

Three feature commits + one doc commit in the branch:
- `1ff70ed hook-phase4: manual completion-achievement override`
- `ee312b4 hook-phase4: manual HLTB ID override`
- `04b5e59 hook-phase4: manual stickiness badge override`
- (this doc commit)

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
