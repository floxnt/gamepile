# GamePile Feature Inventory — Current Shipped State

**Version:** 0.9.10 + unreleased v1.0-readiness work (2026-08-03)
**Purpose:** Complete factual inventory of what GamePile currently does
and doesn't do, for grounding external product-direction discussion.

---

## A. Top-level pages

### Shortlist (`/`)

The recommender tab and default landing page. Shows recent picks (last
8, deduped by game) and generates 5 recommendation cards on demand.

**Five modes** (user-intent labels, not algorithm descriptions):

1. **I only have tonight** — time-windowed picks filtered by a minutes
   slider (default 90min, ±25/50/75% bands). Scores in_progress games
   +2, never_played/recent +1, quality bonus for high reviews/metacritic.
2. **Continue something** — in_progress games only, scored by
   percentage through HLTB main, time-fit penalty, affinity, and pin
   boost.
3. **Comfort pick** — high-playtime games the user has clearly enjoyed
   (played_unclassified/in_progress/finished with a dynamic playtime
   floor). Soft time penalty.
4. **Start something new** — never_played + effectively-untouched
   played_unclassified games. Long-form scoring with quality and
   affinity weighting.
5. **Surprise me** — weighted random with quality bias across eligible
   games.

**Card actions** (inline + overflow menu):
- "I picked this" — records pick in history, creates feedback prompt
- "Not feeling it" — dismiss from current recommendations
- "Already completed" — marks finished, applies +0.5 affinity
- "Never recommend" — blacklists (permanent exclusion)
- "Finished" / "Bounced off it" / "Not my thing" — status transitions
  with affinity effects

**Global exclusions** from all modes: blacklisted games, not_interested
status, strong-dropped games, software/beta_playtest game types.

**Pin boost**: Games pinned from Backlog get +6.0 score in all modes
except Surprise me (14-day auto-expiry).

### Library (`/library`)

Full table view of all games in the Steam library. 10 columns (locked
as of v0.8.7):

| # | Column | Sortable | Data source |
|---|---|---|---|
| 1 | Title | Yes (default asc) | Steam |
| 2 | Type | No | Derived (game_type classification) |
| 3 | Tags | Yes (by first tag) | SteamSpy user_tags |
| 4 | HLTB Main | Yes | HowLongToBeat |
| 5 | HLTB Completionist | Yes | HowLongToBeat |
| 6 | Playtime | Yes | Steam |
| 7 | Steam Reviews | Yes (by %) | Steam |
| 8 | Metacritic | Yes | Steam store data |
| 9 | Avg. Achievement % | Yes | Steam achievements (median) |
| 10 | My Achievement % | Yes | Steam player achievements |

**Title search:** Always-visible search box in the filter bar.
Case-insensitive substring match on game name, debounced 300ms, ANDed
with every other axis. Persists in the URL (`?q=`). Deliberately not
behind the Filters disclosure — it's a primary action. No fuzzy
matching or relevance ranking: ranking would fight the user's chosen
column sort.

**Advanced filter panel:** Collapsible panel behind a "Filters" button,
which carries a badge showing the count of active axes. Apply-then-
submit (not live-filtering) so a multi-axis filter can be composed
before committing. Axes: tags (searchable multi-select with chips),
HLTB Main bucket, HLTB Completionist bucket, Status, Type, and "Show
removed" (includes is_active=0 games).

The **Status** axis is the only route to finished games — they leave
the Backlog on completion and the Status column was dropped from
Library in v0.8.7, so before it existed the completed pile had no
destination at all.

**Column resize:** Drag handles between headers. Widths persist to
localStorage under `gamepile-col-widths`. Not exported in backups — px
values don't transfer between machines at different DPI scaling.

**Sticky header:** Column headers persist on scroll for sort access.

**Scrolling:** Page-level, not an inner scroll container (the pattern
was made global — see DESIGN_CONTRACT).

### Backlog (`/backlog`)

Sectioned view of the unfinished pile. Games grouped by engagement
state:

