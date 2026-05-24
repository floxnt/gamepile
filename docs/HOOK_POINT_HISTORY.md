# Hook-Point History — Comprehensive Record for Revisit Consideration

## A. Original goal

The hook-point feature set was GamePile's attempt to answer one question
for each game in the user's Steam library: **"if I start this, will I
see it through?"** The product hypothesis was that Steam achievement
data, review playtime distributions, and SteamSpy engagement statistics
could be composed into a per-game "stickiness" signal — a categorical
badge the user could scan in Library/Shortlist views to filter for games
that match their engagement style (deep completionists vs. short-session
players vs. variety-seekers).

The feature predates v3 in concept. V2 laid the groundwork by adding
SteamSpy as a data source and building the affinity/taste-learning
pipeline. The v3 roadmap (documented in `docs/PROJECT_STATE.md` §v3)
envisioned four phases:

1. **Phase 1 — Stickiness signals:** Five per-game metrics from Steam
   achievements, Steam reviews, and SteamSpy playtime data, composed
   into a categorical badge.
2. **Phase 2 — Hour-mapped progression curves:** Identify progression
   achievements by name pattern ("Chapter X", "Act X", "Mission X"),
   map them to estimated hours via HLTB, identify the sharpest drop-off
   hour.
3. **Phase 3 — Community signal aggregation:** Pull Metacritic/OpenCritic
   user reviews and Reddit posts, LLM-extract "hook-point" mentions,
   combine with Phase 2 estimates into a confidence-weighted hour range.
4. **Phase 4 — Manual curation overrides:** User-supplied corrections
   for games where automated extraction fails.

The display goal was a clean range ("Hook point: hours 3–6") on Shortlist
cards, with per-source breakdown on Game Detail. The behavioral goal was
to let the Shortlist recommender incorporate engagement likelihood into
pick scoring — a game that hooks most players would rank higher for
"Start something new" mode; a game that filters early would rank lower
unless the user's own affinity signals overrode the population data.

The stickiness signal was the prerequisite for Phase 2's hour-mapped
curves and Phase 3's community aggregation. Without a reliable
categorical signal, the downstream phases had no validated input to
build on.

## B. Phased approach as designed

### Phase 1a — Raw metrics (spec: `SPEC_V3_HOOK_PHASE_1A.md`)

Shipped 2026-05-03. Five per-game metrics, all nullable:

| Metric | Source | What it measures |
|---|---|---|
| `completion_rate` | Steam `GetGlobalAchievementPercentagesForApp` + `GetSchemaForGame` | Unlock % of the lowest-% story-completion achievement (heuristic match), or lowest-% achievement overall (fallback) |
| `completion_rate_confidence` | Same | `'high'` when displayName matches strong patterns (ending/credits/epilogue) AND unlock ≤ 50%; `'low'` otherwise |
| `cliff_metric` | Same achievement data | Largest single percentage-point drop between consecutive achievements sorted by unlock % desc, after discarding top 2–3 launch achievements |
| `review_playtime_median` | Steam `appreviews` (per-review `author.playtime_at_review`) | Median reviewer playtime in minutes; NULL if < 10 reviews |
| `stickiness_ratio` | Same review data + HLTB main hours | Fraction of reviewers whose playtime_at_review ≥ 0.5 × HLTB main (or flat 20h fallback) |
| `playtime_median_avg_ratio` | SteamSpy `median_forever / average_forever` | High ≈ even engagement; low ≈ long-tail bounce. **Structurally dead** — SteamSpy free tier returns 0 for both fields |

Supporting work: `app/fetchers/steam_achievements.py` (new fetcher
combining `GetGlobalAchievementPercentagesForApp` + `GetSchemaForGame`),
`app/hook_metrics.py` (pure-functional computation module), Game Detail
"Engagement signals" section displaying raw numbers.

