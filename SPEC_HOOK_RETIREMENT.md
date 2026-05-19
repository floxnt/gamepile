# Hook-point / stickiness retirement — v0.7.0

## Why this exists

This SPEC documents the retirement of the hook-point / stickiness feature
from the GamePile live product in v0.7.0. The intent is that a future
maintainer (or fresh Claude Code session) reading this file understands
**deliberately preserved dormant ≠ dead code**. The hook-point pipeline,
schema, and accumulated data are still here on purpose; do not purge
them on a future cleanup pass without an explicit decision.

The companion SPECs covering the feature's design (`SPEC_V3_HOOK_PHASE_1A.md`,
`SPEC_V3_HOOK_PHASE_1B.md`, `SPEC_V3_HOOK_PHASE_1C.md`,
`SPEC_V3_PHASE_4_OVERRIDES.md`) remain in place as the historical record
of what the feature was. They describe code that no longer ships in the
live UI but whose pipeline functions are preserved in
`app/hook_metrics.py`.

## What was removed (UI / app)

Removed from the live user-facing product in v0.7.0:

- **Library view**: the Stickiness column, the Stickiness filter
  dropdown, the stickiness sort routing, the stickiness-related
  empty-state branch.
- **Game Detail page**: the Engagement signals section in its entirety
  — type line, stickiness-signal header, per-signal breakdown, "Auto
  would say…" disclosure, completion-rate row, cliff row, review-
  playtime row, sticky-reviewers row, playtime-ratio row.
- **Game Detail overrides**: the manual completion-achievement picker
  (lazy-loaded `<details>` block + Steam API call) and the manual
  stickiness-badge override (`<select>` dropdown + Save/Reset). These
  were the Phase 4 override surfaces. The HLTB-ID manual override is
  unrelated and stays.
- **Shortlist card**: the stickiness pill that sat under the badges row.
- **Routes** (now 404): `/games/{appid}/achievements` (GET — completion
  picker), `/games/{appid}/completion_achievement` (POST),
  `/games/{appid}/reset_completion_achievement` (POST),
  `/games/{appid}/stickiness_badge` (POST),
  `/games/{appid}/reset_stickiness_badge` (POST).
- **DB write helpers**: `set_completion_achievement_manual`,
  `clear_completion_achievement_manual`, `set_stickiness_badge_manual`,
  `clear_stickiness_badge_manual` in `app/database.py`. The upsert
  COALESCE behavior that preserves existing values across refresh is
  kept — see "What was preserved" below.
- **Jinja globals**: `compute_stickiness_signal`,
  `compute_stickiness_signal_display`, `engagement_display_rules`,
  `qualitative_ratio_hint`, `stickiness_active_badges`,
  `stickiness_badge_labels`, `stickiness_badge_tooltips` removed from
  `templates_config.py`.
- **CSS**: all `.stickiness-*`, `.engagement-*`, `.completion-override-*`,
  `.library-stickiness*`, `.engagement-override-*`,
  `.stickiness-override-*` rules in `app/static/style.css`. The unrelated
  `.hltb-override-*` rules stay.
- **Sync hook-point calls**: the entire achievement-fetching block in
  `app/sync.py _phase_enrich`, the SteamSpy `playtime_median_avg_ratio`
  computation, the review-playtime-derived `review_playtime_median` and
  `stickiness_ratio` computations. The sync still fetches Steam reviews
  for the aggregate `steam_review_pct` / `steam_review_count` fields;
  it just discards the per-review playtime array that fed the
  hook-point metrics.
- **Recommender**: no change required. The recommender (`app/recommender.py`)
  was structurally already not using hook-point/stickiness as a scoring
  input — its quality signal is Metacritic + Steam review % only, and
  its exclusions key off `game_type`. The "STOPS using the signal
  entirely" requirement was already structurally satisfied.

## What was preserved (dormant)

Deliberately preserved in place, **do NOT delete on a future cleanup pass**:

- **`app/hook_metrics.py`** — the entire module. Pipeline functions
  (`find_story_completion_achievement`, `pick_completion_achievement`,
  `compute_completion_rate`, `compute_completion_rate_confidence`,
  `compute_cliff_metric`, `compute_cliff_position`,
  `compute_review_playtime_median`, `compute_stickiness_ratio`,
  `compute_playtime_median_avg_ratio`, `qualitative_ratio_hint`,
  `compute_stickiness_signal`, `compute_stickiness_signal_display`) all
  retained. Threshold constants, badge taxonomy, scoring weights all
  retained. Module header marks the dormant status and points at this
  SPEC.
- **DB schema columns** — unchanged. `games.completion_rate`,
  `games.completion_rate_confidence`, `games.cliff_metric`,
  `games.cliff_position`, `games.review_playtime_median`,
  `games.stickiness_ratio`, `games.playtime_median_avg_ratio`,
  `games.completion_achievement_name_manual`,
  `games.stickiness_badge_manual` all remain. No DROP COLUMN migration
  shipped.
