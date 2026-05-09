# v3 Phase 4 — Manual Curation Overrides

## Purpose

Phase 4 fills in the remaining gaps in the manual-override pattern that
`game_type_manual`, `manually_set` (status), and `hours_played_manual`
already established. Three Game Detail surfaces let the user correct
specific kinds of bad data without waiting for model-level work, all
following the same shape: a nullable `*_manual` column shadowing or
feeding the auto-derived value, an inline route that re-fetches /
re-computes downstream values on save, and a Reset button that clears
the manual flag and re-runs the auto path.

This document is a successor to `SPEC_V3_GAME_DETAIL.md` — that one
covers the original detail-page scope; this one covers the three
override surfaces added during Phase 1c soak.

## Source-of-truth precedence

Per `docs/DESIGN_CONTRACT.md`, the precedence order is:

1. Manual user override (anything `*_manual = NULL → not set; otherwise wins`)
2. Steam-owned facts (playtime, ownership, last_played)
3. External enrichment (HLTB, Metacritic, SteamSpy)
4. Inferred or guessed data

All three Phase 4 surfaces sit at level 1.

## Surface 1 — Manual story-completion achievement

### Use case

Hollow Knight / Witcher 3 / Disco Elysium-type games where the Phase 1a
heuristic (`hook_metrics.find_story_completion_achievement`) picks the
wrong achievement because the actual story-completion achievement has
a themed or opaque name that doesn't match the
`STORY_COMPLETION_PATTERNS` substring set. The user picks the right
one; sync re-derives `completion_rate` from that achievement's unlock
percent on every refresh, with confidence forced to `'high'`.

### Schema

```sql
ALTER TABLE games ADD COLUMN completion_achievement_name_manual TEXT;
```

Stores the achievement's internal `name` (the stable Steam ID — not
`displayName`, which can be themed or change with localisation). NULL
means "use heuristic."

### Domain helper

```python
# app/hook_metrics.py
def pick_completion_achievement(achievements, manual_name) -> Optional[dict]:
    """Locate an achievement by internal name. Returns the dict or None
    when manual_name is unset / not found in the list."""
```

### Sync interaction

In `app/sync.py` `_phase_enrich` after fetching the achievement list:

- If `game.completion_achievement_name_manual` is set:
  - Look up that achievement via `pick_completion_achievement`
  - If found: write `completion_rate = match["percent"] / 100.0`,
    `completion_rate_confidence = 'high'`
  - If missing (developer removed it / typo / stale): keep last-stored
    values via the upsert COALESCE — don't null out
- Else: run `compute_completion_rate` + `compute_completion_rate_confidence`

`cliff_metric` and `cliff_position` are still derived heuristically
regardless of override — there's no manual cliff override (cliff is a
structural metric, not a labeling judgment).

### Routes (`app/routes/game_detail.py`)

- `GET /games/{appid}/achievements` — lazy-load picker partial. Fires
  via HTMX `hx-trigger="toggle once"` on the `<details>` disclosure;
  pays the Steam API round-trip only when the user actually engages
  the override.
- `POST /games/{appid}/completion_achievement` — body has
  `achievement_name`. Re-fetches achievements inline, validates the
  name is present, persists via `set_completion_achievement_manual`,
  returns the engagement-section partial.
- `POST /games/{appid}/reset_completion_achievement` — refetches,
  re-runs heuristic, persists via `clear_completion_achievement_manual`.

### Templates

- `partials/game_detail_completion_picker.html` — new partial. `<select>`
  of every achievement sorted by unlock % ascending (story endings
  cluster at top), with the current pick selected.
- `partials/game_detail_engagement.html` — completion-rate row gets
  a `<details>` disclosure linking to the picker, plus a "manually
  set" indicator and Reset button when override is active.

### CSS

`.engagement-override-block`, `.engagement-override-summary`,
`.engagement-override-body`, `.engagement-override-loading`,
`.completion-override-form`, `.completion-override-label`,
`.completion-override-select`, `.completion-override-empty`,
`.engagement-aside--manual` (accent-colour + italic on the
"manually set" indicator).

## Surface 2 — Manual HLTB ID

### Use case