The story-completion heuristic was verified against 10 named games via
`tests/verify_story_completion_heuristic.py`. Schema resolution
(`GetSchemaForGame`) was essential: ~80% of games return opaque internal
IDs (`ACH00`, `Achievement_GOSCC_NNN`) from the percentages endpoint;
without schema-resolved displayNames, pattern matching was ~12% accurate.
With schema resolution: ~40% correct match + the rest correctly flagged
as low confidence.

### Game-type classification (spec: `SPEC_V3_GAME_TYPE_CLASSIFICATION.md`)

Shipped 2026-05-03, same session as Phase 1a. Eleven-value `game_type`
taxonomy (linear, multiplayer, no_endpoint, mixed, mmo, sandbox,
beta_playtest, software, expansion, early_access, unknown) to control
which metrics display per game type. Key design: for
multiplayer/mmo/no_endpoint/sandbox types, completion and cliff metrics
are hidden (achievement completion isn't meaningful for open-ended games);
for software, the entire engagement section is suppressed; for
beta_playtest/early_access/unknown, the categorical badge is suppressed
regardless of populated metrics.

### Phase 1b — Categorical signal (spec: `SPEC_V3_HOOK_PHASE_1B.md`)

Shipped 2026-05-05. Combined three of the Phase 1a metrics into a
four-state badge: **Sticky / Average / Filters players hard /
Insufficient data**. Used a 2-of-3 voting model: cliff, stickiness_ratio,
and high-confidence completion each contributed one of sticky/filters_hard/
neutral/no_data. Badge required 2-of-3 agreement for Sticky or Filters;
Average was the catch-all.

Live distribution across 634 games:
- Sticky: 27 (4.3%)
- Average: 377 (59.5%)
- Filters players hard: 11 (1.7%)
- Insufficient data: 219 (34.5%)

The Active signal density (Sticky + Filters) was only 6.0%. The Average
bucket at 60% conveyed no information. The 2-of-3 voting model forced
binary contributions across heterogeneous signals, dropping
low-confidence completion entirely (which covered ~90% of games).

### Phase 1c — Weighted scoring (spec: `SPEC_V3_HOOK_PHASE_1C.md`)

Shipped 2026-05-07, refined 2026-05-11. Five changes:

1. **New `cliff_position` metric** — where in the achievement list the
   largest cliff sits, [0.0, 1.0]. Distinguishes early-game abandonment
   cliffs (filters signal) from late-game completionist gates (neutral).
2. **Weighted scoring** replacing voting — each signal returns -1/0/+1
   with weights: stickiness 1.5, cliff 1.0, high-confidence completion
   0.7, low-confidence completion 0.3.
3. **Recalibrated completion thresholds** against published
   Steam-population data (Bailey & Miyata 2019: median ~10%, mean ~14%).
   Sticky raised to 0.25 (was 0.15), filters raised to 0.05 (was 0.03).
4. **Average split** into Marathon (≥50h playtime + high-conf completion
   < 0.10), Mixed signals (in-band with strong contributors), Standard
   engagement (in-band, no strong signals).
5. **Lean sub-buckets** (refinement, 2026-05-11): "Usually hooks"
   (score [+0.5, +1.5)) and "Often filters" (score (-1.0, -0.5]).
   Final taxonomy: 8 badges.

Final distribution across 636 games:
- Hooks players: 65 (10.2%)
- Usually hooks: 48 (7.5%)
- Filters early: 24 (3.8%)
- Often filters: 12 (1.9%)
- Marathon: 4 (0.6%)
- Mixed signals: 3 (0.5%)
- Standard engagement: 291 (45.8%)
- Limited data: 189 (29.7%)

Active signal density improved from 6.0% to 14.0%. However, Standard
engagement at 45.8% was unchanged by the lean split because games with
no strong contributor could only contribute via low-confidence completion
(weight ±0.3), which never reaches the ±0.5 lean threshold. The
spec acknowledged this: "it'd suggest the score formula itself needs
revisiting, not just additional thresholds."

### Phase 4 — Manual overrides (spec: `SPEC_V3_PHASE_4_OVERRIDES.md`)

Shipped 2026-05-09. Three Game Detail surfaces:

1. **Manual story-completion achievement** — user picks the correct
   completion achievement from a lazy-loaded picker (Steam API call on
   toggle). Stored as `completion_achievement_name_manual`, forces
   confidence to `'high'`.
2. **Manual HLTB ID** — user supplies a HLTB game ID or URL when
   name-search mismatches. (Still live — not retired with hook-point.)
3. **Manual stickiness badge** — user overrides the categorical badge
   directly. Engagement section becomes visible even for ineligible
   game types to surface the override.

Also shipped: Library stickiness badge filter (single-select dropdown).

### Phases 2 and 3 — Never implemented

Phase 2 (hour-mapped progression curves from achievement name patterns)
and Phase 3 (Reddit/LLM-extracted community signals) were never built.
Phase 1's retirement made them moot — without a validated stickiness
signal, the downstream phases had no reliable input to consume.

## C. What was implemented vs. what was retired

### Implemented and still present (dormant)

| Component | Status |
|---|---|
| `app/hook_metrics.py` — all 17 functions, threshold constants, badge taxonomy | Retained, DORMANT header |
| 9 DB schema columns on `games` table | Retained, no DROP COLUMN |
| `tests/test_hook_metrics.py` | Retained, DORMANT header, tests pass |
| `tests/test_hook_phase1c.py` | Retained, DORMANT header, tests pass |
| `tests/test_phase4_completion_override.py` | Retained, DORMANT header |
| `tests/test_phase4_stickiness_override.py` | Retained, DORMANT header |
| 4 diagnostic scripts in `tests/` | Retained, read-only DB reports |
| COALESCE-on-upsert in `database.py upsert_game` | Active — preserves any dormant data across refresh |

### Removed from live product (v0.7.0)

| Component | What was removed |
|---|---|
| Library view | Stickiness column, stickiness filter dropdown, stickiness sort |
| Game Detail | Entire Engagement signals section, manual completion-achievement picker, manual stickiness badge override |
| Shortlist card | Stickiness pill |
| Routes | 5 endpoints under `/games/{appid}/` (achievements GET, completion_achievement POST/reset, stickiness_badge POST/reset) |
| DB write helpers | `set_completion_achievement_manual`, `clear_completion_achievement_manual`, `set_stickiness_badge_manual`, `clear_stickiness_badge_manual` |
| Jinja globals | 7 stickiness/engagement template helpers |
| CSS | All `.stickiness-*`, `.engagement-*`, `.completion-override-*` rules |
| Sync hooks | Achievement-fetching block in `_phase_enrich`, SteamSpy ratio computation, review-playtime-derived computations |

### What replaced it (v0.7.0)

A single display-only stat: **median per-achievement global unlock
percent**. Stored in `games.median_achievement_unlock_pct`. Computed
from the same `GetGlobalAchievementPercentagesForApp` data source but
using a different summary statistic (median of all per-achievement
unlock percentages, not a heuristic-matched completion achievement).
Displayed in Library and Game Detail as a labeled number. Explicitly
NOT wired into the Shortlist recommender.

## D. The retirement decision

### Triggering evidence

The empirical achievement-signal probe run on 2026-05-19 against the
live 636-game library produced the finding that drove retirement:

**430 of 476 achievement-bearing games (90.3%) had `completion_rate`
values with `completion_rate_confidence = 'low'`.** Only 46 games (9.7%)
hit the pattern-matched story-completion path that produces a `'high'`
confidence label. Every other game fell back to "lowest-percent
achievement overall" as the completion proxy.

The pipeline presented this 90%-low-confidence inference as if
authoritative via the Phase 1c categorical badges. The user reads
"Hooks players" or "Filters early" — a confident label — without
knowing that the underlying completion signal is mostly a heuristic
guess about which achievement represents story completion.

### Why this was treated as a correctness problem, not a polish problem

The project's design rules treat "presenting low-confidence inference as
authoritative" as a correctness problem. The categorical badge collapses
confidence information into a single label. There is no way to "relabel"
the badge to be honest without removing the badge entirely — the badge's
value proposition is precisely that it gives a quick categorical read,
and a quick categorical read of low-confidence data is misleading by
construction.

### What worked

- Marathon validation worked: the canonical "play forever without
  finishing" cases (Red Dead Redemption 2, Civilization VI, Total War:
  WARHAMMER II/III) all correctly hit the Marathon label.
