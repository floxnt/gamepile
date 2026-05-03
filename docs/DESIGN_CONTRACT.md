# Design Contract — GamePile

This document defines the design rules for GamePile. Any AI coding agent
(Claude Code, Codex, or otherwise) must read this file before making changes
and obey the constraints below. Do not "improve" the design unless the user
explicitly asks for a redesign.

## Product framing

GamePile is a Steam backlog manager. Its purpose is helping the user
make progress through games they own. The Shortlist feature (the
"suggest 5 games" recommender) is one tool inside the app, not the
app's defining function.

When in doubt, prioritize backlog management UX over picker UX.

## Naming

- **GamePile** — the app
- **Shortlist** — the recommender feature/tab (formerly "Tonight's Pick" / "Game Roulette")
- The Shortlist tab label in the top nav is "Shortlist"
- Avoid "roulette" as a feature name in user-facing copy — the recommender
  is weighted and deliberate, not random

## Mode labels: user-intent, not algorithm description

The five modes inside the Shortlist tab are:

1. **I only have tonight** — fits-in-window short-term picks
2. **Continue something** — surfaces in-progress games, especially ones near completion
3. **Comfort pick** — favors high-playtime games the user has clearly enjoyed
4. **Start something new** — never-played games (and effectively-untouched played_unclassified games) worth committing to
5. **Surprise me** — randomized with quality bias

Do not rename these to time-window labels (Short-term, Long-term, Both, etc.).
The labels describe user intent, not algorithm internals.

## Card layout

- Cards are horizontal (cover art left, content right), single column,
  full content width
- Cover art is poster-style (vertical orientation), ~120px wide
- Card height is roughly equal to cover art height — no large empty
  space below buttons
- Action buttons live inline with the stats row (right-aligned), not
  in a separate row below
- Use the "⋯" overflow menu pattern for secondary actions; primary
  actions are visible inline

## Library view columns

Default columns, in order:

1. Title (sortable, default sort ascending)
2. Status (badge, color-coded by state)
3. Tags (from SteamSpy user_tags, truncated with "+N more" overflow)
4. Developer
5. HLTB Main
6. HLTB Compl.
7. Playtime
8. Steam % (positive percentage)
9. Steam Reviews (total review count)
10. Metacritic (critic score)
11. OpenCritic (drop column entirely if integration remains broken in v3)

Do NOT show AppID — internal use only.
Do NOT show genres column when tags column is present (tags supersede genres).
Do NOT show a separate Steam Tier column — % positive plus review count already
conveys the same information.

## Color and visual hierarchy

- Use the existing dark theme; do not introduce new accent colors
- Primary action buttons use the existing purple/violet accent
- Status badges use existing color codes (green for finishable,
  blue for in progress, amber for warnings)
- Do not change global typography or spacing without explicit request
- Native form elements (<select>, <input>, <textarea>) must be styled
  to match the dark theme — never use browser defaults. White-on-white
  selects are a recurring regression to watch for.

## Navigation

- Top-level nav: Shortlist, Library
  (and future: Backlog, Dashboard, Game Detail, Settings — added when
  implemented, not before)
- Do not add navigation items speculatively
- "Refresh Library" + "Force" stay in the top-right header position

## Source of truth rule

When data conflicts, the order of authority is:

1. Manual user override (anything `manually_set = true`)
2. Steam-owned facts (playtime, ownership, last_played)
3. External enrichment (HLTB, OpenCritic, Metacritic, SteamSpy)
4. Inferred or guessed data

Never overwrite higher-precedence data with lower-precedence data
during refresh.

## Data integrity

- The app must function if HLTB, OpenCritic, or SteamSpy fails — these
  are enrichment, not core dependencies
- Steam API failures during refresh should be logged and skipped,
  not crash the refresh
- Manual overrides survive refresh; refresh never resets user state

## Testing against live data

When verifying behavior against the real database, never UPDATE production
data for testing purposes. Use one of:

- A temp copy: `cp gamepile.db gamepile-test.db` and point the app at it via
  env override
- An explicit transaction wrapped around test mutations, with rollback
- A read-only query that simulates the condition rather than mutating to
  create it

Test mutations to live data, even with intent to restore, can lose precision
on timestamps and other fields. Recovery from approximate values is possible
but never identical to the original.

## Scope guardrails (what NOT to add without explicit request)

- No achievement tracking (i.e., per-user achievement progress dashboards)
  — using global achievement % as a stickiness signal in v3 is different
  and IS in scope
- No LLM features in the runtime app
  — one bounded exception: optional v3 hook-point summarization,
    cacheable per game
- No scheduled background jobs (refresh is manual)
- No cross-platform game tracking beyond Steam
- No mobile UI
- No multi-user support
- No analytics or telemetry
- No social/sharing features (do not become Backloggd; the differentiator
  is action-oriented recommendation, not history logging)

## When in doubt

- Preserve existing layout, spacing, color, and behavior
- Implement only the requested change
- If a request seems to require redesigning something not asked
  about, stop and ask the user before proceeding