| Section | Criteria |
|---|---|
| In progress | status = in_progress |
| Likely finished | played_unclassified, playtime >= 1.5× HLTB main |
| Finishing late | played_unclassified, 0.7× <= playtime < 1.5× HLTB main |
| In progress (unconfirmed) | played_unclassified, 0.1× <= playtime < 0.7× HLTB main |
| Barely touched | played_unclassified, playtime < 0.1× HLTB main OR no HLTB |
| Never played | status = never_played |
| Forever games | is_forever_game = True (hidden by default) |
| Dropped (soft) | status = dropped, dropped_strength = 'soft' |

**Filter dimensions** (8 axes):
- Time fit chips: short (0–5h), medium (5–15h), long (15–50h), very_long (50+h), unknown
- Tag chips (multi-select, case-insensitive)
- Status chips: in_progress, played_unclassified, never_played, dropped_soft
- Has HLTB toggle
- Include bounced toggle (shows dropped-soft section)
- Show forever games toggle
- Affinity pill filters: genre, tag, developer (single-value, from Dashboard)
- Per-section sort: default, title, hltb_main, playtime, recently_added, affinity

**Row actions** (context-dependent per section):
- Pick for Shortlist / Pin to Shortlist / Unpin
- Mark in progress / Confirm finished / Bounced / Not my thing / Never recommend

**Decision Sessions** — a focused triage mode started per section. The
section's games become a queue and are presented one card at a time
(full `<main>` replacement, "N of M" progress) with decision hints
computed against library-wide thresholds. Each card offers only the
actions valid for that game's current status; answering advances the
queue, and a recap renders at the end.

Same transitions as the Shortlist quick-actions, with the same affinity
consequences: `confirm_finished` clears any pin and applies finished
affinity; `bounced` sets dropped/soft; `not_my_thing` sets
dropped/strong; both drops feed the taste model. The point is to make
clearing a backlog section a single sustained pass rather than a
scroll-and-click hunt.

### Dashboard (`/dashboard`)

Stats overview: recent picks (last 7 days), affinity trends (positive
and negative genre/tag/developer weights). Affinity pills are clickable
links to filtered Backlog views.

### Game Detail (`/games/{appid}`)

Per-game deep view with 6 sections:

1. **Header** — cover art, title, developer, release year, HLTB
   duration badge, forever-game badge
2. **Status bar** — status dropdown (with Reset to auto-detected),
   game-type dropdown (with Reset), manual hours override
3. **Game data** — all enrichment data in card layout: Steam stats,
   HLTB times, SteamSpy tags, median achievement unlock %, user
   achievement %, Metacritic. Manual HLTB ID override with URL/ID
   input.
4. **Personal** — notes (free text, auto-save), personal rating
   (half-stars, clearable), manual hours played

   Ratings are stored as a 0–10 integer and displayed as 0.5–5.0.
   Clicking the left half of a star sets a half-step, the right half a
   full step; clicking the current value clears it. The integer scale
   is why backups carry a `rating_scale` field — an importer assuming
   0–5 would silently halve every rating.
5. **Affinity contributions** — per-label affinity weights this game
   contributes to (genre × weight, tag × weight, developer × weight)
6. **Pick history** — chronological list of every time this game
   appeared in Shortlist recommendations, with outcome and feedback

**Back navigation:** SVG chevron-left in the page header chrome (left
of GamePile wordmark), calls `history.back()` with `/library` fallback.

### Settings (`/settings`)

View and edit credentials. API key displayed masked (last 4 visible).
SteamID displayed in full. Edit-in-place forms with re-validation
before persisting. Banner when using .env fallback instead of keyring.

**Backup export** — downloads the user-authored layer as JSON
(`gamepile-backup-YYYY-MM-DD.json`). Covers game_state in full, the
sparse manual overrides on `games` (`game_type` paired with its
`game_type_manual` flag, plus `hltb_id_manual`), the affinity table,
and pick history. Excludes fetched Steam/HLTB data and computed
signals — a refresh rebuilds those — and excludes localStorage column
widths.

The envelope is version-stamped (`schema`, `rating_scale`,
`exported_at`, `app_version`). Timestamps are passed through as
stored, never restamped with export time. Export only; **import is not
implemented yet.**

