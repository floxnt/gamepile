markdown# v3 Hook-Point Phase 1a Spec

## Purpose

Phase 1a computes and stores five new per-game metrics that will, in
Phase 1b, feed into a categorical "stickiness" signal. Phase 1a does
NOT define the categorical signal itself — that requires looking at
the actual distribution across the user's library before tuning
thresholds.

Phase 1a deliverable: all five metrics computed, stored, and surfaced
on Game Detail page as raw numbers. No categorical badge yet. No
changes to other views.

## Five new metrics

For each game, compute and store:

1. **completion_rate** (REAL, nullable, 0.0-1.0)
   - The unlock percentage of the lowest-unlocked story-completion
     achievement (or fallback: lowest unlock % overall)
   - Source: Steam `GetGlobalAchievementPercentagesForApp` joined with
     `GetSchemaForGame` (the latter resolves opaque internal IDs like
     `ACH00` to display names like "Roll Credits"; without it the
     heuristic fails for ~80% of games)
   - Story-completion identification heuristic: achievement names
     containing "complete", "finish", "ending", "credits", "the end",
     "epilogue", "final" (case-insensitive). Match runs against BOTH
     `displayName` (when schema-resolved) AND the internal `name` —
     games like Hades carry the signal in `name` (`AchReachedEpilogue`)
     while displayName is themed ("One for the Ages")
   - If no story-completion candidate found, fallback: use the lowest
     unlock % achievement overall
   - If game has no achievements: NULL

1a. **completion_rate_confidence** (TEXT, nullable, 'high' | 'low')
   - Honesty flag for the metric above. Phase 1b will treat 'high' and
     'low' differently when weighting completion_rate into the
     categorical stickiness signal.
   - **'high'** when the heuristic matched an achievement whose
     **displayName** (not internal name) contains a strong-pattern word:
     "ending", "endings", "credits", "the end", "epilogue",
     "finished the game", "beat the game" — AND the unlock percent is
     ≤ 50% (anything higher is a launch achievement, not a story
     endpoint)
   - **'low'** otherwise: heuristic fell back to lowest-overall, OR the
     match's displayName uses only weak words ("complete" / "final"),
     OR the unlock percent exceeds the 50% cap
   - **NULL** when the game has no achievements (no completion_rate to
     label)
   - The matching pattern set is intentionally permissive (catch as
     many real completions as possible); confidence labelling is
     intentionally strict (only mark high when the user-visible
     achievement text unambiguously says "you finished the story")

2. **cliff_metric** (REAL, nullable, 0.0-100.0)
   - The largest single percentage-point drop between consecutive
     achievements when sorted by unlock % descending
   - After discarding the top 2-3 achievements (filters out launch
     achievements that nearly everyone gets)
   - Specifically: discard top 3 if game has ≥10 achievements, top 2
     if game has 5-9, no discard if game has <5
   - Returns the cliff size in percentage points (e.g., 23.4 means
     "from 67% unlock to 43.6% unlock")
   - If game has no achievements or fewer than 4 after discard: NULL

3. **review_playtime_median** (INTEGER, nullable, in minutes)
   - Median value of `author.playtime_at_review` across all reviews
     fetched for the game
   - Already-fetched data; we just stop discarding it
   - If fewer than 10 reviews: NULL (too few to be meaningful)

4. **stickiness_ratio** (REAL, nullable, 0.0-1.0)
   - Fraction of reviewers whose `playtime_at_review` meets the sticky
     threshold. Threshold scales with the game's own length:
     - HLTB main present → `0.5 × hltb_main_hours × 60` minutes
       ("reviewer played at least half the main story before reviewing"
        — informed-opinion proxy that adapts to game length)
     - HLTB main missing → flat 1200 minutes (20 hours) as fallback so
       the metric stays useful for the ~9% of games without HLTB main
       data, rather than returning NULL
   - Rationale for the HLTB-relative threshold: a flat 20-hour cutoff
     is arbitrary across very different games. A 4-hour walking sim
     reviewer at 6 hours is sticky; a 200-hour CRPG reviewer at 25
     hours has barely started.
   - Computed alongside review_playtime_median from the same review data
   - If fewer than 10 reviews: NULL

5. **playtime_median_avg_ratio** (REAL, nullable, 0.0-1.0)
   - SteamSpy `median_forever / average_forever`
   - High ratio (close to 1.0) = even engagement distribution
   - Low ratio (close to 0) = long-tail bounce pattern (most players
     bounce, hardcore minority inflates the average)
   - If either field is missing or zero: NULL

## Schema additions

All on the `games` table, all nullable, all via try/except ALTER TABLE:

- `completion_rate REAL`
- `completion_rate_confidence TEXT` (values 'high' / 'low' / NULL)
- `cliff_metric REAL`
- `review_playtime_median INTEGER`
- `stickiness_ratio REAL`
- `playtime_median_avg_ratio REAL`

## New fetcher: app/fetchers/steam_achievements.py

Two endpoints, called together to produce display-name-annotated unlock
percentages. Schema resolution is essential: empirical verification
showed the percentages endpoint returns opaque internal IDs (e.g.
`ACH00`, `Achievement_GOSCC_NNN`, `TrophyTitle_NN_fiber_Steam`) for
~80% of the test games, making heuristic matching against names alone
~12% accurate. Adding schema-resolved displayName brings that to ~40%
correct + the rest correctly flagged as low confidence.

