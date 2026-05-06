markdown

# v3 Hook-Point Phase 1b Spec

## Purpose

Phase 1b defines the categorical "stickiness" signal that combines the four
live Phase 1a metrics into a user-facing badge. Phase 1a populated raw data;
Phase 1b interprets it.

The signal has four states: **Sticky**, **Average**, **Filters players hard**,
**Insufficient data**.

## Metrics in scope

Four of the five Phase 1a metrics. `playtime_median_avg_ratio` is excluded
(structurally dead — SteamSpy free tier returns zeros).

1. **cliff_metric** — percentage-point drop at the steepest cliff
2. **stickiness_ratio** — fraction of reviewers crossing 0.5x HLTB main
   (or 5h flat fallback when HLTB null)
3. **completion_rate** — only when `completion_rate_confidence = high`
4. **review_playtime_median** — informational only, does not contribute
   to the categorical signal

## Per-metric threshold rules

Each metric contributes one of: `sticky`, `filters_hard`, `neutral`, or
`no_data` to the combined signal.

### cliff_metric

- `>= 20.0` → `filters_hard`
- `< 20.0` → `neutral`
- NULL → `no_data`

Note: cliff has no "sticky" contribution. A small cliff isn't a positive
signal on its own; it's just absence of a negative signal.

### stickiness_ratio

- `>= 0.90` → `sticky`
- `<= 0.50` → `filters_hard`
- in between → `neutral`
- NULL → `no_data`

### completion_rate (high-confidence only)

Only contributes when `completion_rate_confidence = 'high'`. When
confidence is `low` or NULL, the signal contribution is `no_data`
regardless of the completion_rate value.

When confidence is high:
- `>= 0.15` → `sticky`
- `<= 0.03` → `filters_hard`
- in between → `neutral`

### review_playtime_median

Informational only. Display on Game Detail; does not affect categorical
signal.

## Combining contributions

The badge is determined by counting `sticky` and `filters_hard`
contributions across the three categorical metrics (cliff, stickiness,
high-confidence completion):

- **Sticky** = at least 2 contributions are `sticky` AND zero are `filters_hard`
- **Filters players hard** = at least 2 contributions are `filters_hard` AND zero are `sticky`
- **Insufficient data** = at least 2 of (cliff, stickiness, completion-with-high-confidence) are `no_data`. The badge is suppressed entirely.
- **Average** = anything else (mixed signals, all neutral, or one each of sticky/filters_hard with the rest neutral)

The order of evaluation matters:
1. Check Insufficient data first (fewest populated signals)
2. Check Sticky / Filters next (consistent strong signal)
3. Default to Average

## Game type interaction

The `engagement_display_rules(game_type)` helper from the game-type spec
already determines whether the categorical badge displays. Phase 1b
honors that:

- **Software**: badge entirely suppressed (engagement section already hidden)
- **Beta/playtest, Early access, Unknown**: `categorical_badge_eligible=False` per spec — badge suppressed even if metrics happen to be populated. Caveat text shown instead.
- **All other types** (linear, mixed, multiplayer, mmo, no_endpoint, sandbox, expansion): badge displays per the threshold rules above.

For `multiplayer`, `mmo`, `no_endpoint`, `sandbox` types: cliff and
high-confidence completion are not displayed (per existing display rules
suppressing them for these types). The categorical signal for these games
is computed from stickiness alone:

- stickiness `sticky` → badge `Sticky`
- stickiness `filters_hard` → badge `Filters players hard`
- stickiness `neutral` → badge `Average`
- stickiness `no_data` → `Insufficient data`

This is a single-signal fallback for game types where the other metrics
don't apply.

## Surfacing locations

- **Game Detail page**: Engagement signals section gets a header line:
Stickiness signal: Sticky · 2 of 3 signals support this
  The "2 of 3 signals support this" sub-line shows the count of contributing
  signals.