### Setup Wizard (`/setup/*`)

First-run flow: Welcome → Migrate (conditional, .env → keyring) →
API Key → SteamID (vanity name resolution) → Validate (test
GetOwnedGames call) → Done (kicks off initial sync with progress
polling). Error recovery via combined edit page.

The Done page polls `/setup/sync-status` every 2s and resolves to one
of three states: still working, complete (auto-redirects to Shortlist),
or failed. The failed branch names the reason, stops the polling chain,
and offers Retry plus "Continue to app anyway". That escape link is
also present while running, because a sync wedged at `running=True` is
indistinguishable from a slow one and would otherwise be a dead end.

---

## B. User-actionable game states

### Status values (`game_state.status`)

| Status | Meaning | How entered |
|---|---|---|
| `never_played` | User has never launched this game | Auto-inferred (playtime = 0) |
| `played_unclassified` | Has Steam playtime, not yet categorized | Auto-inferred (playtime > 0, no recent activity or no HLTB) |
| `in_progress` | Currently playing | Auto-inferred (recent activity + playtime < HLTB main) OR manual |
| `finished` | Completed | Manual only ("Confirm finished" / "Already completed") |
| `dropped` | Stopped playing | Manual only ("Bounced off it") |
| `not_interested` | Will never play | Manual only ("Not my thing" / "Never recommend") |

`game_state.finished_at` records when a game entered `finished`, set on
the transition into that status and cleared when it leaves. Distinct
from `updated_at`, which any edit moves — completion history stays
honest even if the row is touched later. Backfilled once for pre-
existing finished games from `updated_at` (data migration v2), which is
the best available approximation for rows that predate the column.

Finished games are reachable via the Library Status filter. They do not
appear in the Backlog, which has no finished section by design.

### Status inference rules (auto, when `manually_set = 0`)

Applied in order during sync/state creation:
1. `playtime == 0` → never_played
2. `playtime < 30 min` → played_unclassified
3. `playtime >= 30 AND playtime < HLTB main AND last_played within 30 days` → in_progress
4. Everything else → played_unclassified

### Drop strength (`game_state.dropped_strength`)

- `'soft'` — "Bounced off it" — game appears in Backlog's dropped
  section, can be reconsidered. Applies -0.5 affinity per label.
- `'strong'` — "Not my thing" — excluded from all Shortlist
  recommendations. Applies -1.0 affinity per label.

### Blacklist (`game_state.blacklisted`)

Hard exclusion from Shortlist. Set by "Never recommend." The game
still appears in Library and Backlog but is permanently filtered from
recommendation candidates.

### Manual override flag (`game_state.manually_set`)

When True, auto-inference during sync does not overwrite the status.
Set whenever the user changes status via UI. "Reset to auto-detected"
clears this flag and re-runs inference.

### Pin (`game_state.pinned_for_shortlist`)

Temporary boost for Shortlist recommendations. Set from Backlog "Pin
to Shortlist" action. +6.0 score boost in all modes except Surprise
me. Auto-expires after 14 days.

---

## C. Per-game data fields

### Steam-sourced (fetched every sync)

| Column | Type | Description |
|---|---|---|
| `appid` | INTEGER PK | Steam application ID |
| `name` | TEXT | Game title |
| `playtime_minutes` | INTEGER | Total Steam playtime |
| `last_played_steam` | TEXT | ISO timestamp of last Steam session |
| `installed` | INTEGER | Currently installed (0/1) |
| `is_active` | INTEGER | In user's library (0 = removed/refunded) |

### Steam store details (fetched, age-band TTL cache)

| Column | Type | Description |
|---|---|---|
| `release_date` | TEXT | Release date string |
| `description` | TEXT | Short description |
| `app_type` | TEXT | Raw Steam type (game/dlc/demo/music) |
| `genres` | TEXT | Steam store categories (v1 compat) |
| `developer` | TEXT | Developer name |
| `publisher` | TEXT | Publisher name |
| `metacritic_score` | INTEGER | Metacritic score (0–100) |
| `steam_review_pct` | INTEGER | Positive review percentage |
| `steam_review_count` | INTEGER | Total review count |