- Phase 1c distribution shape hit target bands (10.2% Hooks within
  10–25% target, 0.6% Marathon within ≤10% target, 29.7% Limited
  data within ≤30% target).
- Manual override surfaces from Phase 4 were cleanly implemented.
- The lean-bucket split (Usually hooks / Often filters) successfully
  reorganized the former Mixed signals bucket.

### What didn't fix the structural problem

None of the Phase 1b → 1c improvements fixed the fundamental issue:
the completion_rate metric was unreliable for 90% of games. Weighted
scoring, recalibrated thresholds, cliff position awareness, and
lean-bucket sub-division all improved the *presentation* of the signal
but couldn't improve the *signal quality* because the input data
(achievement names as story-completion proxies) was structurally noisy.

### Preserved for reevaluation

The retirement spec (`SPEC_HOOK_RETIREMENT.md`) explicitly states a
reevaluation intent around ~v1.0:

> "The pipeline and data are preserved so the question 'is there a useful
> honest signal here' can be revisited later with more data."

Reevaluation requirements listed:
1. Re-run probe-style empirical characterization on the then-current
   library
2. Decide explicitly: re-enable the dormant pipeline, replace with a
   different formulation, or purge for real
3. Document the decision with the same structure as the retirement record

## E. Methodology of probes and experiments