- **Library view**: New column "Stickiness" between Type and Developer.
  Badge-style, color-coded:
  - Sticky: green
  - Average: muted/gray
  - Filters hard: amber
  - Insufficient data: hidden (empty cell)
  Sortable.

- **Shortlist cards**: Inline indicator below the existing badges row.
  Small text + colored pill. Hidden for Insufficient data.

- **Backlog rows**: Skipped per Sunday's decision. Already dense.

## New domain helpers (app/hook_metrics.py)

Add to existing module:

```python
def categorize_cliff(cliff_metric: Optional[float]) -> str:
    """Returns 'sticky' / 'filters_hard' / 'neutral' / 'no_data'."""

def categorize_stickiness(stickiness_ratio: Optional[float]) -> str:
    ...

def categorize_completion(completion_rate: Optional[float], confidence: Optional[str]) -> str:
    ...

def compute_stickiness_signal(game) -> tuple[str, int, int]:
    """Returns (signal_label, sticky_count, filters_hard_count).
    
    signal_label: 'sticky' / 'average' / 'filters_hard' / 'insufficient_data'
    sticky_count: how many contributing signals were 'sticky'
    filters_hard_count: how many contributing signals were 'filters_hard'
    
    Honors game_type display rules — for multiplayer/mmo/no_endpoint/sandbox,
    uses stickiness alone. For beta_playtest/early_access/unknown, returns
    'insufficient_data' regardless.
    """
```

## Schema additions

None. Phase 1a metrics are all in place. The categorical signal is
computed at render time from existing data.

(Future: if the categorical signal computation becomes expensive at
scale, cache as `games.stickiness_signal TEXT`. Not needed at 634 games.)

## Constants

```python
CLIFF_FILTERS_THRESHOLD = 20.0       # percentage points
STICKINESS_STICKY_THRESHOLD = 0.90
STICKINESS_FILTERS_THRESHOLD = 0.50
COMPLETION_STICKY_THRESHOLD = 0.15
COMPLETION_FILTERS_THRESHOLD = 0.03
```

## Tests (tests/test_hook_phase1b.py)

Runnable assertion script. Coverage:
- Each `categorize_*` function across all branches (NULL, threshold edges, neutral)
- `categorize_completion` with low-confidence returns no_data even with sticky-range value
- `compute_stickiness_signal` for each game type:
  - linear/mixed: full 3-signal evaluation
  - multiplayer/mmo/no_endpoint/sandbox: stickiness-only
  - beta_playtest/early_access/unknown: always insufficient_data
  - software: always insufficient_data (or skip — section hidden upstream)
- 2-of-3 majority rule with various combinations
- Insufficient data threshold (2-of-3 no_data)

## CSS additions

New rules under `/* === Stickiness signal === */`:
- `.stickiness-badge` — base
- `.stickiness-badge--sticky` — green
- `.stickiness-badge--average` — muted
- `.stickiness-badge--filters_hard` — amber
- `.stickiness-pill` — Shortlist card variant (smaller)

## Templates

Modified:
- `app/templates/partials/game_detail_engagement.html` — header line with
  signal label and count
- `app/templates/library.html` — new Stickiness column
- `app/templates/partials/library_row.html` — column cell
- `app/templates/partials/game_card.html` — inline indicator

## Done criteria

- All four `categorize_*` functions return correct values per the threshold rules
- `compute_stickiness_signal` honors game type display rules
- Game Detail shows the signal label + count line
- Library has Stickiness column, sortable, color-coded
- Shortlist cards show inline indicator (hidden for Insufficient data)
- Backlog rows unchanged
- All existing pages still 200
- Distribution check: report counts of Sticky / Average / Filters / Insufficient across the live library

## Out of scope (Phase 1c+ if ever)

- Mode-specific badge variants (e.g., "Sticky for Continue something")
- Threshold customization per user
- Historical signal tracking (does a game's signal change over time)
- Per-genre threshold variation
- Reddit/community-derived signals (Phase 3)
- Hour-mapped progression curves (Phase 2)
