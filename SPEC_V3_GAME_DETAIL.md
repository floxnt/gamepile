markdown
# Per-Game Detail View Spec

## Purpose

A drill-down page for any single game, accessible from anywhere a game appears
in the system. Functions as: the "user can correct bad data" surface (manual
overrides), the "what is the system doing with this game" surface (affinity
contributions, pick history), and the "personal annotations" surface (notes,
rating).

Read-mostly. The few mutable fields are explicitly user-overridable data.

## Navigation

Route: `/games/{appid}`

Click-throughs added:
- Library: clicking a game's title cell → game detail
- Backlog: clicking a game's title in any row → game detail
- Shortlist: clicking a game's title on a recommendation card → game detail
- Recent picks: clicking a game's title → game detail

The "⋯" overflow menu on cards/rows gets a new "View details" item that also
navigates here. Overflow always present means the link path is robust even if
title-click feels off in some contexts.

## Layout
[Page header: Game title + cover art (large, ~200px)]
[Status bar: current state + manual override controls]
[Game data section: Steam facts + HLTB + critic scores + description]
[Personal section: notes + rating + manual hours played]
[Affinity contributions: how this game affects your taste]
[Pick history: every time this game has been picked]

## Section 1 — Header

- Cover art ~200px wide (poster orientation), left-aligned
- Title (large, bold) + developer + release year + length category badge
- Compact: takes the top ~250px of the page

## Section 2 — Status bar

A row showing current state with manual override options:
Status: [In Progress ▾]  ●  Manually set  ●  Last refreshed: 3 days ago

The status dropdown is the actual editable status field. Selecting from it
sets `manually_set = true` and updates `game_state.status`. The available
options follow the state machine doc transitions from the current status.

"Manually set" indicator shows if `manually_set = true`. Hover tooltip:
"You explicitly set this status. Auto-inference won't override it."

A small "Reset to auto-inferred" button appears when manually_set = true,
which clears `manually_set` and re-runs inference logic. Hidden otherwise.

## Section 3 — Game data (read-only)

Two-column layout:

**Left column — Steam facts:**
- Steam playtime (lifetime, formatted as Xh Ym)
- Last played (formatted as "3 days ago" / "2 months ago" / "Never")
- Installed status (if available)
- Genres (comma-separated from Steam)
- Top 10 tags (from SteamSpy user_tags)

**Right column — External enrichment:**
- HLTB Main / Main+Sides / Completionist (with timestamps last_refreshed)
- Metacritic critic score (with link to Metacritic page if we ever wire that up)
- Steam review summary (% positive + total count + tier label)
- (OpenCritic was removed in v3 — see PROJECT_STATE.md.)

Below both columns: full Steam description (the longer detailed_description if
we have it; else short_description used elsewhere). Untruncated, in a readable
prose block.

## Section 4 — Personal

Three editable fields stacked vertically:

**Personal notes:**
Notes
[multi-line textarea, ~4 rows]
[Save] (only enabled when content changed)

**Personal rating:**
Your rating
[★ ★ ★ ☆ ☆]  3/5
[Clear]

Hover-and-click stars to set. Click again on the same star to confirm. Stars
saved to `game_state.personal_rating`. "Clear" sets to NULL.

**Hours played (manual override):**
Hours played (your estimate)
[12] hours
[Save]

Integer input, saves to `game_state.hours_played_manual`. When set, this value
overrides Steam playtime in the recommender's remaining-time calculation.
Tooltip: "Use this if your Steam playtime is wrong (e.g., idled, played
elsewhere, didn't track properly)."

## Section 5 — Affinity contributions

Read-only pill display showing this game's affinity contributions:
How this game affects your taste
Genres
[Action +0.5]  [RPG +0.5]
Tags
[Soulslike +0.5]  [Atmospheric +0.5]  [Story Rich +0.5]
Developers
[FromSoftware +0.5]

These pills show what *this specific game's labels* contribute to your overall
affinities. Helps user understand "why does the system think I like Soulslike?"
— click into Dashboard to see total accumulated affinities.

If no affinity data exists for this game (never picked, never gave feedback):

> No taste data yet. Pick this game from Shortlist and tell us how it went
> to start tracking.

## Section 6 — Pick history