### Probe 1: Story-completion heuristic verification (Phase 1a gate)

**Hypothesis tested:** Pattern-matching achievement names against
STORY_COMPLETION_PATTERNS (complete, finish, ending, credits, the end,
epilogue, final) can reliably identify the story-completion achievement.

**Method:** `tests/verify_story_completion_heuristic.py` — fetches
Steam global achievement percentages for 10 named games (Persona 5
Royal, Dark Souls III, Hades, Witcher 3, Portal 2, Disco Elysium,
Mass Effect, Talos Principle, Stanley Parable, Firewatch), prints the
full sorted achievement list, marks the heuristic's pick, and self-flags
suspicious picks (launch achievements > 50% unlock, lower-% achievements
the heuristic missed).

**Finding:** Schema resolution (`GetSchemaForGame`) was essential —
without it, internal IDs were opaque for ~80% of test games. With
schema resolution, ~40% of games got a correct high-confidence match;
the remainder were correctly flagged as low confidence. The heuristic
was deemed "good enough for Phase 1a" but the high rate of low-confidence
fallbacks was a known limitation from the start.

### Probe 2: Phase 1a distribution report (post-backfill)

**Hypothesis tested:** The five Phase 1a metrics populate meaningfully
across the library — coverage isn't so sparse that downstream
categorization is impossible.

**Method:** `tests/report_phase1a_distribution.py` — queries the live
DB, reports per-metric coverage, min/max/median/mean, confidence
breakdown, cliff histogram, stickiness threshold-path split
(HLTB-relative vs flat fallback), game-type × metric cross-tabulation,
and a diagnostic of linear/mixed games where all 5 metrics are NULL.

**Key findings documented in PROJECT_STATE.md:**
- `playtime_median_avg_ratio` was structurally dead (SteamSpy free tier
  returns 0 for both input fields). Dropped from Phase 1b categorization.
- ~5% of libraries hit a Steam appreviews API quirk for pre-2012 catalog
  games: drastically reduced review counts despite thousands of total
  reviews. These games will always show insufficient data for
  review-derived metrics.

### Probe 3: Phase 1c distribution and target-envelope check

**Method:** `tests/report_phase1c_distribution.py` — computes the Phase
1c weighted-score badge for every active game, reports overall counts,
per-game-type breakdown, Phase 1b → 1c migration diff, target-envelope
verdicts, and diagnostic samples (3 per badge).