### HLTB enrichment (age-band TTL cache)

| Column | Type | Description |
|---|---|---|
| `hltb_main_hours` | REAL | Main story completion time |
| `hltb_main_extra_hours` | REAL | Main + extras time |
| `hltb_completionist_hours` | REAL | 100% completion time |

### SteamSpy enrichment (age-band TTL cache)

| Column | Type | Description |
|---|---|---|
| `user_tags` | TEXT | Top 10 SteamSpy user tags (comma-separated) |

### Achievement data (active — computed during sync)

| Column | Type | Description | UI surface |
|---|---|---|---|
| `median_achievement_unlock_pct` | REAL | Median per-achievement global unlock % (0–100) | Library "Avg. Achievement %" column, Game Detail |
| `user_achievement_pct` | REAL | User's own unlock % (0–100) | Library "My Achievement %" column |

### Dormant hook-point columns (preserved, NOT populated on current DB)

| Column | Type | Status |
|---|---|---|
| `completion_rate` | REAL | Dormant — sync removed v0.7.0 |
| `completion_rate_confidence` | TEXT | Dormant |
| `cliff_metric` | REAL | Dormant |
| `cliff_position` | REAL | Dormant |
| `review_playtime_median` | INTEGER | Dormant |
| `stickiness_ratio` | REAL | Dormant |
| `playtime_median_avg_ratio` | REAL | Dormant (SteamSpy free tier returns 0) |
| `completion_achievement_name_manual` | TEXT | Dormant — Phase 4 override |
| `stickiness_badge_manual` | TEXT | Dormant — Phase 4 override |

### Derived fields (computed, not fetched)

| Column | Type | Description |
|---|---|---|
| `game_type` | TEXT | One of 11 types (see §D) |
| `game_type_manual` | BOOLEAN | User overrode classification |

### Manual override fields (user input)

| Column | Type | Description |
|---|---|---|
| `hltb_id_manual` | INTEGER | Manual HLTB game ID (bypasses name search) |
| `game_type` + `game_type_manual` | TEXT + BOOLEAN | Manual type override. The flag alone is not enough — the chosen value lives in `game_type`, so the pair must travel together |

`games.user_tags` is *not* a manual override despite the name: it holds
SteamSpy community tags and is fetched, not authored.

### Deprecated

| Column | Type | Status |
|---|---|---|
| `opencritic_score` | INTEGER | Preserved nullable; API moved behind paywall |

---

## D. Active computed/derived signals

### Game-type classification (11 types)

Computed by `app/game_type.py classify_game()` during sync. Priority
order: beta_playtest → software → expansion → early_access → mmo →
multiplayer → mixed → sandbox → no_endpoint → linear → unknown.

| Type | Description | Count (typical ~648 library) |
|---|---|---|
| linear | Story-driven with HLTB main | ~343 |
| mixed | Single-player + meaningful multiplayer (Co-op tag) | ~98 |
| no_endpoint | Roguelike/sandbox/open-ended | ~36 |
| multiplayer | Multiplayer-only, no single-player | ~43 |
| sandbox | Open-ended creative with HLTB main | ~21 |
| mmo | Massively multiplayer | ~23 |
| expansion | DLC | 0 (typical) |
| early_access | Early Access | ~36 |
| beta_playtest | Beta/playtest (title keyword) | ~18 |
| software | Utility/tool | ~3 |
| unknown | Detection failed | ~27 |

### Affinity / taste learning

Three dimensions tracked in the `affinity` table:

| Kind | Multiplier | Source |
|---|---|---|
| genre | 0.5 | Steam store genres |
| tag | 0.3 | SteamSpy user tags |
| developer | 0.7 | Steam store developer |

Weights range [-10, +10], updated by:
- Feedback flow ratings (steps 2–4)
- Quick actions (finished: +0.5, soft drop: -0.5, strong drop: -1.0)
- Did-not-play reasons (changed_mood: +0.3 to alternative, picked_another: ±0.3)