Games whose Steam title doesn't disambiguate against HLTB's catalog
(Soul Reaver Remastered → matches the original; Wasteland 1 - The
Original Classic → doesn't match anything via search/clean). User
finds the right HLTB record, copies the URL or the ID, and pastes it.

### Schema

```sql
ALTER TABLE games ADD COLUMN hltb_id_manual INTEGER;
```

When set, sync calls `fetch_hltb_by_id(hltb_id_manual)` and skips
name-based search. The HLTB values themselves (`hltb_main_hours`, etc.)
remain auto-derived from whatever the ID returns — only the LOOKUP
path is overridden, not the values.

### Fetcher (`app/fetchers/hltb.py`)

```python
async def fetch_hltb_by_id(hltb_id: int) -> HltbResult:
    """Fetch HLTB data for a specific game ID, bypassing name search.
    Returns HltbResult(found=False) on bad ID / network / parse error."""

def parse_hltb_id_input(text: str) -> Optional[int]:
    """Extract an HLTB ID from user input. Accepts bare integer or
    howlongtobeat.com/game/<id> URL (with or without scheme, www,
    query string, trailing slash)."""
```

`fetch_hltb_by_id` wraps `howlongtobeatpy.HowLongToBeat.search_from_id`
under `run_in_executor` — same async-via-thread pattern as `fetch_hltb`.

### Sync interaction

In `app/sync.py` `_phase_enrich` HLTB branch — three-way fork:

1. Cache hit + not stale → skip
2. Manual ID set → `fetch_hltb_by_id`; misses don't pollute
   `hltb_outcomes` (a bad manual ID isn't signal about server health
   for the adaptive-pacing window)
3. Default → name-based `fetch_hltb` with adaptive pacing

### Routes

- `POST /games/{appid}/hltb_id` — body has `hltb_id_input`. Parses via
  `parse_hltb_id_input`, fetches inline, persists on success via
  `set_hltb_id_manual`. On parse failure or fetch miss, returns the
  data-section partial with `hltb_error` populated and DOES NOT persist.
- `POST /games/{appid}/reset_hltb_id` — clears manual ID, re-runs
  `fetch_hltb` inline, persists via `clear_hltb_id_manual`. If the
  name-search misses, writes NULL HLTB values rather than leaving stale
  ones (so the user sees the same result a fresh refresh would produce).

### Templates

- `partials/game_detail_data.html` — External column gets a `<details>`
  override block with text input + Save + hint about the URL format.
  Inline error pane (`.hltb-override-error`) renders when `hltb_error`
  is populated. Manual-ID indicator + Reset button on the HLTB Main row.

The full-page render context (`_build_full_context`) sets
`hltb_error: None` so the partial can be `{% include %}`d cleanly on
page load.

### CSS

`.hltb-override-block`, `.hltb-override-summary`, `.hltb-override-body`,
`.hltb-override-loading`, `.hltb-override-form`, `.hltb-override-label`,
`.hltb-override-input`, `.hltb-override-error` (warn-coloured pane),
`.hltb-override-hint` (with inline `<code>` styling),
`.data-aside--manual` (accent-colour indicator next to HLTB Main).

## Surface 3 — Manual stickiness badge

### Use case

During the Phase 1c soak period, the user observes a badge that feels
obviously wrong on a specific game. Immediate escape hatch rather
than waiting for threshold tuning or new signal sources. When override
is set, all surfaces (Game Detail header, Library row, Shortlist pill)
display the manual value. The auto-computed score breakdown still
appears on Game Detail with an indicator that the user override is
active — preserves "what would the system say" without losing the
"what does the user say" decision.

### Schema

```sql
ALTER TABLE games ADD COLUMN stickiness_badge_manual TEXT;
```

Valid values: any of the five `ACTIVE_BADGES` constants
(`hooks_players` / `filters_early` / `marathon` / `mixed_signals` /
`standard_engagement`). `limited_data` is intentionally NOT a valid
override — manually asserting "no signal" is meaningless; clearing
the override is the right way to revert. The route layer validates
against `ACTIVE_BADGES` before persisting.

### Domain helper (`app/hook_metrics.py`)

```python
def compute_stickiness_signal_display(game) -> tuple:
    """Returns (badge, auto_badge, score, breakdown, is_overridden).
       - badge: what to display (manual override if set, else auto)
       - auto_badge: what the auto path returned regardless
       - score, breakdown: from the auto path (informational)
       - is_overridden: True when game.stickiness_badge_manual is set"""
```

Used by every call site that previously called `compute_stickiness_signal`
directly:

- `library_row.html` (Library Stickiness column)
- `game_card.html` (Shortlist pill)
- `game_detail_engagement.html` (header line + breakdown)
- `app/routes/library.py` `_sort_games` (column sort key)

All four updated atomically in the Surface 3 commit.

### Override scope (per design-question Q4)

Override applies on ALL game types — including those where the auto
path would short-circuit to `limited_data` (software / beta_playtest /
early_access / unknown). On those types the engagement section is
normally hidden; with override set, the section becomes visible to
surface the manual badge, and the breakdown reads "Auto: not computed
for this type (engagement section normally hidden — your override
surfaces it anyway)".

This is the maximum-utility version of the override: the user's
assertion bypasses both the auto-compute logic AND the type-based
suppression rules. The cases where the user is most likely to disagree
with the model are exactly the cases where the model has the least
data — letting the override apply there is the point.

### Routes

- `POST /games/{appid}/stickiness_badge` — body has `badge`, validated
  against `ACTIVE_BADGES`. Persists via `set_stickiness_badge_manual`.
- `POST /games/{appid}/reset_stickiness_badge` — clears via
  `clear_stickiness_badge_manual`. Auto-computed badge resumes.

### Templates

- `library_row.html` — Stickiness column reads
  `compute_stickiness_signal_display(gws.game)`; renders the displayed
  badge with `.stickiness-badge--overridden` outline class when
  `is_overridden`, tooltip swaps to "Manually set: <label>".
- `game_card.html` — Shortlist pill same pattern, bypasses
  `limited_data` hiding when override is set.
- `game_detail_engagement.html` — header line shows the manual badge
  with `manually set` aside + Reset button. Below the header, the
  auto disclosure renders either:
  - "Auto would say: <auto_badge> (score X.X)" + breakdown, or
  - "Auto: not computed for this type" (when auto returned
    `limited_data` due to ineligible type).
- The Override picker `<details>` appears at the bottom of the
  engagement section on every eligible-by-override game (always
  visible — discoverable affordance).

### CSS

`.stickiness-badge--overridden`, `.stickiness-pill--overridden`
(dashed accent outline on the existing badge tones),
`.engagement-signal-aside--manual` (accent-colour italic),
`.engagement-signal-auto-note` (auto-disclosure block under
manually-set badges),
`.stickiness-override-block`, `.stickiness-override-summary`,
`.stickiness-override-body`, `.stickiness-override-form`,
`.stickiness-override-label`, `.stickiness-override-select`.

## Tests

Three new test files under `tests/`:

- `test_phase4_completion_override.py` — 15 tests:
  `pick_completion_achievement` lookup behavior, heuristic-vs-override
  rate resolution, forced 'high' confidence, reset path, Phase 1c
  signal recomputation under override, sync fail-open path.
- `test_phase4_hltb_override.py` — 18 tests: `parse_hltb_id_input`
  across bare integers, URL forms (https/http/no-scheme/www/uppercase
  host/query string/trailing slash), and rejected inputs (empty,
  whitespace, non-numeric, zero, negative, float, unrelated URL,
  wrong path on hltb).
- `test_phase4_stickiness_override.py` — 15 tests: `ACTIVE_BADGES`
  contract, `compute_stickiness_signal_display` return shape,
  override-replaces-displayed-badge, breakdown preservation,
  ineligible-type surfacing (software / beta_playtest / early_access /
  unknown / data-sparse linear), Library sort key routing.

Live HLTB API and Steam achievement API calls are not exercised by
the test suites — those run during manual Game Detail smoke tests
after each commit.

## Smoke-test path per surface

Each commit ends with a manual smoke test:

1. **Surface 1**: open Game Detail for a game with achievements,
   click "Override completion achievement", pick a different
   achievement, save, verify the engagement section refreshes with
   the new completion_rate / "manually set" indicator. Click Reset,
   verify the heuristic value comes back.
2. **Surface 2**: open Game Detail for a game with a wrong HLTB
   match, click "Override HLTB match", paste an ID or URL, save,
   verify HLTB Main / Main+Extra / Completionist update inline. Try
   a bad ID, verify the error pane renders without persisting. Click
   Reset, verify name-search resumes.
3. **Surface 3**: open Game Detail, click "Override stickiness
   badge", pick a different badge, save, verify the badge changes on
   Game Detail header + Library row + (if game appears) Shortlist
   card. Verify "Auto would say…" still renders the breakdown. Click
   Reset, verify auto badge returns. Try the same on a software /
   beta_playtest game, verify the engagement section becomes visible
   and the override surfaces with "Auto: not computed for this type".

## Done criteria

- All three `*_manual` columns exist via try/except ALTER TABLE
- Each surface has set / clear DB helpers + corresponding routes
- Templates render correctly under override-set, override-cleared,
  ineligible-type, and data-sparse paths (verified via inline render
  in commit-time smoke checks)
- All three test suites pass
- Phase 1c categorical signal still works under override on Surface 1
  (override changes `completion_rate` → signal recomputes via the
  weighted-score model)
- Manual smoke test of all three surfaces passes against a live game
- All existing pages still 200; Phase 1a / game-type / Phase 1c
  shipped behavior preserved

## Out of scope

- Bulk override UI (set 100 games at once) — single-game corrections
  only, matches the existing override pattern
- Override audit log (when did the user set this, what was the auto
  value at the time) — not asked for; can be added if soak reveals
  the need
- Override on cliff metrics (early/late position, cliff size) —
  cliff is structural, not a labeling judgment; no override exists
- Override on review-derived metrics (review_playtime_median,
  stickiness_ratio) — these are aggregations of the underlying review
  set, not labels; no meaningful override surface
- Per-game custom badge labels beyond the five `ACTIVE_BADGES` —
  the badge taxonomy is shared across the library by design