**Key findings:**
- Active signal density 6.0% → 14.0% (Phase 1b → 1c)
- Filters early at 3.8% was 1.2pp under the 5% target floor —
  structural, not threshold-tunable (older catalog games with cliff
  data but no stickiness or high-conf completion max out at score -1.0)
- Standard at 45.8% was unchanged by the lean split — structural because
  games with no strong contributor cap at score ±0.3 via low-conf
  completion alone
- Limited data at 29.7% had a 12.7% structural floor: 81 games
  (beta_playtest 18 + early_access 32 + unknown 27 + software 4)
  short-circuit regardless of signal work

### Probe 4: Cliff-position spot-check (Phase 1c gate)

**Method:** `tests/check_cliff_position.py` — verifies populated-envelope
sanity (cliff_position count = cliff_metric count), reports
early/mid/late bucket distribution, spot-checks 6 games stratified by
position.

### Probe 5: Empirical achievement-signal probe (retirement trigger, 2026-05-19)

**Hypothesis tested:** Is the completion_rate signal sufficiently
reliable for the categorical badges to be honest?

**Method:** Queried the live 636-game library for the confidence
breakdown of all populated completion_rate values. Cross-referenced
against the retirement-spec's findings about the average-% axis
(Spearman correlation with existing `completion_rate`).

**Finding:** 430/476 (90.3%) of completion_rate values self-labeled as
low confidence. The probe also found a Spearman correlation of 0.49
between the median-of-all-achievements-% axis and the existing
`completion_rate` — meaning the new median stat carries meaningfully
novel information (only ~49% shared variance) relative to the dormant
completion_rate.

### Probe data artifacts

The empirical probes produced two artifact files referenced in
conversation history:

- `global_achievement_percents.csv` (~1.5 MB, 30,538 rows of
  per-achievement global unlock percentages)
- `probe_results.json` (~1.2 MB, structured probe output)

**Current status:** These files were on the WSL/Windows development
machine (`/home/ike/Projects/gamepile-probe-data/`). They do NOT exist
on the current CachyOS-native machine. The probe data is not preserved
on disk in the current development environment. If a revisit requires
re-analysis of the original probe data, it would need to be either
recovered from the WSL environment or regenerated from a fresh probe
run.

## F. Results and failure modes

### Failure mode 1: Heuristic completion proxy is unreliable at scale

**Finding:** 90.3% low-confidence rate. The story-completion heuristic
(pattern-matching achievement displayNames for words like "ending",
"credits", "epilogue") only works for games where developers used
unambiguous completion-signaling names. Most developers use themed names
("One for the Ages" for Hades' epilogue, "You Died" for a FromSoftware
final boss) that don't match any substring pattern.

**Consequence:** The `completion_rate` metric is structurally a
lowest-percent-achievement fallback for 90% of games. The lowest-percent
achievement is a reasonable proxy for "rare endgame thing" but it's
noisy — it could be a completionist challenge, a secret achievement, a
broken achievement, or an actual story endpoint. There's no way to
distinguish these cases from the data alone.

### Failure mode 2: SteamSpy playtime data structurally dead

**Finding:** SteamSpy's free-tier `appdetails` endpoint returns 0 for
both `median_forever` and `average_forever`. The
`playtime_median_avg_ratio` metric (high ratio = even engagement,
low ratio = long-tail bounce) is permanently NULL for all games via
this data source.

**Consequence:** One of the five Phase 1a metrics was always NULL. Phase
1b correctly dropped it from categorization. No alternative free data
source for this statistic was identified.

### Failure mode 3: Steam reviews API quirk for pre-2012 catalog

**Finding:** Approximately 5% of a typical library consists of games
predating ~2012 that return drastically reduced review counts via the
Steam appreviews API. The API paginates poorly for old catalog entries.

**Consequence:** These games always show insufficient data for
`review_playtime_median` and `stickiness_ratio`. This is a Steam API
limitation, not fixable on GamePile's side.

