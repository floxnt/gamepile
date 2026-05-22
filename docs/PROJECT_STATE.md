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

### Phase 1c refinement — sub-divide Standard by score lean (session of 2026-05-11)

Added two lean buckets between the strong categories and Standard to
expose direction inside the middle band. `Usually hooks` fires on
composite score in `[+0.5, +1.5)`; `Often filters` on `(-1.0, -0.5]`.
Boundaries inclusive on the lean side — leans win at exactly ±0.5.
Marathon precedence unchanged (still wins over every middle-band
label). Leans take precedence over Mixed signals at the ±0.5 boundary,
which collapses Mixed to the truly-balanced cancellation case.

Live distribution across 636 active games after the split:
- Hooks players: 65 (10.2%) — unchanged
- Usually hooks: 48 (7.5%) — new
- Filters early: 24 (3.8%) — unchanged
- Often filters: 12 (1.9%) — new
- Marathon: 4 (0.6%) — unchanged
- Mixed signals: 3 (0.5%) — shrunk from 63 (60 absorbed by leans)
- Standard engagement: 291 (45.8%) — *unchanged*, flagged
- Limited data: 189 (29.7%) — unchanged

Standard engagement at 45.8% is *unchanged* by the split — flagged per
the workbook brief. Structural reason: a game with no strong contributor
(stickiness_value/cliff_value/high_conf_completion_value all zero) can
only contribute via low-confidence completion, which weights ±0.3 max.
Score magnitude tops out at 0.3, never reaching the ±0.5 lean threshold.
The split therefore successfully reorganized Mixed (strong-but-leaning
games) but cannot drain Standard without a score-formula revisit. The
workbook brief anticipated this — "it'd suggest the score formula itself
needs revisiting, not just additional thresholds." Deferred to a Phase
1d/1e session if Standard's opacity becomes a workflow problem.

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

Polish landed alongside Phase 4: Library page now has a stickiness
badge filter (`?stickiness=...`) — single-select dropdown above the
table, HTMX-swap on change with `hx-push-url` so the URL is
bookmarkable, filter respects manual overrides via
`compute_stickiness_signal_display`. Pairs naturally with Surface 3:
filter to a badge, scan, override anything that feels wrong.

Dashboard affinity pills (positive genres / user_tags / developers)
are now clickable links to a filtered Backlog view (`?genre=`,
`?tag=`, `?developer=`); negative pills explicitly stay
non-interactive. Backlog renders a top-of-page active-filter
indicator with an HTMX Clear button when a pill filter is active
(strips just the pill, preserves chip filters). Existing chip-tag
URL param renamed `?tag` → `?tag_chip` to free `?tag` for the
single-value pill (atomic rename across parser + filter-bar
template; no public bookmark risk for in-dev page).

## v4 — shipped (session of 2026-05-10)

First-run setup wizard + OS keychain credential storage. Required
for the v5 friend-distribution path: a non-developer can now
configure GamePile from scratch without editing `.env`. Existing
dev installs with project-root `.env` keep working unchanged.

See `SPEC_V4_SETUP.md` for the full design. Highlights:

- `app/credentials.py` owns all credential I/O. Read precedence:
  keyring (Windows Credential Manager / macOS Keychain / Linux
  Secret Service via `keyring` library) → `.env` (project-root →
  data-dir). Writes go to keyring only; `.env` is read-only
  fallback. `keyring_available()` probes once and caches.
- `app/config.py` no longer eagerly raises on missing creds.
  Fetchers (`steam.py`, `steam_achievements.py`) call accessors
  instead of importing module-level constants