Confidence ramp: contribution scales linearly from 0 to full at 5
picks per label. Total affinity contribution per candidate capped at
±5.0.

Deduplication: if a label appears as both a genre and a tag, only the
higher-precision kind (developer > tag > genre) contributes.

### Shortlist scoring

Per-mode scoring with these signal types:
- Status bonus (in_progress, never_played)
- Recency (last played > 30 days ago)
- Quality (metacritic >= 75 OR steam_review_pct >= 80)
- Affinity contribution (±5 capped)
- Pin boost (+6.0, 14-day expiry)
- Time-fit penalty (mode-specific)
- Random jitter (mode-specific: ±1.0 / ±0.3 / weighted)

**NOT used in scoring** (explicitly): median_achievement_unlock_pct,
user_achievement_pct, any dormant hook-point columns. These are
display-only.

### Median achievement unlock % (display-only)

Median of all per-achievement global unlock percentages. Computed
during sync from `GetGlobalAchievementPercentagesForApp`. Displayed
in Library and Game Detail. Explicitly NOT wired into Shortlist
scoring — display-only by design contract.

### User achievement % (display-only)

User's own unlock percentage via `GetPlayerAchievements`. Display-only
in Library and Game Detail. Added v0.8.7.

---

## E. Filter and sort axes

### Library

| Axis | Type | Values |
|---|---|---|
| Title search | Debounced text input (always visible) | Case-insensitive substring on game name |
| Tag filter | Searchable multi-select with chips | All unique SteamSpy user tags |
| HLTB Main | Single-select bucket | <5h, 5–10, 10–20, 20–30, 30–40, 40–60, 60–100, 100h+ |
| HLTB Completionist | Single-select bucket | <15h, 15–30, 30–60, 60–100, 100–150, 150h+ |
| Status | Single-select dropdown | never_played, played_unclassified, in_progress, finished, dropped, not_interested |
| Type | Single-select dropdown | The 11 game types (see §D) |
| Show removed | Toggle | Include is_active=0 games |
| Sort column | Click header | name, game_type, tags, hltb_main, hltb_compl, playtime, steam_reviews, metacritic, median_unlock, user_achievement |
| Sort direction | Toggle | asc / desc |

All axes AND together and persist in the URL. Everything except title
search lives in the apply-then-submit panel; title search filters live.

### Backlog

| Axis | Type | Values |
|---|---|---|
| Time fit | Multi-chip | short (0–5h), medium (5–15h), long (15–50h), very_long (50+h), unknown |
| Tag | Multi-chip | Free text, case-insensitive |
| Status | Multi-chip | in_progress, played_unclassified, never_played, dropped_soft |
| Has HLTB | Toggle | Only games with HLTB data |
| Include bounced | Toggle | Show dropped-soft section |
| Show forever | Toggle | Show forever-games section |
| Genre pill | Single-value | From Dashboard affinity link |
| Tag pill | Single-value | From Dashboard affinity link |
| Developer pill | Single-value | From Dashboard affinity link |
| Per-section sort | Dropdown | default, title, hltb_main, playtime, recently_added, affinity |

### Shortlist

| Axis | Type | Values |
|---|---|---|
| Mode | Radio | 5 modes (see §A) |
| Time window | Slider | Minutes (default 90, mode-dependent) |
| Include unplayed | Toggle | Include never_played in results |
| Include in progress | Toggle | Include in_progress in results |

---

## F. End-to-end workflows

### "I want to find something to play right now"

1. Open GamePile → lands on Shortlist (`/`)
2. Select mode (e.g., "I only have tonight")
3. Adjust time slider if needed (default 90 min)
4. Click "Show recommendations" → 5 cards appear
5. Scan cards: cover art, title, HLTB estimate, "Why picked" reasons
6. Click "I picked this" on a card → pick recorded in history
7. Next visit: feedback prompt appears at top of Shortlist asking
   "Did you play it?" → 4-step feedback flow updates affinities

**Smooth:** Mode selection → cards → pick is 3 clicks. Feedback is
inline, not a separate page.

### "I want to continue a game I was playing"

