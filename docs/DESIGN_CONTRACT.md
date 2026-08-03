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

Locked column set as of v0.8.7, in order:

1. Title (sortable, default sort ascending)
2. Type (game-type badge: linear / multiplayer / no_endpoint — see
   `app/game_type.py` for the classification logic)
3. Tags (from SteamSpy user_tags, truncated with "+N more" overflow;
   sortable alphabetically by first tag as of v0.8.7)
4. HLTB Main
5. HLTB Completionist
6. Playtime
7. Steam Reviews (combined "93% (10k)" format — percent followed by
   review count in parentheses; sort is by percent. Fallback to just
   the percent when count is missing, "—" when both are missing.)
8. Metacritic
9. Avg. Achievement % (median per-achievement global unlock %;
   internal sort key `median_unlock` retained for URL backcompat with
   pre-rename bookmarks)
10. My Achievement % (user's own unlock % for the game, via
    GetPlayerAchievements)

Removed in v0.8.7 (test-group feedback): Status column (badge was
inaccurate and double-tracked with Shortlist's status display) +
Developer column (no one uses GamePile as a credits database).

Removed in v0.9.5: Edit column and inline edit form. All editing
(status, manual hours, notes) consolidated to Game Detail page.

Removed in v3: OpenCritic. Column dropped after the official API moved
behind a paid RapidAPI gateway incompatible with the friend-shareable
distribution model. See PROJECT_STATE.md "OpenCritic — possible future
re-introduction" for context.

Do NOT show AppID — internal use only.
Do NOT show genres column when tags column is present (tags supersede genres).
Do NOT show a separate Steam Tier column — % positive plus review count already
conveys the same information.
Do NOT reintroduce Status or Developer as a displayed column without
revisiting the v0.8.7 decision; both removals were locked from
test-group feedback, not provisional.

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

- Top-level nav: Shortlist, Backlog, Library, Dashboard
- Settings sits apart from those four, in the right-hand nav chrome —
  it's configuration, not a destination you browse
- Game Detail is reached by clicking through from a list; it is
  deliberately not a nav item
- Do not add navigation items speculatively
- "Refresh Library" + "Force" stay in the top-right header position

## Source of truth rule

When data conflicts, the order of authority is:

1. Manual user override (anything `manually_set = true`)
2. Steam-owned facts (playtime, ownership, last_played)
3. External enrichment (HLTB, Metacritic, SteamSpy)
4. Inferred or guessed data

Never overwrite higher-precedence data with lower-precedence data
during refresh.

## Data integrity

- The app must function if HLTB or SteamSpy fails — these
  are enrichment, not core dependencies
- Steam API failures during refresh should be logged and skipped,
  not crash the refresh
- Manual overrides survive refresh; refresh never resets user state

## Testing against live data

When verifying behavior against the real database, never UPDATE production
data for testing purposes. Use one of:

- A temp copy under a `GAMEPILE_DATA_DIR` override (see below)
- An explicit transaction wrapped around test mutations, with rollback
- A read-only query that simulates the condition rather than mutating to
  create it

Test mutations to live data, even with intent to restore, can lose precision
on timestamps and other fields. Recovery from approximate values is possible
but never identical to the original.

### `GAMEPILE_DATA_DIR`

Set this env var to a directory and GamePile resolves its entire data
directory there — database, logs, everything — instead of the
platformdirs location. Blank or unset means normal behavior.

```
mkdir -p /tmp/gp-test
cp ~/.local/share/gamepile/gamepile.db /tmp/gp-test/
GAMEPILE_DATA_DIR=/tmp/gp-test uv run uvicorn app.main:app --port 8801
```

**Always verify the override actually took effect before writing
anything.** This document recommended an "env override" for a long time
before one existed, and a session that assumed it worked ran `init_db()`
against the real database six times, corrupting user ratings. Print the
resolved path and assert it first:

```python
import app.config as c
assert str(c.DB_PATH).startswith("/tmp/"), f"ISOLATION FAILED: {c.DB_PATH}"
```

The test suites go further and don't trust config resolution at all —
`tests/test_migrations.py` and `tests/test_backup.py` patch
`app.database.DB_PATH` directly and hard-assert the path is under the
system temp dir before opening a connection. Prefer that pattern for
anything automated; an env var that silently does nothing is exactly
how the original incident happened.

## Scope guardrails (what NOT to add without explicit request)

- No per-user achievement tracking dashboard
  — a single per-user unlock-% display column ("My Achievement %")
    landed in v0.8.7 as an honest stat; a dedicated achievement-
    tracking surface / dashboard / drill-down view is still out of
    scope
- No hook-point or "stickiness" inference features
  — the v3 hook-point pipeline (Phase 1a/1b/1c categorical badges)
    was retired in v0.7.0 (see `SPEC_HOOK_RETIREMENT.md`). The
    compute functions and DB columns are preserved dormant for a
    possible future revisit, but the live UI does NOT surface
    inferred engagement signals. Reintroducing the badges requires
    an explicit decision and a new round, not a quiet revival.
- No LLM features in the runtime app
  — the v3 Phase 3 hook-point summarization exception is dead
    alongside the rest of the hook-point work; LLM features are
    out of scope without qualifier
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
