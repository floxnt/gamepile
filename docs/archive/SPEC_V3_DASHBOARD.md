> **ARCHIVED — historical reference only.**
> Phase 3 hook-point work was retired in v0.7.0 (see `SPEC_HOOK_RETIREMENT.md`).
> OpenCritic integration is set-aside (see `docs/PROJECT_STATE.md` — search "OpenCritic — possible future re-introduction").
> Do not implement against this spec without first checking `docs/PROJECT_STATE.md` current state.

---

Locked. Option B with negatives section confirmed.
Time-since-last-pick callout — small and unobtrusive:
A single line under the top-line stats band, only when it's been ≥ 7 days since the last pick. Hidden when fresh.
[ 460 games in backlog ]  [ 4.2 picks/week ]  [ 3 finished this month ]
─────────────────────────────────────────────────────────────────────
12 days since your last pick. [Pick something →]
Just text + a quiet link to Shortlist. Threshold 7 days because anything shorter feels nagging. After 7 days the user has objectively stopped using the app, so a gentle nudge is fair.
Hidden entirely if:

Less than 7 days since last pick
User has zero picks ever (no baseline to compare against; show empty-state copy elsewhere)

The link goes to /shortlist with mode pre-selected as "Continue something" if the user has any in_progress games, else "I only have tonight." Same default-mode logic the home page already uses — keep behavior consistent.
Confirming nothing else on the Dashboard:
Locking it down at: top-line stats band, time-since-last-pick (conditional), affinity profile. Three sections, vertical stack, no other content. Mode usage breakdown / genre diversity / hours-per-completion trends all explicitly deferred to v3.5+ if they ever earn their way in.
Good restraint. Dashboards crowd fast.

Drafting SPEC_V3_DASHBOARD.md now. Posting it below — save to project root and commit before sending to Claude Code, same pattern as the Backlog spec.
markdown# Dashboard View Spec

## Purpose

A top-level page in GamePile providing aggregate stats and affinity profile.
Read-only, no actions. Functions as the "is the system working" surface —
backlog progress at a glance, taste profile transparency, and a gentle nudge
when engagement lapses.

## Navigation

Add **Dashboard** as a top-level nav item between Library and the Refresh
button area. New nav order:

`GamePile · Shortlist · Backlog · Library · Dashboard` (Refresh Library / Force on the right)

Page route: `/dashboard`

## Layout

Three sections, vertical stack, generous spacing:
[Page header: "Dashboard"]
[Top-line stats band — 3 horizontal cards]
[Time-since-last-pick callout — conditional, only when ≥7 days]
[Affinity profile section]

No other content. Crowding is forbidden. Future additions (mode usage,
diversity stats, completion trends) deferred to v3.5+.

## Section 1 — Top-line stats

Three horizontal cards in a row, equal width:

**Card 1: Backlog count**
- Headline: total count of default-visible Backlog games (excludes Forever
  games unless toggle is on globally, but for Dashboard purposes always
  excludes Forever)
- Subtitle: "games in backlog"
- Tooltip on hover: "Games eligible for active progression tracking. Excludes finished, dropped (strong), not interested, blacklisted, and forever-games."

**Card 2: Picks per week**
- Headline: float, one decimal place (e.g., `4.2 picks/week`)
- Subtitle: "rolling 7-day, backlog-eligible only"
- Calculation:
  - Look at `pick_history` rows where `picked_at >= now - 7 days`
  - For each pick row, check the game's status AT TIME OF PICK was one of `never_played`, `played_unclassified`, `in_progress` AND game was not classified as Forever
  - Divide count by 7, multiply by 7 (i.e., raw weekly count, but expressed as decimal)
  - Actually: simpler — just count picks in the last 7 days, display as `X picks/week`
  - Zero displays as `0 picks/week`
- Tooltip: "Counts only picks of games eligible for backlog tracking. Comfort Pick mode replays of finished games don't count."

**Card 3: Finished this month**
- Headline: integer count
- Subtitle: "calendar month, started [Month Day]"
- Calculation:
  - Count games where `game_state.status = 'finished'` AND `game_state.updated_at >= start of current calendar month`
  - If a game was finished, then unfinished, then re-finished within the same month: count once (deduplicate by appid)
  - Zero displays as `0 finished this month`
- Tooltip: "Games marked finished since the 1st of [Month]."

**Visual treatment:**
- Each card: muted card background (matches existing dark theme), large number in primary text color, subtitle in muted gray
- Cards equal width, single row on desktop
- On narrow viewports (mobile not supported but be defensive), wrap to vertical stack
- No icons on the cards — just numbers and labels

## Section 2 — Time-since-last-pick callout (conditional)

Renders only when:
- User has at least 1 pick in `pick_history` (i.e., not a brand-new user)
- Days since most recent pick ≥ 7

Hidden in all other cases.

Format: single muted line below the stats band, with a quiet inline link.
12 days since your last pick. [Pick something →]

The link points to `/shortlist` with mode preselected:
- If user has any games with `status = in_progress` (excluding Forever): mode = `continue_something`
- Else: mode = `i_only_have_tonight`

Implemented as `?mode=continue_something` query param on the link.

Visual: small text size (smaller than body, larger than caption), muted color, link in primary accent color.

## Section 3 — Affinity profile

Pill-based display of weighted affinities across three categories.
Your taste profile
Genres
[Action +3.4]  [RPG +2.8]  [Indie +1.5]  [Adventure +0.9]  [Strategy +0.4]
Tags
[Soulslike +4.1]  [Atmospheric +2.2]  [Story Rich +1.8]  [Roguelite +1.1]  [Open World +0.6]
Developers
[FromSoftware +5.2]  [Larian Studios +2.1]  [Obsidian +0.4]
Cooler on:
[Multiplayer −1.8]  [Casual −1.2]  [Battle Royale −1.0]