1. Shortlist → "Continue something" mode
2. Cards show in_progress games sorted by % completion, time-fit,
   affinity
3. "I picked this" on the game → recorded

**Smooth:** One mode change, cards appear immediately. Or: Backlog →
"In progress" section → games listed with playtime context.

### "I want to clean up my library"

1. Library → scroll/sort by any column → click game → Game Detail
2. Set status, add notes, set hours via Game Detail controls
3. Or: Backlog → section-by-section triage with quick-action buttons
   (Finished / Bounced / Not my thing) or Decision Sessions

**Multi-step:** Library cleanup is per-game via Game Detail. Backlog
is more efficient for batch triage — actions are one-click per game
within each section, or sequential via Decision Sessions.

### "I want to mark a game as never playing again"

Two paths:
1. Shortlist card → "Never recommend" (overflow menu) → blacklisted
2. Game Detail → Status dropdown → "not_interested"

Path 1 is stronger: sets `blacklisted=True` (permanent Shortlist
exclusion) + strong drop affinity (-1.0). Path 2 sets status only.

### "I want to correct a wrong HLTB match"

1. Game Detail → Game data section → HLTB ID field
2. Paste a HLTB URL or numeric ID
3. Save → fetches correct HLTB data inline, updates times
4. "Reset" reverts to name-based search

**Smooth:** Single field, inline validation, immediate feedback.

### "I want to understand why a game was recommended"

1. Shortlist card → "Why:" line shows up to 3 reasons
   (e.g., "Matches your taste (Action: +2.1)", "In progress",
   "Fits tonight's window")
2. Same card → "Why this pick?" disclosure expands the full breakdown:
   every scoring component that contributed, ordered by magnitude, with
   negative contributors marked
3. Game Detail → Affinity contributions section shows per-label
   breakdown (genre/tag/developer × weight)

**Gap:** The breakdown lists components by label, not by raw numeric
score. Ordering conveys relative magnitude; the exact arithmetic is not
surfaced. This is deliberate — component labels are honest about *what*
counted without implying the score is a prediction.

---

## G. Manual user inputs

### Currently active

| Input | Location | Effect |
|---|---|---|
| Status | Game Detail | Sets game state, manually_set=True |
| Game type | Game Detail dropdown | Overrides auto-classification |
| Manual hours | Game Detail | Overrides Steam playtime for display |
| Notes | Game Detail | Free text, persisted per-game |
| Personal rating | Game Detail | Half-stars 0.5–5.0 (stored 0–10), clearable |
| HLTB ID | Game Detail | Overrides name-based HLTB search |
| Feedback (4 steps) | Shortlist prompt | Rating, genre match, retroactive pick → affinity |
| Pin to Shortlist | Backlog | +6.0 boost for 14 days |
| Quick actions | Shortlist cards, Backlog rows | Status transitions + affinity effects |
| Decision Session actions | Backlog session mode | Same transitions, one card at a time |
| API key / SteamID | Settings, Setup wizard | Stored in OS keyring |
| Backup export | Settings | Downloads the user-authored layer as JSON |

### Removed (retired with hook-point, v0.7.0)

| Input | Was at | Effect |
|---|---|---|
| Manual completion achievement | Game Detail | Overrode story-completion heuristic |
| Manual stickiness badge | Game Detail | Overrode categorical engagement badge |

The HLTB ID manual override is **not** part of hook-point retirement
— it remains live because it corrects a general enrichment mismatch,
not a stickiness signal.

---

## H. Sync and refresh behavior

### Trigger

Manual only — "Refresh Library" button in the top-right nav. Optional
"Force" button bypasses all TTL caches. No scheduled/automatic refresh.

### Two-phase pipeline

**Phase 1 — Steam owned games:**
- Fetches complete owned-games list via `GetOwnedGames`
- Upserts playtime_minutes, last_played_steam, name
- Marks removed games as is_active=0
- Creates game_state rows for new games (auto-inferred status)

**Phase 2 — Enrichment (per game, age-band TTL):**
- Steam store details: release_date, description, app_type, developer,
  publisher, metacritic_score, genres
