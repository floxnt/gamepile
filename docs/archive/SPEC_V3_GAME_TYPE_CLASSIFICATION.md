> **ARCHIVED — historical reference only.**
> Phase 3 hook-point work was retired in v0.7.0 (see `SPEC_HOOK_RETIREMENT.md`).
> OpenCritic integration is set-aside (see `docs/PROJECT_STATE.md` — search "OpenCritic — possible future re-introduction").
> Do not implement against this spec without first checking `docs/PROJECT_STATE.md` current state.

---

# Game Type Classification Spec

## Purpose

Adds a structured `game_type` field to every game in the library, replacing
the lightweight `compute_game_type()` helper currently in `app/backlog.py`.
The type controls how stickiness metrics (Phase 1a) are displayed and
categorized (Phase 1b), not whether they're computed. Most metrics are
still computed for nearly all game types — only Software fully suppresses
display.

This spec is a Phase 1a sibling, not a successor. It must land before
Phase 1a checkpoint 2 so the metric-display logic is type-aware from
the start.

## Game type taxonomy

Eleven values for `game_type`:

1. **linear** — Single-player with HLTB main, clean campaign structure
2. **multiplayer** — Multi-player categories AND no Single-player category
3. **no_endpoint** — Single-player but no clear endpoint (sandbox, roguelike, infinite)
4. **mixed** — Has both Single-player and multiplayer categories (Halo, Borderlands)
5. **mmo** — User tags or categories include "MMO" / "Massively Multiplayer"
6. **sandbox** — User tags include "Sandbox" with no clear narrative endpoint
7. **beta_playtest** — Title contains beta/playtest/test-server keywords
8. **software** — App is a utility/tool, not a game (3DMark, Wallpaper Engine, etc.)
9. **expansion** — Steam appdetails returns `type: "dlc"` for this appid
10. **early_access** — Steam appdetails marks game as Early Access
11. **unknown** — Detection failed; treat as best-effort `linear` for display

Notes:
- `linear`, `multiplayer`, `no_endpoint`, `mixed` already exist in
  `compute_game_type()`. They retain their semantics.
- The new types (`mmo`, `sandbox`, `beta_playtest`, `software`,
  `expansion`, `early_access`) extend the taxonomy.
- `sandbox` is distinct from `no_endpoint`: a sandbox is open-ended creative
  (Stardew, Minecraft); `no_endpoint` is grindy/roguelite (Hades, Slay the
  Spire). The detection rule below disambiguates.

## Detection rules (in priority order)

Apply rules top-to-bottom; first match wins.

1. **beta_playtest** — Title (case-insensitive) contains any of:
   `beta`, `playtest`, `public test`, `pts`, `test server`,
   `testing branch`, `staging branch`, `unstable`
2. **software** — Steam appdetails categorizes as Utility/Software OR
   appid is in a small curated list (initial: 3DMark 223850, Wallpaper
   Engine 431960, Lossless Scaling 993090, OBS, etc.). Curated list
   stored as a constant; user can manually mark a game as software via
   Game Detail in v3.5+.
3. **expansion** — Steam appdetails returns `type: "dlc"`
4. **early_access** — Steam appdetails returns Early Access category OR
   `release_date.coming_soon: true` with significant playtime in library
5. **mmo** — User tags include "MMO" or "Massively Multiplayer"; OR
   Steam categories include "MMO"
6. **multiplayer** — Multi-player Steam categories AND no Single-player
   category (existing rule from `compute_game_type`)
7. **mixed** — Single-player Steam category AND HLTB main is present
   AND `Co-op` substring (case-insensitive) appears in SteamSpy
   user_tags. Steam-side multiplayer subcategories alone don't qualify:
   Souls-likes (Elden Ring, DS3, Sekiro) carry every Co-op/PvP/Online
   subcategory but aren't genuinely Mixed; pre-cross-plat dedicated MP
   games (Borderlands 2, L4D2, Destiny 2) have identical Steam-side
   profiles to Souls-likes. The SteamSpy `Co-op` user_tag empirically
   discriminates the two (5/5 mixed-intent vs 0/6 linear-intent across
   the verified sample). Niche indies with empty user_tags fall through
   to linear by default — defensive direction.
8. **sandbox** — User tags include "Sandbox" AND HLTB main is present
   AND completionist:main ratio < 7x
9. **no_endpoint** — Roguelike/Roguelite tag, OR completionist:main > 7x,
   OR Single-player with HLTB main missing AND has Open World/Sandbox
   tag (existing logic from `compute_game_type`)
10. **linear** — Has Single-player AND HLTB main AND not classified above
11. **unknown** — None of the above (extremely rare; e.g., apps with no
    metadata at all)

The rule ordering matters: beta detection runs before everything else
because a "Counter-Strike Beta" should be classified as beta even
though it'd otherwise fit multiplayer. Software runs second because
some software has weird Steam categorization.

## Schema additions

- `games.game_type TEXT` — nullable, computed during refresh
- `games.game_type_manual BOOLEAN DEFAULT 0` — user manually overrode

`game_type_manual = 1` means refresh inference doesn't override the
user's choice. Same pattern as `manually_set` for status.

## Migration

Via try/except ALTER TABLE pattern. Backfill: re-run classification
on all rows where `game_type_manual = 0` during the next refresh.
No data loss — the existing `compute_game_type()` callers keep working
because they compute on the fly; the new schema column is the
persistent cached version.

## Detection implementation

New module: `app/game_type.py` (extracted from `app/backlog.py`).

- `classify_game(game) -> str` — returns the game_type string per
  the rules above