- `fetch_global_achievement_percentages(client, appid)` — public,
  no API key. `GetGlobalAchievementPercentagesForApp/v2/`.
- `fetch_achievement_schema(client, appid)` — requires API key (we
  have one). `GetSchemaForGame/v2/`. Returns `{internal_name:
  display_name}`. Best-effort: schema may be unavailable for games
  whose developers didn't publish a public schema; falls back to
  using the internal name as the display name.
- `fetch_achievements_with_metadata(client, appid)` — combined wrapper
  that returns the joined `[{name, displayName, percent}, ...]` list.
  This is what callers actually use.
- Pacing: simple per-request delay + retry-with-backoff (matches the
  appdetails fetcher pattern). NOT the full HLTB adaptive-window
  logic — Steam Web API tolerates sustained requests at modest pace.
- On 404 / empty response (game has no achievements): return None.
- Schema cache: not a separate field. The age-band TTL on the
  per-game refresh check (same as HLTB) means schema and percentages
  are skipped together when the game's achievement metrics are still
  cached.

## Sync orchestration changes (app/sync.py)

Add a new enrichment phase: `_phase_achievement_stats`. Runs alongside
existing HLTB / SteamSpy / appdetails phases.

- Iterates eligible games (cache check below)
- For each game: fetch global achievement percentages, compute
  completion_rate and cliff_metric, write to DB
- Reuse existing adaptive-pacing helpers
- Track `achievement_fetched` and `achievement_skipped` counters in
  refresh progress
- Surface counters in refresh_status template

Cache eligibility (matches existing HLTB pattern):
- Skip if completion_rate is NOT NULL AND game is in indefinite-TTL
  age band (released 2+ years ago)
- Skip if completion_rate is NOT NULL AND last_refreshed within
  age-appropriate TTL (30 days for <6mo, 180 days for 6mo-2yr)
- Otherwise fetch

Add a new phase for review-data extraction at the same time:
`_phase_review_stats`. This processes the already-fetched Steam review
data (from existing `_phase_enrich` for review summaries) and computes
review_playtime_median + stickiness_ratio. No new API calls — pure
data extraction from data already in memory.

The Steam reviews fetcher (`app/fetchers/steam.py`) currently only
returns aggregate review data. Update it to ALSO return per-review
playtime data (the `author.playtime_at_review` field). The aggregate
fields stay unchanged; we just stop discarding the per-review array.

SteamSpy fetcher already returns median_forever and average_forever.
Compute playtime_median_avg_ratio in `_phase_enrich` after SteamSpy
returns its data.

## Game Detail page surfacing

Add a new section to Game Detail between "External enrichment" and
"This game's taste profile":
Engagement signals (Phase 1a — raw data)
Completion rate:           23.4%
Achievement cliff:         67.2 → 18.5 (48.7 pt drop after top 3)
Review playtime median:    14.2 hours
Sticky reviewers (20+ h):  41% of 1,847 reviews
Playtime median:average:   0.32 (long-tail pattern)

This is intentionally raw / explanatory text rather than a polished
display. Phase 1b will replace this with the categorical badge after
threshold tuning. The raw display is for the user (and us) to see
the actual distribution across the library and make informed
threshold decisions.

If any metric is NULL, display "—" or "no data" for that line.
If ALL metrics are NULL, hide the section entirely (don't show empty).

## Cache reset / force refresh

Force refresh (`?force=true`) should reset achievement and review
stats per the same pattern as HLTB / OpenCritic / SteamSpy — clear
the relevant cache check and re-fetch.

## Done criteria

- All 5 schema columns exist in games table
- New `_phase_achievement_stats` runs cleanly during refresh
- New `_phase_review_stats` runs cleanly during refresh
- Updated Steam reviews fetcher returns per-review playtime data
- SteamSpy fetcher's median_forever/average_forever flow into the new
  ratio metric
- Game Detail page renders the new "Engagement signals" section with
  all 5 metrics where data is available, "no data" where missing
- Force refresh against the real library populates as much as
  available; report:
  - How many games had completion_rate populated (vs games with
    no achievements at all)
  - How many had cliff_metric populated (subset of completion_rate
    games — needs ≥4 post-discard achievements)
  - How many had review_playtime_median (subset with ≥10 reviews)
  - Distribution of values for each metric (min/max/median/mean) so
    we can use this in Phase 1b threshold tuning
- All other pages still 200, no regressions

## Out of scope (deferred to Phase 1b or later)

- Categorical "Sticky / Filters players hard / Average" signal
- Surfacing the signal on Library, Backlog, Shortlist cards
- Threshold tuning
- Per-user achievement progress (different API endpoint, different
  feature, possibly v4+)
- Hour-mapped progression curves (Phase 2)
- Reddit / LLM extraction (Phase 3)
- "Hook point" range estimate display
- Multiplayer-focus / no-defined-endpoint exclusions for stickiness
  display (Phase 1b decision: do we hide stickiness for those games
  or show it anyway?)