### Failure mode 4: 2-of-3 voting conflates heterogeneous signals

**Finding (Phase 1b → 1c transition):** The voting model forced each
signal into binary sticky/filters, then required 2-of-3 agreement.
This dropped low-confidence completion entirely (which covered ~90%
of games), locked mixed-type games out of the Sticky badge (no
high-confidence completion → third signal always no_data → can't reach
2-of-3 majority), and packed three distinct engagement patterns into
"Average."

**Resolution:** Phase 1c's weighted scoring fixed this specific failure
mode. Active signal density improved from 6.0% to 14.0%.

### Failure mode 5: Standard engagement remains opaque

**Finding (Phase 1c refinement):** 45.8% of games in Standard
engagement after the lean split. Structural reason: games with no
strong contributor (stickiness/cliff/high-conf completion all zero)
can only score via low-conf completion at weight ±0.3, never reaching
the ±0.5 lean threshold. The lean split successfully reorganized
Mixed signals but couldn't drain Standard.

**Implication for revisit:** Any future signal work that doesn't add
a new data source or fundamentally change the score formula will leave
Standard at ~46%. The score formula itself would need revisiting, or
a new data source would need to provide strong signals for the games
currently stuck at zero.

### What WAS a positive finding

- **Median achievement unlock %** (the replacement stat) carries
  meaningfully novel information: Spearman 0.49 correlation with the
  dormant `completion_rate` means ~75% independent variance. This
  validates using the same data source (achievement percentages) with
  a different summary statistic (median across all achievements vs.
  heuristic-matched single achievement) as a complementary signal.
- **Cliff position** successfully distinguished early-game abandonment
  from late-game completionist gates. The information was valid; the
  question was whether the cliff signal alone was strong enough to
  justify the categorical badge.
- **Stickiness ratio** was the most-populated and most-reliable signal.
  Its 0.5× HLTB-main adaptive threshold correctly scaled across game
  lengths.

## G. Current state of dormant data

### Database columns (live DB on CachyOS)

The current CachyOS database was created fresh on 2026-05-23 for the
Qt pivot (v0.9.0). All dormant hook-point columns exist in the schema
but contain **zero populated values**:

| Column | Populated count (of 648 active games) |
|---|---|
| `completion_rate` | 0 |
| `completion_rate_confidence` | 0 |
| `cliff_metric` | 0 |
| `cliff_position` | 0 |
| `review_playtime_median` | 0 |
| `stickiness_ratio` | 0 |
| `playtime_median_avg_ratio` | 0 |
| `completion_achievement_name_manual` | 0 |
| `stickiness_badge_manual` | 0 |
| `median_achievement_unlock_pct` (live, not dormant) | 484 |

The dormant data is empty because the hook-point sync logic was already
removed (v0.7.0) before this DB was created. The original populated data
existed on the WSL/Windows development machine's DB. The COALESCE-on-upsert
mechanism preserves existing values but can't create values that were
never written.

### Code preserved

- **`app/hook_metrics.py`** — 764 lines, 17 functions, all threshold
  constants and badge taxonomy. Pure-functional, no DB/FastAPI imports.
  Top-of-module DORMANT header points at SPEC_HOOK_RETIREMENT.md.
- **4 test files** — `test_hook_metrics.py`, `test_hook_phase1c.py`,
  `test_phase4_completion_override.py`, `test_phase4_stickiness_override.py`.
  All pass. DORMANT headers.
- **4 diagnostic scripts** — `report_phase1a_distribution.py`,
  `report_phase1c_distribution.py`, `check_cliff_position.py`,
  `verify_story_completion_heuristic.py`. Read-only, runnable against
  the DB.
- **Sync passthrough** — `app/sync.py` still passes dormant column
  values through `upsert_game` (lines ~494–518, ~566–579). This is the
  COALESCE preservation mechanism — existing values survive refresh.
  The sync no longer *computes* new values for these columns.

### Probe data on disk