Scrollable vertical list, all rows shown, no pagination:
Pick history (8)
─────────────────
May 3, 2025 · Continue something · 90 min window
→ Played and finished. Rating: 5/5. Genre fit: 5/5.
April 18, 2025 · I only have tonight · 120 min window
→ Did not play (changed mood). Picked instead: Hades.
March 22, 2025 · Surprise me · no window
→ Played, still going. Rating: 4/5.
[...]

Each row: date, mode, time window (or "no window" for surprise/comfort),
outcome, optional rating, optional retroactive-pick info.

If zero picks: hide the section entirely (no empty header).

Newest first.

## Schema additions

- `game_state.personal_notes TEXT` — nullable
- `game_state.personal_rating INTEGER` — nullable, 1-5
- (existing) `game_state.hours_played_manual INTEGER` — already exists, just exposing UI

All new columns via try/except ALTER TABLE.

## New route: `app/routes/game_detail.py`

Endpoints:
- `GET /games/{appid}` — full page render
- `POST /games/{appid}/notes` — save personal notes (HTMX swap)
- `POST /games/{appid}/rating` — save personal rating (HTMX swap)
- `POST /games/{appid}/hours_played_manual` — save manual hours (HTMX swap)
- `POST /games/{appid}/status` — change status (HTMX swap, sets manually_set=true)
- `POST /games/{appid}/reset_status` — clear manually_set, re-run inference

All POST endpoints return the updated section partial for HTMX swap.

## New domain helpers (app/game_detail.py)

Pure functions:
- `compute_per_game_affinity_contribution(game, affinity_table) -> list[Pill]` — what this game's labels contribute
- `format_pick_history_row(pick) -> dict` — render-friendly version of a pick row
- `valid_status_transitions(current_status) -> list[GameStatus]` — for the dropdown options, sourced from state machine doc

## New templates

- `app/templates/game_detail.html` — extends base.html
- `app/templates/partials/game_detail_header.html` — section 1
- `app/templates/partials/game_detail_status_bar.html` — section 2 (HTMX swap target)
- `app/templates/partials/game_detail_personal.html` — sections 4 fields (each separately swappable)
- `app/templates/partials/game_detail_pick_history_row.html` — single pick row
- `app/templates/partials/affinity_pill.html` — REUSE from Dashboard

## Linkability changes (other templates)

Make game titles clickable across:
- `app/templates/library.html` — title cell wrapped in `<a href="/games/{{ game.appid }}">`
- `app/templates/partials/backlog_row.html` — title wrapped
- `app/templates/partials/game_card.html` — title wrapped
- `app/templates/partials/recent_pick_card.html` — title wrapped (if present)

Add "View details" to all overflow menus (`backlog_overflow_menu.html` and any
shortlist-card overflow menu).

## CSS additions

New rules under `/* === Game detail === */`:
- `.game-detail-header` — flex layout, cover + title block
- `.game-detail-cover` — ~200px poster
- `.game-detail-title-block` — title + meta
- `.game-detail-status-bar` — flex row with status, indicator, refresh time
- `.game-detail-data` — two-column grid
- `.game-detail-data-column` — column styling
- `.game-detail-personal` — vertical stack of editable fields
- `.game-detail-rating-stars` — clickable stars
- `.game-detail-affinity` — wrapper
- `.game-detail-pick-history` — list container
- `.game-detail-pick-row` — individual pick row

## Done criteria

- `/games/{appid}` renders for any game in the library
- All editable fields save correctly via HTMX swap
- Status dropdown only offers valid transitions per state machine
- "Reset to auto-inferred" works and clears manually_set
- Personal notes / rating / hours played save and persist
- Affinity contributions render correctly with proper signs
- Pick history shows all picks for this game, sorted newest first
- All other pages now have clickable titles + "View details" overflow items
- No regressions on existing pages

## Out of scope (deferred)

- Cover art high-res / multiple screenshots — using existing CDN URL
- DLC handling
- Multiple manual hours entries (e.g., "60 hours total but 25 of those were on Steam Deck")
- Note formatting (rich text, markdown) — plain text only
- Per-pick deletion (you can't undo a pick from history)
- Achievement-level data (deferred to v3 hook-point work)