- `SOFTWARE_APPID_LIST: tuple[int, ...]` — curated list, expandable
- `BETA_KEYWORDS: tuple[str, ...]` — title-pattern matchers
- `is_forever_game(game) -> bool` — kept for backward compatibility,
  now derived from game_type (returns true if type in
  `{multiplayer, mmo, no_endpoint, sandbox}`)

`app/backlog.py` imports from the new module. Existing
`compute_game_type()` shim retained as a deprecation alias that
calls `classify_game()` — Library template doesn't need changes.

## Sync orchestration

Add a step to `_phase_enrich`: after Steam appdetails, HLTB,
SteamSpy, and review data are populated, call `classify_game()`
and persist `game_type` if `game_type_manual = 0`.

Cache: re-run classification on every refresh. The function is pure
and cheap; no need to cache. (Alternative: only re-run if any input
field changed since last refresh. Marginal optimization, skip
unless refresh time becomes a concern.)

## Display logic per game type

Centralized helper: `engagement_display_rules(game_type) -> dict`
returns:
- `show_completion_rate: bool`
- `show_cliff_metric: bool`
- `show_review_playtime: bool`
- `show_stickiness_ratio: bool`
- `show_playtime_ratio: bool`
- `categorical_badge_eligible: bool` — Phase 1b uses this
- `caveat_text: Optional[str]` — footnote shown if metrics displayed

| Game type     | completion | cliff | playtime | sticky | ratio | badge | caveat                                        |
|---------------|------------|-------|----------|--------|-------|-------|-----------------------------------------------|
| linear        | ✓          | ✓     | ✓        | ✓      | ✓     | ✓     | —                                             |
| mixed         | ✓          | ✓     | ✓        | ✓      | ✓     | ✓     | —                                             |
| multiplayer   | ✗          | ✗     | ✓        | ✓      | ✓     | ✓     | —                                             |
| mmo           | ✗          | ✗     | ✓        | ✓      | ✓     | ✓     | —                                             |
| no_endpoint   | ✗          | ✗     | ✓        | ✓      | ✓     | ✓     | —                                             |
| sandbox       | ✗          | ✗     | ✓        | ✓      | ✓     | ✓     | —                                             |
| beta_playtest | ~          | ~     | ✓        | ✓      | ✓     | ✗     | "Data may be unstable for beta/playtest"      |
| early_access  | ~          | ~     | ✓        | ✓      | ✓     | ✗     | "Data still settling for Early Access"        |
| expansion     | ✓          | ✓     | ✓        | ✓      | ✓     | ✓     | "Treated as standalone game; data may be limited" |
| software      | ✗          | ✗     | ✗        | ✗      | ✗     | ✗     | (entire engagement section hidden)            |
| unknown       | ✓          | ✓     | ✓        | ✓      | ✓     | ✗     | "Type detection failed; signals best-effort"  |

(`~` = shown if data is present, but with the caveat displayed alongside.)

## Game Detail page changes

The "Engagement signals" section (introduced in Phase 1a) gets a header
that includes the game type and any caveat:Engagement signalsType: Linear · Story-drivenCompletion rate:           23.4%
Achievement cliff:         48.7 pt drop
Review playtime median:    14.2 hours
Sticky reviewers (50%+):   41% of 1,847 reviews
Playtime median:average:   0.32 (long-tail pattern)

For software:Engagement signalsType: Software(Engagement signals don't apply to software/utilities.)

For beta_playtest:Engagement signalsType: Beta / Playtest
Note: Data may be unstable for beta/playtest titles.Review playtime median:    8.4 hours (29 reviews)
Sticky reviewers (50%+):   12% of 29 reviews
Playtime median:average:   0.74 (even engagement)

The display helper `engagement_display_rules(game_type)` decides
which lines to render.

## Library Type column

The existing Library Type column already shows the four-way classification
(linear/multiplayer/no_endpoint/mixed) via badges. After this work, it
shows the full taxonomy with appropriate color coding:

- linear, mixed → green tint
- multiplayer, mmo, sandbox, no_endpoint → blue/teal tint
- beta_playtest, early_access → amber tint
- software → gray
- expansion → purple tint
- unknown → muted

CSS additions: new `.gt-mmo`, `.gt-sandbox`, `.gt-beta_playtest`,
`.gt-software`, `.gt-expansion`, `.gt-early_access`, `.gt-unknown`
classes paralleling the existing four.

## Manual override on Game Detail

Add a "Game type" line to the status bar section (alongside Status):Status: [In Progress ▾]   Type: [Linear ▾]   ●  Manually set   ●  Last refreshed: 3 days ago

Selecting from the Type dropdown sets `game_type_manual = 1`. Available
options: all 11 types in dropdown.

A "Reset to auto-detected" button appears next to it when
`game_type_manual = 1`, parallel to the status reset.

## Out of scope

- Per-user Software curated list expansion (Game Detail mark-as-software
  is v3.5+; for now, curated constant is the only path)
- Sub-types within categories (e.g., "JRPG" vs "WRPG" vs "ARPG" within
  linear)
- Multi-tag games (a game that's BOTH multiplayer AND has linear
  campaign — already handled as `mixed`)
- Backlog re-bucketing based on new types (the Backlog view stays the
  same; type-classification affects display, not eligibility)

## Done criteria

- `games.game_type` and `games.game_type_manual` columns exist
- `classify_game()` returns one of the 11 values for every game
- Refresh populates game_type for all rows where game_type_manual = 0
- Library Type column shows all 11 types with appropriate styling
- Game Detail status bar shows Type with dropdown override
- Game Detail Engagement signals section displays per
  `engagement_display_rules(game_type)` with appropriate caveats
- Software type fully suppresses the engagement signals section
- All existing pages still 200; existing forever-game detection still
  works (`is_forever_game` derives from `game_type`)
- Force-refresh against real library and report distribution: how many
  games per type