**Per-category rules:**
- Show top 5 entries per category by weight, descending
- Skip a category entirely if zero entries (don't show empty header)
- Within each category, exclude entries with weight between -0.5 and +0.5 (too neutral to be meaningful)
- Pill format: `[label sign±weight]` where sign is + or − and weight is rounded to one decimal

**Cooler on section:**
- Combines negative-weight entries across all three categories (genre/tag/dev), sorted ascending by weight (most negative first)
- Show top 3
- Only render section if at least one entry below -1.0 (don't surface barely-negative noise)

**Pill styling:**
- Positive: existing primary accent color or neutral dark surface, light text
- Negative: muted amber or red border, same body
- Pill with `pick_count < 3` (low confidence affinity): rendered with reduced opacity (e.g., 60%) to signal "we don't have much data on this yet"
- All pills non-interactive in v3 (clicking doesn't filter or do anything; v3.5+ may add click-to-filter-Backlog)

**Empty state (no affinity data):**
- If `affinity` table is empty or all entries are between ±0.5: show placeholder
  
  > Your taste profile builds as you mark games. Pick something from Shortlist and tell us how it went to start.

- Don't render any category headers in empty state — single line of placeholder copy is enough

## Schema additions

Two new nullable columns on `pick_history`, captured at insert time by
`mark_picked` and used by the Dashboard's picks-per-week eligibility filter:

- `status_at_pick TEXT` — the game's `game_state.status` at the moment of
  the pick (read before `mark_picked` overwrites with `in_progress`)
- `was_forever_at_pick BOOLEAN` — `is_forever_game(game)` at the moment of
  the pick

Both via try/except `ALTER TABLE`. Rows inserted before this migration
have NULL on both columns. The picks-per-week filter applies a
charitable NULL-as-include rule for those rows — losing real history is
worse than approximating, and back-inferring from current state would be
misleading (the user has acted on those games since). No backfill.

Going forward, `is_backlog_pick(pick)` is the single source of truth for
"was this pick a backlog progression event": NULL → include; was-forever
→ exclude; status in {`never_played`, `played_unclassified`, `in_progress`}
→ include; everything else (finished / dropped / not_interested) → exclude.

The remaining sections still rely on existing columns:
- `game_state.status` and `game_state.updated_at` for finished this month
- `affinity` table for taste profile

## New route: `app/routes/dashboard.py`

Single endpoint:
- `GET /dashboard` — full page render
- No POST endpoints — Dashboard is read-only

## New domain helpers in `app/dashboard.py` (or wherever similar lives)

Pure functions, no DB/FastAPI imports, testable:

- `compute_backlog_count(games_with_state) -> int`
- `compute_picks_per_week(pick_history, now) -> int` — simple count of picks in last 7 days where the game was backlog-eligible at pick time
- `compute_finished_this_month(game_states, now) -> int` — count distinct appids with `status = 'finished'` and `updated_at >= start_of_month(now)`
- `compute_days_since_last_pick(pick_history, now) -> Optional[int]` — None if no picks ever
- `build_affinity_profile(affinity_rows) -> AffinityProfile` — dataclass with `genres: list[Pill]`, `tags: list[Pill]`, `developers: list[Pill]`, `negatives: list[Pill]`

`Pill` dataclass: `label: str`, `weight: float`, `pick_count: int`, `low_confidence: bool` (derived from pick_count < 3), `is_negative: bool`.

## New templates

- `app/templates/dashboard.html` — extends base.html, single-page layout
- `app/templates/partials/dashboard_stat_card.html` — single stat card (used 3 times for the top-line band)
- `app/templates/partials/affinity_pill.html` — single pill (used many times)

## CSS additions

New rules under `/* --- Dashboard --- */`:
- `.dashboard-stats` — flex row container
- `.dashboard-stat-card` — individual stat card styling
- `.dashboard-stat-headline` — large number
- `.dashboard-stat-subtitle` — muted label
- `.dashboard-callout` — time-since-last-pick line
- `.affinity-section` — wrapper for the profile
- `.affinity-category` — per-category block
- `.affinity-pills` — flex-wrap pill container
- `.affinity-pill` — base pill style
- `.affinity-pill--negative` — variant for negative affinities
- `.affinity-pill--low-confidence` — opacity-reduced variant
- `.affinity-empty` — empty-state placeholder copy

## Files NOT touched (preserves existing v1/v2/v2.5/v3 work)

- All other routes, recommender, sync, fetchers, models — unchanged
- Backlog, Library, Shortlist, recent picks views — unchanged
- Schema — unchanged

## Done criteria

- `/dashboard` renders without errors against the real library
- All three stat cards show correct values (verify by manual SQL count vs displayed number)
- Picks-per-week correctly excludes Comfort Pick replays of finished games
- Finished-this-month correctly handles deduplication of re-finished games within the same month
- Time-since-last-pick callout appears at exactly the right threshold (7+ days) and hides correctly when under
- Affinity profile renders with positive pills, negative pills, and low-confidence styling
- Empty affinity state renders the placeholder copy correctly
- Nav link to /dashboard added to base.html in correct position
- All existing pages render unchanged
- No new schema migrations needed (verify by running `init_db()` on a fresh DB)

## Out of scope for this Dashboard (deferred to v3.5+)

- Mode usage breakdown ("you use Continue Something 60% of the time")
- Genre diversity / variety stats
- Average hours-per-completion trend
- Backlog burndown over time (requires schema work for snapshots)
- Click-pill-to-filter-Backlog interaction
- Manual game tracking (for non-Steam games)
- Per-week / per-month historical comparison ("you finished 5 games last month, 3 this month")
- Dashboard customization / hideable sections