The probe data files (`global_achievement_percents.csv`,
`probe_results.json`) are **not present** on the current development
machine. They were on the WSL/Windows machine at
`/home/ike/Projects/gamepile-probe-data/`. Recovery would require
accessing the WSL environment or regenerating from a fresh probe.

### Sync paths still wired vs. fully removed

| Path | Status |
|---|---|
| Achievement fetch (`GetGlobalAchievementPercentagesForApp` + `GetSchemaForGame`) for `median_achievement_unlock_pct` | **Active** — this is the live stat, not hook-point |
| Achievement fetch for `completion_rate` / `cliff_metric` / `cliff_position` | **Removed** from sync |
| Per-review playtime extraction for `review_playtime_median` / `stickiness_ratio` | **Removed** from sync (reviews still fetched for aggregate `steam_review_pct` / `steam_review_count`, but per-review playtime array is discarded) |
| SteamSpy `playtime_median_avg_ratio` computation | **Removed** from sync |
| COALESCE passthrough for all dormant columns in `upsert_game` | **Active** — preserves any existing data |

## H. Open questions for a productive revisit

### 1. What evidence would justify another attempt?

The core failure was that the story-completion heuristic was unreliable
for 90% of games. A revisit that produces a different outcome would need
to change at least one of:

- **The data source.** Steam's `GetGlobalAchievementPercentagesForApp`
  + `GetSchemaForGame` is the only free, no-auth/low-auth data source
  for per-achievement population-level unlock rates. No alternative has
  been identified. Valve could expose richer achievement metadata in
  the future (achievement categories, progression flags), but there's
  no indication this is planned.

- **The completion identification methodology.** The substring-matching
  heuristic is the bottleneck. Alternatives:
  - **LLM-based classification** of achievement names/descriptions into
    "story completion" / "challenge" / "collectible" / "progression" /
    "misc". Would require either a local LLM or an API call per game.
    Cost and latency scale with library size. The project's "no LLM in
    runtime" design rule (documented in PROJECT_STATE.md "Out of scope")
    would need to be reconsidered for a one-time classification pass
    during sync.
  - **Community-maintained achievement databases.** None with
    sufficient coverage have been identified.
  - **Abandoning completion identification entirely** and using only
    stickiness_ratio + cliff metrics + a differently-formulated
    aggregate stat. The median-achievement-unlock-% stat that replaced
    hook-point takes this approach — it sidesteps the "which achievement
    is completion?" question entirely.

- **The product framing.** Instead of a categorical badge claiming "this
  game hooks players," a display-only stat (like the median unlock % that
  already shipped) provides honest data for the user to interpret. The
  question is whether a second honest stat adds enough value beyond
  what median unlock % already provides.

### 2. Is the dormant pipeline worth preserving further?

The dormant pipeline's compute functions are pure-functional and tested.
They represent non-trivial domain work. However:

- The DB columns are empty on the current machine (data loss from
  machine migration)
- The sync paths are removed (would need to be re-wired)
- The probe data is not on disk (would need regeneration)
- The pipeline's output (categorical badges) was the thing that was
  found to be unreliable

If a revisit takes a fundamentally different approach (different data
source, different methodology, different product framing), the dormant
pipeline code may not be reusable. The threshold constants and scoring
weights were tuned to the specific signal mix; changing any input
invalidates the tuning.

If a revisit is judged unlikely or if the approach would differ enough
that the dormant code isn't reusable, a clean purge (DROP COLUMN,
delete `hook_metrics.py`, delete dormant tests) is the honest
alternative to indefinite preservation.

### 3. Does the Spearman 0.49 finding suggest a productive direction?

The empirical probe found Spearman 0.49 between the median-of-all-
achievements-% axis and the existing `completion_rate`. This means the
two statistics carry ~75% independent variance — they're measuring
meaningfully different things about the same underlying data.

The median unlock % is already shipped as a display-only stat. If a
revisit wanted to build a categorical signal, it could potentially
compose `median_achievement_unlock_pct` (already populated, honest,
no completion-identification needed) with `stickiness_ratio` (the
strongest of the dormant signals, but currently not computed because
per-review playtime extraction was removed from sync) and
`cliff_metric` + `cliff_position` (ditto — achievement data is fetched
for median unlock but cliff computation was removed).