- **Accumulated data** — every value previously written to those
  columns is intact. The upsert_game COALESCE behavior preserves
  existing values across refresh so the dormant data set doesn't get
  silently nulled when sync runs without recomputing.
- **Tests** — `tests/test_hook_metrics.py`, `tests/test_hook_phase1c.py`,
  `tests/test_phase4_completion_override.py`,
  `tests/test_phase4_stickiness_override.py` all retained with a
  module-level `DORMANT (v0.7.0)` header pointing at this SPEC. Each
  exercises pure functions from `app/hook_metrics.py`; they pass and
  serve as a correctness guarantee for the preserved pipeline. The
  one removed test was `tests/test_library_stickiness_filter.py` —
  exercised UI behavior that no longer exists.
- **Diagnostic scripts** — `tests/check_cliff_position.py`,
  `tests/report_phase1a_distribution.py`,
  `tests/report_phase1c_distribution.py`,
  `tests/verify_story_completion_heuristic.py` retained. Not picked up
  by `tests/run_tests.py` (which only globs `tests/test_*.py`); they
  are read-only reports against the DB and remain runnable for a future
  reevaluation.

## Why retired (rationale)

The hook-point signal overpromised on low-confidence inference. The
empirical achievement-signal probe run on the live 636-game library
(2026-05-19) found:

- **430 / 476 (90.3%) of stored `completion_rate` values self-label as
  `'low'` confidence.** Only 46 games (9.7%) hit the pattern-matched
  story-completion path that produces a `'high'` confidence label;
  every other achievement-bearing game falls back to "lowest-percent
  achievement overall" as the completion proxy.
- **The pipeline silently presents this 90%-low-confidence inference as
  if authoritative** via the Phase 1c categorical badge ("Hooks players"
  / "Filters early" / "Marathon" / etc.). The user reads a confident
  label even though the underlying signal is mostly heuristic guess.
- **Marathon validation worked**, the Phase 1c distribution shape hit
  target bands, the manual-override surfaces from Phase 4 were
  cleanly implemented — none of that fixed the structural overclaim.
  The label sounds confident because the pipeline collapses confidence
  information into a single badge.

This is exactly the kind of "presenting low-confidence inference as
authoritative" failure mode the project's design rules treat as a
correctness problem, not a polish problem. The honest move is to
remove the feature from the live product rather than relabel it.

## Reevaluation intent (~v1.0)

The pipeline and data are preserved so the question "is there a useful
honest signal here" can be revisited later with more data. By v1.0 the
library will have had longer review of accumulated values, the Steam
achievement schema coverage may have improved, and the empirical
probe's findings about the avg-% axis being meaningfully novel
information (Spearman 0.49 to existing `completion_rate`) can be
designed against from a clean slate.

Concretely, a reevaluation round should:

1. Re-run the probe-style empirical characterization on the then-current
   library.
2. Decide explicitly whether the dormant pipeline gets re-enabled (and
   the UI re-added), gets replaced with a different formulation, or
   gets purged for real.
3. Whatever the decision, it should be documented with the same
   "what / why / what was preserved" structure as this retirement
   record.

Until that decision happens, the dormant code stays.

## Continuity rules for future maintainers / sessions

1. The presence of unused functions / columns / tests in this area is
   **deliberate**, not dead code awaiting cleanup. Confirm against this
   SPEC before purging anything matching the patterns
   `hook_metrics`, `stickiness*`, `completion_rate*`, `cliff_*`,
   `*_badge_manual`.
2. The compute functions in `app/hook_metrics.py` must remain pure
   (no DB / FastAPI imports) so reevaluation is straightforward.
3. The DB columns must keep COALESCE-on-upsert semantics in
   `app/database.py upsert_game` so existing dormant data survives
   refresh.
4. Tests marked `DORMANT (v0.7.0)` must remain runnable and green; if
   they break on a future refactor, fix the test or fix the pipeline,
   do not delete them. Their job is to keep the preserved code correct.
5. New signals must not be evolved silently from the dormant code. If
   a new completion-style signal is added, it goes through a fresh
   design decision; it does not inherit the hook-point label or
   threshold history without being explicitly chosen.

## What replaced it (v0.7.0)

A single display-only stat: **median per-achievement global unlock
percent**, computed from the same
`GetGlobalAchievementPercentagesForApp` endpoint the hook-point pipeline
used. Stored in a new nullable column `games.median_achievement_unlock_pct`,
rendered as a sortable Library column and a single stat on the Game
Detail external-data card. Display-only — not wired into the Shortlist
recommender's pick logic. See the v0.7.0 entry in `docs/PROJECT_STATE.md`
for the design and SPEC continuity note.