- First-run middleware in `app/main.py` redirects every non-
  whitelisted request to `/setup/welcome` until credentials are
  configured. Whitelist: `/setup/*`, `/static/*`, `/healthz`,
  `/refresh*` (the wizard's done page polls refresh status)
- Wizard at `/setup/*`: welcome → migrate (conditional) → api-key
  → steam-id → validate → done. SteamID input accepts numeric
  SteamID64, vanity URL, or `/profiles/<digits>` URL — vanity
  names resolved via new `resolve_vanity_url` fetcher hitting
  `ISteamUser/ResolveVanityURL`. Validation runs `GetOwnedGames`
  with explicit credential overrides; failure routes to a
  combined edit page showing both fields with the error
- Migration: data-dir `.env` only (project-root never offered);
  user picks Migrate or Keep .env; `migration_done` keyring
  marker prevents re-prompt. The `.env` file stays in place as
  backup
- Done page kicks off the existing `sync_module.run_refresh()`
  as a background task and HTMX-polls `/setup/sync-status` every
  2s; auto-redirects to `/shortlist` when sync completes
- Settings page at `/settings` (small nav link top-right) shows
  current credentials with API key masked (`·`×N + last4) and
  SteamID visible, edit-in-place forms re-validate before
  persisting. `.env`-fallback banner surfaces when keyring is
  unavailable
- 18 unit tests in `tests/test_credentials.py` covering keyring
  round-trip (mocked), read precedence, fallback semantics,
  migration scope + idempotence, probe caching. Wizard routes
  smoke-tested via FastAPI TestClient during commit-time
  verification

Cross-platform note: Linux Secret Service is what's exercised on
this machine. Windows Credential Manager and macOS Keychain
backends get exercised during v5 friend-testing iteration per
the v4 brief.

Four commits:
- `c12e625 v4-setup: app/credentials.py — keyring storage + .env fallback`
- `d76b94b v4-setup: wizard backend + templates + first-run middleware`
- `b4290e7 v4-setup: settings page + nav link`
- (this doc commit)

## v5 — shipped (session of 2026-05-10)

Cross-platform binary distribution. The CI pipeline ships; real-world
cross-platform validation is the v5.x patch-release loop (friend runs
the bundle on Windows, reports issues, fixes ship in `v0.5.1` etc.).

See `SPEC_V5_DISTRIBUTION.md` for the full design. Highlights:

- **Path resolution audited and migrated to `platformdirs`.** Production
  sites (`app/config.py`, `app/credentials.py`) swapped from XDG-only
  resolution to `platformdirs.user_data_dir("gamepile", appauthor=False)`.
  Resolves: Linux `$XDG_DATA_HOME/gamepile`, Windows `%LOCALAPPDATA%\gamepile`,
  macOS `~/Library/Application Support/gamepile`. Legacy
  `tonights-pick` / `game-roulette` migration guarded by
  `sys.platform == "linux"`. New `tests/test_paths.py` (9 tests) plus a
  regression-canary grep against hardcoded XDG patterns in production code.
- **`pyproject.toml` PEP 508 marker** on `PyGObject>=3.42; sys_platform == 'linux'`
  keeps `uv sync` working on the Windows CI runner (no wheel exists).
- **PyInstaller spec rewritten** for cross-platform `--onedir` builds.
  `--onedir` not `--onefile` — Windows Defender flags --onefile far more
  often. Per-platform hiddenimports/excludes select active backends:
  edgechromium on Windows, gtk on Linux. Keyring backends scoped
  per-platform (Linux Secret Service depends on `secretstorage` which
  isn't on Windows wheels).
- **`app/_resources.py`** — frozen-aware resource resolver. Returns
  `sys._MEIPASS/app` inside a PyInstaller bundle (where `__file__`-based
  resolution breaks because PyInstaller flattens the script entry point),
  package dir in dev. Both StaticFiles and Jinja2Templates route through it.
- **`app/main.py` WebView2 runtime detection** (Windows-only) probes
  3 registry locations; on missing runtime, auto-opens the WebView2
  installer page via `webbrowser.open` and exits cleanly. Better than
  pywebview's opaque crash on LTSC / fresh Windows installs.
- **`--healthz-only` CLI flag** for CI smoke-testing: starts uvicorn,
  polls `/healthz`, prints "ok"/"fail", exits 0/1. No GUI. 30s timeout
  absorbs slow cold-start on windows-latest runners.
- **`uvicorn.Config(app, ...)`** receives the FastAPI app object directly
  (not the `"app.main:app"` import string) — the import string fails in
  the bundle because PyInstaller flattens the script entry.
- **`.github/workflows/release.yml`** — tag-driven (`v*`) cross-platform
  build pipeline. Matrix runs `ubuntu-latest` + `windows-latest` in
  parallel. Linux: install GTK/WebKit2 system deps → uv sync → run full
  test suite → pyinstaller → tar.gz. Windows: uv sync → pyinstaller →
  `--healthz-only` smoke test → ZIP. Tag pushes upload to a draft GitHub
  Release via `softprops/action-gh-release@v2`; `workflow_dispatch` runs
  upload to actions/upload-artifact only (no Release object touched).
- **`README.md`** rewritten as developer-facing; **`README.bundled.md`**
  ships inside each release archive as user-facing (install / first-run /
  troubleshooting: SmartScreen, WebView2, data location).

Versioning: semver starting `v0.5.0`. `v1.0.0` reserved for "friend
validation has shaken bugs out and the app feels stable."

Cross-platform validation status: Linux bundle smoke-tested locally end
to end (`/healthz`, `/static/style.css`, `/setup/welcome` all respond
correctly). Windows bundle untested until the workflow runs against a
real tag push and a friend runs the artifact on Windows. Expected
follow-ups in v5.x: WebView2 runtime path verification, Windows
Credential Manager round-trip, SmartScreen UX confirmation.

Four commits:
- `eccd4ab v5-dist: platformdirs migration + path-resolution audit`
- `4647d0f v5-dist: cross-platform PyInstaller spec + WebView2 detection + --healthz-only`
- `e6b625c v5-dist: GitHub Actions release workflow (Linux + Windows)`
- (this doc commit)

## v5.x — Windows-bundle launch saga (2026-05-17 → 2026-05-19)

The first time the v5 Windows build met a clean consumer Windows 11
machine (v0.5.3 on 2026-05-17), it crashed. Diagnosing and fixing it
took seven patch releases (v0.5.4 → v0.6.2) and surfaced a layered set
of CI-vs-consumer-environment failures, each one masking the next.
v0.6.2 is the first release that survives the manual gate;
v0.6.3 onward is post-saga.

`SPEC_V5_DISTRIBUTION.md` is the *technical* record (what the
architecture is now, why each layer exists, the disciplines extracted).
This section is the *diagnostic* record — per round, the hypothesis,
what actually broke, and the rule each failure produced. The intent is
that a fresh Claude Code session can inherit the full arc here without
reconstructing it from commit archaeology.

**v0.5.3 — initial failure.** Bundle crashes on clean Windows 11 with
`RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize`
despite v5 CI passing. The saga starts here.

**v0.5.4** — hypothesis: PyInstaller spec missing pythonnet /
clr_loader data files (collect_all gap). Fix: `collect_all("pythonnet")`
+ `collect_all("clr_loader")` in `gamepile.spec`; add
`--check-windows-runtime` CLI detector for CI. What broke: bundle still
crashed byte-for-byte the same on the consumer machine despite the spec
fix. What it taught: bundling-completeness was not the only issue.

**v0.5.5** — hypothesis: still bundling-completeness (some other
collect_all gap). Fix: none material, mostly a re-test with refined
detector. What broke: bundle crashed identically on the consumer
machine; the `--check-windows-runtime` detector passed on the runner
but the consumer machine failed at the same Loader.Initialize site.
The bundling-completeness theory was dead. What it taught: **CI
environment ≠ consumer environment.** windows-latest is a developer
image — multiple .NET SDKs, Framework Developer Pack, populated GAC,
registry hints. The pre-v0.5.6 netfx architecture (host-resolved facade
assemblies) cannot be reliably validated by CI because that
environmental gap is structural, not a configuration mistake.

**v0.5.6** — hypothesis: bundle the .NET 8 coreclr runtime so the
loader chain stops depending on host .NET state. Also vendor the
WebView2 netcoreapp3.0 binding to fix the `ContextMenu` → `ContextMenuStrip`
type rename. Fix: download + SHA-verify .NETCore.App + WindowsDesktop.App
8.0.27 in the workflow, ship them inside the bundle, point
`pythonnet.set_runtime()` at a custom `Python.Runtime.runtimeconfig.json`.
What broke: the spec filter intended to drop pywebview's bundled net462
binding was silently bypassed. pywebview's `hook-webview.py` re-runs
during `Analysis()` and re-adds the net462 DLLs *after* the filter ran.
What it taught: PyInstaller hook execution order matters; spec-time
filters can be re-overridden by hooks. Fix must run *after* PyInstaller,
not during it.

**v0.5.7** — hypothesis: post-build filesystem replacement. Fix:
workflow step copies the verified netcoreapp3.0 DLLs over the bundle's
`_internal/webview/lib/` with double-SHA verify (source SHA before
copy, destination SHA after copy). What broke: the same symptom further
in the load chain — `WebViewException: You must have pythonnet installed
in order to use pywebview`. What it taught: pywebview's catch-all
`except ImportError` masks the real exception. The misleading error
message is a *symptom* of an upstream class-body crash being swallowed.

**v0.5.8** — hypothesis: instrument first, fix second. Fix: add
`_dump_exc()` to `app/main.py` to walk the import chain and print the
swallowed exception traceback to stderr. What broke: nothing — the
diagnostic worked exactly as designed and revealed `AttributeError:
'NoneType' object has no attribute 'GetMethod'` in
`webview/platforms/winforms.py`'s `OpenFolderDialog` class body. What
it taught: **make the load chain audible before trying to fix the
symptom.** Diagnostic instrumentation stays in main permanently as
load-bearing surface, not scaffolding — every future regression in
this layer benefits from the same audibility.

**v0.5.9 / v0.6.0** — hypothesis: vendor pywebview 6.2.1's
`winforms.py` with the smparkes:fix/dotnet8-coreclr patch applied (all
three sub-fixes, not just OpenFolderDialog). Fix: add
`vendor/pywebview/winforms.py` with provenance header; workflow step
SHA-verifies source, overwrites venv copy, SHA-verifies destination.
What broke (v0.6.0): SHA512 mismatch between WSL-committed vendored
file and Windows-runner-checked-out file. Root cause: Git's default
`core.autocrlf=true` on Windows runners converted LF → CRLF on
checkout, changing the file's bytes. What it taught: **SHA-pinned
files require `.gitattributes` line-ending pins.** Without `text
eol=lf`, the SHA validates against a different byte sequence than what
was committed.

**v0.6.1** — hypothesis: `.gitattributes` fixes the line-ending
issue; the patched winforms.py now applies cleanly. Fix: add
`.gitattributes` pinning `vendor/pywebview/winforms.py` to LF. CI
reported all checks green: `Build linux → success`,
`Build windows → success`, `--check-windows-runtime PASSED`. Release
published as canonical non-prerelease. **The bundle still crashed on
the consumer machine.** What broke: the smparkes patch gates its .NET 8
compatibility branches on `os.environ.get('PYTHONNET_RUNTIME') ==
'coreclr'`. Our code selected the runtime via the explicit
`pythonnet.set_runtime()` API and never set the env var. The patch's
coreclr branches never fired; the original `OpenFolderDialog` class
body ran; the AttributeError raised exactly as in v0.5.9. The
three-stage detector assertions passed because none of them covered
the `import webview.platforms.winforms` step that was throwing. The
chain walk did emit `[exc]` lines in CI stderr, but no stage failed on
exception presence — CI green over a broken bundle.

What it taught: two structural rules, now SPEC discipline notes:

1. **Audit the caller-side interface of any vendored third-party
   patch, not just the patch's own diff.** The patch's `if`/`else`
   gates are caller-side preconditions in disguise.
2. **Every new layer added to the load chain must include an
   assertion AT that layer, not delegated to downstream stages.**
   Downstream assertions test the chain produced the right artifact;
   they cannot prove the chain ran without swallowing errors along
   the way.

**v0.6.2** — hypothesis: set `PYTHONNET_RUNTIME=coreclr` env var in
addition to the explicit `set_runtime()` API call, and tighten the
detector to fail-fast on any chain-walk exception. Fix: two lines in
`app/main.py` — set the env var in the top-of-module frozen-Windows
block, and a module-level `_CHAIN_EXC_COUNT` counter that
`_dump_exc()` increments; detector fails the run if non-zero before
the three-stage assertions execute. CI ran green; manual gate (clean
Windows 11 double-click) **passed** (2026-05-19). v0.6.2 is the
canonical release; v0.5.3 through v0.6.1 are flagged prerelease with
the uniform note "superseded — Windows bundle not functional, use the
latest release."

**Cumulative rules from the arc** (also reflected in SPEC discipline
callouts):

1. CI environment ≠ consumer environment. Bundle the runtime; do not
   resolve against the host.
2. Make the load chain audible before fixing the symptom. The
   diagnostic instrumentation is load-bearing surface, not scaffolding.
3. Three-stage assertions duplicate coverage between mechanism and
   symptom precisely because single-check passing while reality fails
   is the recurring failure mode of this arc.
4. CI green is necessary, never sufficient. Release acceptance is the
   manual clean-Windows-11 double-click test.
5. Audit the caller-side interface of any vendored third-party patch,
   not just the patch's own diff.
6. Every new layer added to the load chain needs an assertion AT that
   layer, not delegated downstream.
7. SHA-pinned files require `.gitattributes` line-ending pins to
   survive Windows checkout's autocrlf conversion.

**Post-saga deferred housekeeping** (tracked in `SPEC_V5_DISTRIBUTION.md`):

- Revisit bundled .NET 8 pin before EOL 2026-11-10
- Update pinned .NET 8 URL/SHA on security patches
- Drop the WebView2 binding override when pywebview ships upstream fix
  (issue #1803)
- Drop the vendored pywebview winforms.py when pywebview ships upstream
  fix (issue #1803). On re-vendor or drop, re-run the audit discipline
  above — the new code's gating model may differ
- Refactor `SPEC_V5_DISTRIBUTION.md` into v5-baseline + v5.x-maintenance-
  log after one or two more clean release rounds without further saga
- Node 20 action bumps, windows-2025-vs2026 pin (longstanding deferred)

## v0.7.0 — hook-point retirement + median-unlock display stat (2026-05-19)

Two-commit change: removed the hook-point / stickiness feature from the
live product and added a single honest display-only stat in its place.
The two are conceptually different — one was a behavioral inference
that overpromised, the other is a labeled fact the user interprets
themselves. They are not "one feature replacing another."

### Hook-point retirement (commit 1)

The empirical achievement-signal probe (2026-05-19) found that 90.3% of
stored `completion_rate` values self-labeled as `'low'` confidence —
the pipeline was presenting low-confidence inference as authoritative
via the Phase 1c categorical badges. Removed from live UI:

- Library Stickiness column + filter + sort
- Game Detail Engagement signals section (entire section)
- Game Detail manual-override surfaces for completion-achievement and
  stickiness-badge (HLTB-ID override is unrelated and stays)
- Shortlist card stickiness pill
- Five POST/GET routes under `/games/{appid}/`
- The hook-metrics call sites in `app/sync.py _phase_enrich`

Preserved dormant (do not purge):

- `app/hook_metrics.py` — all compute functions, threshold constants,
  badge taxonomy — with a top-of-module `RETAINED DORMANT` header
- Nine DB schema columns (`completion_rate`, `completion_rate_confidence`,
  `cliff_metric`, `cliff_position`, `review_playtime_median`,
  `stickiness_ratio`, `playtime_median_avg_ratio`,
  `completion_achievement_name_manual`, `stickiness_badge_manual`)
- All accumulated values in those columns (COALESCE-on-upsert kept)
- Four tests covering the dormant pipeline pure-functions, with
  `DORMANT (v0.7.0)` headers pointing at the retirement SPEC

Recommender note: `app/recommender.py` was structurally already not
using hook-point/stickiness as a scoring input. Its quality signal
remained Metacritic + Steam review %; exclusions key off `game_type`.
No degradation from the removal because the input wasn't there to
remove.

See `SPEC_HOOK_RETIREMENT.md` for the full removal record, preservation
inventory, rationale, reevaluation intent (~v1.0), and continuity rules
for future maintainers.

### Median-unlock display stat (commit 2)

New display-only stat: median per-achievement global unlock percent.
Stored in a new nullable column `games.median_achievement_unlock_pct`,
computed via `GetGlobalAchievementPercentagesForApp` (no-key endpoint)
during normal library sync — same data source the hook-point pipeline
used, different summary statistic. Sortable Library column; single
stat on the Game Detail external-data card. "—" displays for the ~25%
of the library with no achievements (no imputation, no zero-fill — an
honest coverage boundary).

Median, not mean, because per-game distributions are heavily right-
skewed (probe-confirmed): every game has a few high-% launch
achievements plus a long low-% tail; mean is dragged by achievement-
list design, median is the robust honest summary.

Display-only. Explicitly **NOT** wired into the Shortlist recommender's
pick logic. This is the corrective discipline: a number we display, not
a behavioral claim. Any future move to use it as pick-logic input
requires an explicit decision and round of design, not a quiet wire-up.

Migration: additive column only, starts NULL for everyone; populated by
the normal sync path on next refresh. No bootstrap from the probe-time
CSV — keeping dev-machine artifacts out of production data paths is
the conservative call even at the cost of one slower first sync.

See `SPEC_HOOK_RETIREMENT.md` and the in-file commit headers for the
design decisions and the "must not evolve into a behavioral signal
silently" continuity note.

## v0.8.0 — Inno Setup installer replaces raw .zip (Windows)

Distribution-layer phase, no app behavior changes. The Windows release
artifact changed from `gamepile-vX.Y.Z-windows-x64.zip` (extract +
double-click `gamepile.exe`) to `gamepile-setup-vX.Y.Z.exe` (proper
per-user Inno Setup installer with Add/Remove Programs entry +
uninstaller + upgrade-in-place path). Linux artifact unchanged
(`.tar.gz`).

Key properties of the installer:

- **Per-user install** (`PrivilegesRequired=lowest`), no admin
  elevation, no UAC. Installs to `%LocalAppData%\Programs\GamePile`.
- **Stable AppId GUID** (`{D72A3C2F-1F81-4B71-80C5-AFF7276673BD}`),
  committed as a fixed constant — never regenerated across versions.
  Inno uses this to detect existing installs for upgrade-in-place.
- **Never touches `%LocalAppData%\gamepile`** (the user data directory)
  on install, upgrade, or uninstall. Months of accumulated Steam sync,
  classifications, pick history, affinity weights all survive every
  installer operation by virtue of the platformdirs-driven separation
  between install-location and data-location. Flagged in
  `SPEC_V5_DISTRIBUTION.md` as a load-bearing distribution property.
- **No "also remove user data" uninstall checkbox.** Cost-asymmetry
  argument: misclick cost is irrecoverable months of data; no-checkbox
  cost is ~10 KB of leftover folder. README.bundled.md documents the
  one-line manual cleanup for the rare case someone wants it.
- **Publisher = `floxnt`** (the GitHub repo owner pseudonym used
  throughout the project).
- **Ships unsigned.** SmartScreen "More info → Run anyway" remains the
  expected first-launch experience. Signing deferred — same calculus
  as v0.5.0; revisit if friend-count grows enough that SmartScreen
  reputation could meaningfully accrue.

Build pipeline change: the workflow installs Inno Setup via chocolatey
on the windows-latest runner (no SHA-pin — see the formalized
"SHA-pin user-shipped binaries; don't ceremonially SHA-pin host-only
build tools" principle in SPEC_V5_DISTRIBUTION.md) and runs `iscc.exe`
against `installer/gamepile.iss` with the version passed in via
`/DAppVersion=`. The compile step asserts the produced installer
exists and is above a 100 MB plausibility floor (fails fast if the
`[Files]` section silently dropped the payload).

Manual hardware gate APPLIES for this release. This is the first
distribution-layer phase since the v0.5.x → v0.6.2 saga that needs
the manual install/launch/upgrade/uninstall confirmation on a real
Windows 11 machine — CI green is necessary but NOT sufficient. The
gate has four sub-tests (clean install + launch, upgrade-over-prior
+ data preservation, Add/Remove Programs entry, uninstall doesn't
touch data); all four must pass before the Release is promoted from
prerelease to canonical. Until the gate clears, the Release is
flagged prerelease with an awaiting-manual-validation note.

Deferred housekeeping queued by this phase:

- Linux installation parity via AppImage (close the install-experience
  gap between Windows installer and Linux raw tar.gz)
- Revisit code signing if/when friend-count grows enough for
  SmartScreen reputation to accrue meaningfully

See `SPEC_V5_DISTRIBUTION.md` "Inno Setup installer (Windows, v0.8.0+)"
for the full architecture record, AppId GUID rationale, scope
guarantees, SHA-pinning discipline principle, and the manual-hardware-
gate release-acceptance criterion.

## v0.8.2 — Linux AppImage replaces raw .tar.gz

Distribution-layer phase, no app behavior changes. The Linux release
artifact changed from `gamepile-vX.Y.Z-linux-x64.tar.gz` (extract +
install distro-specific GTK/WebKit packages + run `./gamepile/gamepile`)
to `gamepile-vX.Y.Z-linux-x64.AppImage` (single self-contained file
bundling GTK 3 + WebKit2GTK + GObject-introspection typelibs + Cairo).
Windows artifact unchanged (still the Inno Setup installer from v0.8.0).

The motivating concrete failure: v0.8.1's .tar.gz crashed on a fresh
CachyOS install with `Namespace Gtk not available` / `No module named
'qtpy'` because the host lacked GTK 3 system packages and introspection
typelibs. PyInstaller bundles Python code + Python deps but not
system-level native GTK libraries; the .tar.gz only worked on distros
where the user had previously installed GTK 3 themselves. AppImage
closes that gap by bundling the GTK stack INTO the artifact.

Key properties of the build:

- **`linuxdeploy` + `linuxdeploy-plugin-gtk`** as the tool combination.
  The plugin walks the bundle's GTK dependencies and packages
  libraries + introspection typelibs + GSettings schemas + GdkPixbuf
  loaders into the AppImage. Both binaries SHA512-pinned per the
  formalized "SHA-pin user-shipped binaries" discipline (their output
  is the AppImage payload that ships to users, so they're load-bearing
  pinned alongside the Windows .NET 8 / WebView2 / vendored pywebview
  patches).
- **Per-user run.** AppImages are always per-user — no system install,
  no root, no package manager. End-user host needs only FUSE2 (or the
  AppImage's `--appimage-extract-and-run` fallback).
- **AppImage REPLACES the tar.gz.** Same "replace, don't double-ship"
  shape as v0.8.0's installer replacing the Windows zip.
- **Placeholder icon (stock Adwaita).** linuxdeploy requires an icon to
  build; v0.8.2 ships the runner's stock `applications-games` theme
  icon as a placeholder. Real GamePile icon design is deferred to a
  separate polish round that also covers the Inno Setup `.iss` icon
  gap.

Build pipeline change: the workflow installs `librsvg2-bin` +
`adwaita-icon-theme` (host-only, not SHA-pinned — they don't ship
into the AppImage), SHA-verifies the pinned linuxdeploy + plugin-gtk
binaries, stages an AppDir layout from the PyInstaller `--onedir`
output, and invokes linuxdeploy with the GTK plugin. The build step
asserts the produced AppImage exists and is above a 40 MB plausibility
floor (fails fast if WebKit2GTK or another major sub-bundle silently
dropped).

Manual hardware gate APPLIES for this release on a fresh consumer
Linux machine (CachyOS as the reference, since v0.8.1's .tar.gz failed
there). The gate is: download the AppImage, `chmod +x`, run, confirm
a window opens — all WITHOUT preinstalling GTK 3 / pygobject / Cairo
system packages on the host (that's the load-bearing test of "AppImage
is self-contained"). Until the gate clears, the Release is flagged
prerelease with an awaiting-manual-validation note covering both the
v0.8.0 Windows installer gate and the v0.8.2 AppImage gate.

Version-bump discipline note formalized at v0.8.2 (added to SPEC):
**minor bumps require new running-app functionality the user can
actually use**; distribution/packaging changes (installer format,
artifact format, build-pipeline changes) are patch bumps regardless
of how user-visible the wrapper change is. v0.7.0 (hook removal + new
stat) was a justified minor; v0.8.0 (Windows installer replaces zip)
was minor in retrospect closer to a patch, but shipped and not worth
rewriting; v0.8.2 (AppImage replaces tar.gz) is a patch by the
tightened rule going forward. 1.0 reserved for hook-point reevaluation
or equivalent product-readiness milestone.

Deferred housekeeping queued by this phase:

- Re-pin linuxdeploy + linuxdeploy-plugin-gtk URLs/SHAs when bytes
  drift (same pattern as the .NET 8 / Node 20 deferred pins).
- Commission/create a real GamePile icon and wire it through both
  AppImage AppDir + Inno Setup `.iss` in one polish round.

See `SPEC_V5_DISTRIBUTION.md` "AppImage (Linux, v0.8.2+)" for the full
architecture record, AppDir layout, tool-combination rationale,
SHA-pinning entries, and the manual-hardware-gate release-acceptance
criterion. The "Version bump discipline" section codifies the
forward-applying rule.

## v0.8.3 — DEPLOY_GTK_VERSION=3 fix for AppImage build (follow-on to v0.8.2)

v0.8.2's CI failed at the `linuxdeploy-plugin-gtk` step with "failed to
auto-detect GTK version. Please set DEPLOY_GTK_VERSION to {2, 3, 4}".
Root cause: the plugin's auto-detection walks the application binary's
`ldd` output for libgtk-*, but the PyInstaller bootloader at
`AppDir/usr/bin/gamepile` has no direct GTK linkage — GTK loads at
runtime via PyGObject dlopen from inside `_internal/libpython3.12.so`,
invisible to the plugin's inference. Fix: explicit `DEPLOY_GTK_VERSION=3`
env var on the "Build AppImage" workflow step, with an in-place YAML
comment block documenting the structural blindness so the env var isn't
removed as cruft by a future maintainer. GamePile uses GTK 3 via
pywebview's gtk backend (see v0.8.2 entry above for the AppImage
architecture).

Same audit-then-fix discipline as the v0.5.x → v0.6.2 Windows saga (read
the actual stderr before guessing a fix). Same forward-with-history-
preserved recovery pattern: v0.8.2 stays in the Releases page as a
prerelease marker with the broken-Linux history; v0.8.3 ships as the
forward-fix. `SPEC_V5_DISTRIBUTION.md` "Distribution flow" step 4 was
clarified alongside this fix to read "increment the patch component"
unambiguously (the previous "re-tag with a patch bump" wording was
ambiguous between same-number re-roll and component-increment;
"increment the patch component" matches what the project actually does
and matches the Windows saga's v0.5.3 → v0.6.2 prerelease-chain
precedent).

Manual hardware gate still applies on the v0.8.2 reference target
(fresh CachyOS without preinstalled GTK packages). Until cleared, v0.8.3
is prerelease.

## v0.8.4 — LD_LIBRARY_PATH apprun-hook injection (follow-on to v0.8.3)

v0.8.3's AppImage shipped a structurally-complete GTK 3 bundle
(`libgtk-3.so.0` in `usr/lib/`, 36 typelibs in
`usr/lib/girepository-1.0/`, full transitive closure of 99 libraries
deployed by linuxdeploy-plugin-gtk) but failed on the manual hardware
gate (clean CachyOS without preinstalled GTK packages) with
`ValueError: Namespace Gtk not available` — identical to v0.8.1's
.tar.gz failure on the same machine. Root cause from the v0.8.3
empirical audit (inventoried the artifact's contents, read the
plugin's actual CI log, walked the plugin's source, reproduced by
inversion on WSL where system GTK masks the bundle's defect):
linuxdeploy-plugin-gtk's auto-generated AppRun exports
`GI_TYPELIB_PATH` and `GTK_*` metadata vars but NOT
`LD_LIBRARY_PATH`. The plugin's design assumption is that the
application binary has direct `NEEDED` entries for `libgtk-3.so.0`
— under that assumption, the dynamic linker eager-loads the bundled
GTK stack at process start via the binary's
`RUNPATH=$ORIGIN/../lib` (= `$APPDIR/usr/lib/`), and PyGObject's
later `gi.require_version()` dlopens find the libraries already-
resident. Our PyInstaller bootloader has no GTK linkage in `NEEDED`
(same gap that caused v0.8.2's auto-detect failure for
`DEPLOY_GTK_VERSION`); no eager GTK load happens. At runtime, when
libgirepository (inside `_internal/`) dlopens `libgtk-3.so.0`, that
dlopen does NOT consult the executable's `RUNPATH` (`DT_RUNPATH`
is per-object, not inherited transitively from a library-context
dlopen — well-known glibc behavior). The bundled `libgtk-3.so.0`
at `$APPDIR/usr/lib/` is invisible to that dlopen on a clean host,
and gi reports the namespace as unavailable.

Fix: extend the plugin's apprun-hook with an `LD_LIBRARY_PATH`
export that makes `$APPDIR/usr/lib/` discoverable to dlopen calls
from bundled libraries. Implementation is a three-phase linuxdeploy
invocation: (1) deploy with `--plugin gtk` (no `--output`), (2)
append `installer/linux/apprun-libpath-hook.sh` to the plugin's
generated apprun-hook, (3) package with `--output appimage` (no
`--plugin`, so the hook isn't regenerated). We append to the
plugin's hook rather than modifying AppRun directly because
AppRun's `source` line for that hook is the only extension point
linuxdeploy reliably preserves — `--custom-apprun` is documented
broken (linuxdeploy/linuxdeploy issue #100, where the custom file
is silently overwritten). The two-phase + append-to-plugin-hook
approach was empirically verified on WSL before being committed:
phase 3 (`--output appimage` with no `--plugin`) does not
regenerate the hook, and the appended export survives into the
final packaged `.AppImage`.

Same forward-with-history-preserved recovery pattern as v0.8.2 →
v0.8.3: v0.8.3 stays prerelease in the Releases page with a body
note recording the manual-gate failure mechanism; v0.8.4 ships as
the forward-fix. Same empirical-audit-before-fix discipline as the
v0.5.x → v0.6.2 Windows saga: read the actual stderr (and inventory
the actual artifact, and reproduce by inversion on a host that
masks the failure) before guessing a fix. Part B (WebKit2GTK
bundling — `libwebkit2gtk-4.1.so.0` is absent from the bundle, only
its typelib is present) is deferred to a v0.8.5 round IFF the
manual gate empirically surfaces it; deliberately not pre-implemented
because predictions about CI/build-tool behavior on this project
have a track record of being one structural detail off.

Manual hardware gate still applies on the same CachyOS reference
target. Until cleared, v0.8.4 is prerelease.

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