This is the most plausible incremental path: re-enable cliff and
stickiness computation alongside the already-active median-unlock fetch,
compose a signal that doesn't depend on completion identification at
all, and see if the resulting badge distribution is informative enough
to display.

### 4. What would "informative enough" mean?

The Phase 1c target envelope (10–25% Hooks, 5–15% Filters, ≤10%
Marathon, ≤30% Limited, Mixed + Standard ≤60%) was an attempt to define
"informative enough" by distribution shape. A revisit should set similar
targets before building, not after — and should include a
validation methodology beyond "does the distribution hit target bands?"

The missing validation in the original work: **does the signal correlate
with user behavior?** Does a game labeled "Hooks players" actually get
played longer by this user? Does "Filters early" actually predict
bouncing? Without behavioral validation, the signal is a claim about
population engagement that may or may not match the individual user's
experience — and there's no way to test this without a sustained period
of the user interacting with the signal and providing feedback.

## I. Git history cross-reference

Relevant commits in chronological order:

| Date | Hash | Description |
|---|---|---|
| 2026-05-03 | `c6961f7` | Add v3 hook-point Phase 1a spec |
| 2026-05-03 | `ef1d995` | Phase 1a: schema + fetcher + domain module + tests |
| 2026-05-03 | `d5946c9` | Phase 1a: sync wiring + Steam reviews expansion |
| 2026-05-03 | `7006c8a` | Phase 1a: Game Detail engagement signals section |
| 2026-05-03 | `99f70f9` | Game-type classification: schema + module + tests |
| 2026-05-03 | `065f55f` | Game-type: tighten mixed rule (Co-op user_tag) |
| 2026-05-03 | `bf75f7a` | Game-type: sync orchestration |
| 2026-05-03 | `21db072` | Game-type: Library column + Game Detail dropdown |
| 2026-05-03 | `2b8ce06` | Game-type: engagement_display_rules integration |
| 2026-05-04 | `f3abc93` | Phase 1a distribution report rewrite |
| 2026-05-05 | `a5bee02` | Add Phase 1b spec |
| 2026-05-05 | `d3c67b2` | Phase 1b: domain helpers |
| 2026-05-05 | `95ca434` | Phase 1b: Game Detail signal surface |
| 2026-05-05 | `e7eb1d8` | Phase 1b: Library column + Shortlist pill + CSS |
| 2026-05-07 | `7313789` | Phase 1c: cliff_position metric + spec |
| 2026-05-07 | `26ae8ce` | Phase 1c: weighted-score signal + badge taxonomy |
| 2026-05-07 | `ea4321e` | Phase 1c: Game Detail score breakdown |
| 2026-05-07 | `d106e3d` | Phase 1c: distribution report |
| 2026-05-07 | `a311801` | Phase 1c: SCORE_FILTERS_THRESHOLD -1.5 → -1.0 |
| 2026-05-07 | `ecc0e47` | Docs: Phase 1c shipped |
| 2026-05-09 | `1ff70ed` | Phase 4: manual completion-achievement override |
| 2026-05-09 | `ee312b4` | Phase 4: manual HLTB ID override |
| 2026-05-09 | `04b5e59` | Phase 4: manual stickiness badge override |
| 2026-05-09 | `8b4d61a` | Library stickiness badge filter — backend |
| 2026-05-09 | `df4cf56` | Library stickiness badge filter — template |
| 2026-05-11 | `0bf3de0` | Phase 1c refinement: Usually hooks / Often filters |
| 2026-05-12 | `e0282c9` | Game-type: app_type software rule fix |
| 2026-05-19 | `d15a2ac` | **Retirement: remove hook-point from live UI** |
| 2026-05-19 | `79bb13a` | Add median achievement unlock % (replacement stat) |

Total: 29 commits spanning 2026-05-03 through 2026-05-19 (16 days).