- Steam reviews: steam_review_pct, steam_review_count
- HLTB: hltb_main_hours, hltb_main_extra_hours, hltb_completionist_hours
  (manual ID overrides bypass name search)
- SteamSpy: user_tags (top 10 by vote count)
- Achievements: median_achievement_unlock_pct (median of global unlock
  percentages), user_achievement_pct (user's own unlock %)
- Game-type classification: re-runs classify_game() for non-manual types

### Caching policy (age-band TTL)

| Game age | TTL | Rationale |
|---|---|---|
| < 6 months | 30 days | Active development, data changes frequently |
| 6 months – 2 years | 180 days | Settling |
| > 2 years | Indefinite | Stable, one-time fetch sufficient |

Force refresh bypasses all TTLs.

### Always-fetched (no TTL)

- Steam playtime and ownership (cheap, always current)
- Steam store details (cheap)
- Steam reviews (aggregate summary)

### Data preservation

- Manual overrides survive refresh (manually_set flag, *_manual columns)
- COALESCE-on-upsert: existing non-NULL values are preserved when the
  new value is NULL (prevents data loss on transient API failures)
- Dormant hook-point columns preserved by the same COALESCE mechanism
- JSON backup export from Settings (see §A) — the only way to get the
  user-authored layer out of the SQLite file

### Schema and data migrations

Two mechanisms, deliberately separated:

- **Schema-shape migrations** are `ALTER TABLE ADD COLUMN` statements
  run on every startup under try/except. Genuinely idempotent: a repeat
  run raises "duplicate column" and is swallowed.
- **Data migrations** are version-gated behind `PRAGMA user_version`
  and run at most once.

The split is not stylistic. A data migration placed in the idempotent
list re-executes on every launch, and one that rescales values compounds
each time — a rating-scale migration did exactly that, doubling stored
ratings on every restart before it was caught. `_migrate_v1_rating_scale`
is retained as an intentional no-op to hold the version slot.

---

## I. Recently retired or removed functionality

### Hook-point / stickiness signal (retired v0.7.0)

**What it was:** A categorical engagement badge ("Hooks players" /
"Filters early" / "Marathon" / "Mixed signals" / "Standard engagement"
/ "Usually hooks" / "Often filters" / "Limited data") computed from
achievement completion rates, review playtime distributions, and cliff
metrics. Surfaced on Library, Shortlist cards, and Game Detail.

**Why retired:** 90.3% of completion_rate values had low-confidence
heuristic matches — the pipeline presented uncertain inference as
authoritative labels.

**What was removed:**
- Library Stickiness column + filter + sort
- Game Detail Engagement signals section (entire section)
- Shortlist card stickiness pill
- Manual completion-achievement picker
- Manual stickiness badge override
- Achievement-fetching block in sync (for cliff/stickiness computation)
- Per-review playtime extraction (reviews still fetched for aggregate
  stats, but individual playtime array is discarded)
- SteamSpy playtime_median_avg_ratio computation

**What was preserved dormant:** `app/hook_metrics.py` (all 17 compute
functions), 9 DB schema columns, 4 dormant test files, 4 diagnostic
scripts. COALESCE-on-upsert keeps any existing data intact across
refresh.

### Engagement texture probe (empirically rejected, 2026-05-24)

**What it tested:** Whether composing median_achievement_unlock_pct +
stickiness_ratio + cliff_metric/cliff_position (WITHOUT completion
identification) produces a signal correlated with user engagement.

**Result:** FAIL on both distribution and behavior gates.
- Spearman correlation between score and actual user playtime: 0.11
  (threshold was 0.25)
- Bounce rate difference between top and bottom buckets: 6.4pp
  (threshold was 20pp)

**Root cause:** Population-level achievement/review statistics don't
predict individual user engagement. The signal measures game design
(achievement structure), not user behavior.

**Implication:** The engagement-signal problem is permanently closed.
No categorical engagement badge will ship in GamePile. The product
identity is "honest backlog decision tool" — it presents labeled facts
(median achievement unlock %, playtime, HLTB times) for the user to
interpret, not behavioral predictions the app makes.

### OpenCritic integration (removed v3)

Official API moved behind paid RapidAPI subscription. Column preserved
nullable. Possible future re-introduction via web scraping if the
methodology argument (equal-weight aggregation, "% recommended" metric)
justifies the effort.

---

## J. Out of scope / deferred items

### Permanently out of scope

Per `docs/DESIGN_CONTRACT.md` and `docs/PROJECT_STATE.md`:

- Hook-point / stickiness inference features (retired + empirically
  rejected)
- LLM-powered features in the runtime app
- Per-user achievement tracking dashboard (column exists, dashboard
  doesn't)
- Multi-user / multi-account support
- Cross-platform library sources (GOG, Epic, emulators)
- Mobile UI
- Analytics or telemetry
- Embedded developer Steam API key in distributed binaries
- Social/sharing features
- Scheduled background refresh jobs
- Aggregate per-user playtime distributions (Steam API limitation)

### Deferred maintenance

- Cross-distro testing matrix (Ubuntu 22.04/24.04, Fedora, openSUSE)
- Revisit bundled .NET 8 pin before EOL 2026-11-10
- Drop vendored pywebview winforms.py when upstream ships fix
- Node 20 action bumps, windows-2025 runner pin
- Re-pin linuxdeploy URLs/SHAs on upstream drift
- Code signing (revisit when friend-count grows)

### Deferred product

- **Backup import.** Export shipped first as insurance; the importer is
  a separate round. The envelope is version-stamped (`schema`,
  `rating_scale`) specifically so that importer has something to branch
  on rather than guessing at files already in the wild.
- OpenCritic re-introduction via web scraping (methodology argument
  valid, effort not justified yet)
- v1.0 milestone reserved for "friend-validation has shaken bugs out
  and the app feels stable"

---

## K. Product identity boundaries

### What GamePile WILL do

- Present factual data about games (playtime, HLTB times, achievement
  stats, review scores, tags) as labeled numbers the user interprets
- Learn user taste through explicit feedback (ratings, genre match,
  retroactive picks, quick actions) and apply it to recommendation
  scoring
- Help the user decide what to play next through mode-specific
  weighted recommendations
- Support manual curation (status, notes, ratings, HLTB corrections,
  game-type overrides) as first-class actions
- Display honest coverage boundaries ("—" for missing data, not
  imputed values)

### What GamePile WILL NOT do

- **Predict whether the user will enjoy or finish a game based on
  population data.** The engagement texture probe (Spearman 0.11)
  empirically confirmed that Steam achievement/review population
  statistics don't predict individual engagement. No categorical
  badge, score, or label will claim to answer "will I see this
  through?" The product's answer to that question is "here are the
  facts; you decide."

- **Present low-confidence inference as authoritative labels.** This
  is the design rule that drove hook-point retirement. If a signal
  can't honestly represent its confidence level in its display format,
  it doesn't ship. A categorical badge inherently collapses confidence
  into a label — that format is architecturally incompatible with the
  project's honesty discipline.

- **Silently wire display-only stats into recommendation scoring.**
  median_achievement_unlock_pct is explicitly display-only by design
  contract. Any future move to use it (or any other display stat) as
  a scoring input requires an explicit design decision with empirical
  validation, not a quiet addition.

- **Interpret user behavior from population data.** "Most reviewers
  played 40 hours" is a fact about reviewers, not a prediction about
  this user. GamePile presents facts; it does not make claims about
  what the facts mean for the individual.

### The taste-learning signal IS the engagement signal

The affinity system (genre/tag/developer weights from explicit user
feedback) is GamePile's actual engagement signal. It learns from what
the user tells it, not from what the Steam population does. This is
the product direction established by the hook-point retirement and
the engagement texture probe failure: user-reported taste preferences
(explicit feedback) over population-derived behavioral inference.

The gap: the affinity system requires 5+ rated picks per label before
reaching full confidence. A user with 648 games and 0 picks has no
personalization. The cold-start problem is real but is better addressed
by making the feedback loop more rewarding (faster taste convergence,
more visible taste effects) than by substituting population-level
proxies.
