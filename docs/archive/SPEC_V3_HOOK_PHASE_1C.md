> **ARCHIVED — historical reference only.**
> Phase 3 hook-point work was retired in v0.7.0 (see `SPEC_HOOK_RETIREMENT.md`).
> OpenCritic integration is set-aside (see `docs/PROJECT_STATE.md` — search "OpenCritic — possible future re-introduction").
> Do not implement against this spec without first checking `docs/PROJECT_STATE.md` current state.

---

# v3 Hook-Point Phase 1c Spec

## Purpose

Phase 1b shipped a categorical stickiness badge with thin signal density:
4.3% Sticky, 1.7% Filters hard, 60% Average, 34% Insufficient across the
live 634-game library. Research dive identified the problem isn't
threshold tuning — it's that the 2-of-3 voting model conflates
heterogeneous signals, ignores cliff position, and packs three distinct
cases into one "Average" label.

Phase 1c replaces the voting rule with weighted scoring, adds
`cliff_position` as a new dimension, recalibrates completion thresholds
to published Steam-population data, and splits the middle bucket into
meaningful sub-labels. The badge taxonomy reframes around the user's
actual decision question — "if I start this, will I see it through?" —
rather than the abstract "stickiness" framing.

Phase 1b is a shipped historical artifact. Its spec
(`SPEC_V3_HOOK_PHASE_1B.md`) is preserved unchanged.

## Five changes (in priority order)

### 1. Add `cliff_position` Phase 1a metric

Single highest-leverage change. Identifies *where* in a game's
achievement progression the largest cliff sits, transforming the same
cliff size into different signals depending on context.

- **Schema:** `games.cliff_position REAL nullable`
- **Computation:** Index of the largest cliff in the sorted-descending
  achievement list (after the same launch-achievement discard
  `cliff_metric` uses), normalized to `[0.0, 1.0]`:
  - `0.0` = first cliff (top of the sorted list, early-game side)
  - `1.0` = last cliff (bottom of the sorted list, endgame side)
  - Formula: `i / (n - 2)` where `i` is the index of the largest cliff
    in the post-discard list and `n` is the post-discard length
- **Populated envelope:** identical to `cliff_metric`. Both functions
  share an internal helper so they always agree on whether to emit
  data for a given game.
- **Tie-break:** when multiple cliffs share the largest size, the
  earliest index wins. Position reflects "first occurrence."

### 2. Recalibrated completion thresholds

Published Steam-population data (Bailey & Miyata 2019) puts median
completion rate at 10%, mean 14%, with high-completion outliers
reaching 50–60%. Phase 1b's `COMPLETION_STICKY_THRESHOLD = 0.15` was
"marginally above mean" — not actually sticky.

| Constant                          | Phase 1b | Phase 1c |
|-----------------------------------|----------|----------|
| `COMPLETION_STICKY_THRESHOLD`     | 0.15     | **0.25** |
| `COMPLETION_FILTERS_THRESHOLD`    | 0.03     | **0.05** |

Same thresholds for high-confidence and low-confidence completion;
only the contribution weight differs (see change 3).

### 3. Replace 2-of-3 voting with weighted scoring

Voting forced binary contributions across heterogeneous signals.
Weighted scoring lets each signal contribute proportional to its
trustworthiness.

Each signal returns `-1` (filters), `0` (neutral), or `+1` (sticky).
Composite score:

```
score =
    1.5 * stickiness_signal
  + 1.0 * cliff_signal               # weighted by cliff_position
  + 0.7 * completion_signal          # when confidence == 'high'
  + 0.3 * completion_signal          # when confidence == 'low'
```

Low-confidence completion contributes at one-third the high-confidence
weight. It is no longer dropped — it's a soft signal that adds
discrimination across the ~428 games we ignored in Phase 1b.

Cliff signal incorporates position:

| `cliff_metric` | `cliff_position`        | Signal           |
|----------------|-------------------------|------------------|
| `≥ 20.0`       | `≤ 0.30` (early)        | `-1` (filters)   |
| `≥ 20.0`       | `0.30 < pos < 0.70` (mid) | `-1` (filters)   |
| `≥ 20.0`       | `≥ 0.70` (late)         | `0` (neutral)    |
| `< 20.0`       | any                     | `0` (neutral)    |
| NULL           | any                     | `0` (neutral)    |

A late cliff with large drop is the completionist gate (rare endgame
achievement), not abandonment. Treating it as filters was a Phase 1b
error.

Score thresholds:

- `score ≥ +1.5` → `Hooks players`
- `score ≤ -1.5` → `Filters early`
- between → falls through to sub-classification (change 4)

### 4. Split "Average" into five meaningful labels

The Phase 1b "Average" bucket was 60% of the library and conveyed
nothing. The initial Phase 1c ship differentiated based on signal
*pattern* (Marathon / Mixed / Standard); the refinement below adds
two score-*lean* buckets that further sub-divide the middle band.

- **`Marathon`** — `review_playtime_median ≥ 50h` AND **high-confidence**
  `completion_rate < 0.10`. High engagement, low confirmed completion =
  open-ended / sandbox-y games people play forever without finishing.
  Restricting to high-confidence completion prevents sparse-data games
  from earning Marathon labels off noisy heuristic estimates.

- **`Usually hooks`** *(refinement, post-initial ship)* — composite
  score in `[+0.5, +1.5)`. Above-neutral lean (above-neutral stickiness,
  marginal completion + small late cliff, etc.) but not strong enough
  for Hooks players. Wins over Mixed signals when both qualify — a
  clear directional lean is more informative than "strong signals
  present".

- **`Often filters`** *(refinement, post-initial ship)* — composite
  score in `(-1.0, -0.5]`. Below-neutral lean, mirror of Usually hooks.
  Asymmetric inner threshold at -0.5 (parallel to Usually hooks at +0.5)
  rather than the SCORE_FILTERS_THRESHOLD-mirroring -0.75, because
  -0.5 is the cleanest "meaningful lean" cut and the boundary the
  workbook brief specified.

- **`Mixed signals`** — composite score in `(-0.5, +0.5)` AND at least
  one of `stickiness` / `cliff` / **high-confidence** `completion`
  contributes a non-zero signal. Lean buckets win over Mixed at the
  ±0.5 boundary, so Mixed now fires only when strong signals genuinely
  cancel out to a near-zero composite. Low-confidence completion still
  feeds the score but doesn't qualify as the strong signal that promotes
  a game from Standard to Mixed.

- **`Standard engagement`** — score in `[-0.5, +0.5]` AND no qualifying
  strong signal. Truly middle-of-the-road metrics across the board.

Marathon takes precedence over Mixed signals, Usually hooks, Often
filters, and Standard when its playtime + completion conditions match.

Marathon eligibility honors game type rules: applies wherever
`review_playtime` is shown (everything except `software`, with
`beta_playtest` / `early_access` / `unknown` short-circuiting to
`Limited data` before the Marathon check).

### 5. Reframed taxonomy and copy

| Phase 1b badge       | Phase 1c badge          | Tooltip                                                                  |
|----------------------|-------------------------|--------------------------------------------------------------------------|
| Sticky               | **Hooks players**       | Most reviewers play deep into the game; cliff patterns suggest engagement holds |
| (split from Average) | **Usually hooks**       | Leans positive — engagement signals tilt sticky but not strong enough for Hooks players |
| Filters players hard | **Filters early**       | Many players abandon in the early or mid-game                            |
| (split from Average) | **Often filters**       | Leans negative — engagement signals tilt toward filtering but not strong enough for Filters early |
| (split from Average) | **Marathon**            | High engagement, low completion — the kind of game people play forever without finishing |
| (split from Average) | **Mixed signals**       | Strong signals in different directions — taste-dependent                 |
| (split from Average) | **Standard engagement** | Middle-of-the-road metrics across the board                              |
| Insufficient data    | **Limited data**        | (unchanged behavior — relabel)                                           |

Game Detail header line shows the composite score and per-signal
breakdown:

```
Stickiness signal: Hooks players (score +2.2)
  Stickiness: high (+1.5)
  Cliff: late & small (0)
  Completion: high-conf, sticky (+0.7)
```

Each breakdown line shows `<signal>: <value description> (weighted contribution)`.
The numbers on the right are weight × signal value; they sum to the
composite score.

## Order of evaluation

The badge resolution order is:

1. **Limited data first** — at least `ceil(total/2)` of the contributing
   metrics are NULL. Low-confidence completion counts as populated for
   this gate so the soft-signal path stays useful on sparse-data games.
2. **Hooks players** — score ≥ +1.5
3. **Filters early** — score ≤ -1.0 (asymmetric)
4. **Marathon** — Marathon condition met (precedence over every
   middle-band label)
5. **Usually hooks** — score ≥ +0.5 (lean wins over Mixed when the lean
   is clear)
6. **Often filters** — score ≤ -0.5
7. **Mixed signals** — score in (-0.5, +0.5), qualifying strong signal
   present (now only fires when strong signals truly cancel)
8. **Standard engagement** — none of the above; default for in-band
   scores with no strong contributing signals

## Game type interaction

`engagement_display_rules(game_type)` continues to gate which signals
contribute:

- **Software:** entire engagement section hidden (unchanged)
- **Beta/playtest, Early access, Unknown:** `Limited data` regardless
  of populated signals (unchanged short-circuit)
- **Multiplayer, MMO, No endpoint, Sandbox:** stickiness alone
  contributes. Score becomes `1.5 × stickiness_signal`. Single-signal
  thresholds:
  - stickiness sticky → score +1.5 → Hooks players
  - stickiness filters → score -1.5 → Filters early
  - stickiness neutral → score 0 → Standard / Marathon / Mixed
- **Linear, Mixed, Expansion:** all three categorical signals contribute

Marathon can fire on any non-`software` and non-ineligible type as long
as the underlying metrics are populated. This is intentional — the
canonical Marathon case (high engagement, low completion) is most
common in `sandbox` and `no_endpoint` types where completion isn't the
headline metric but still has meaning.

## Schema additions

- `games.cliff_position REAL` — nullable, computed by
  `compute_cliff_position(achievements)` alongside `cliff_metric`.

Migration via the same try/except `ALTER TABLE` pattern. Backfill
performed by a one-shot full force refresh post-merge.

## Constants

```python
# cliff position bands
CLIFF_EARLY_POSITION_MAX = 0.30   # ≤ 0.30 = early-game cliff
CLIFF_LATE_POSITION_MIN = 0.70    # ≥ 0.70 = late-game / completionist
# (mid is between these)

# completion thresholds (Phase 1c recalibration)
COMPLETION_STICKY_THRESHOLD = 0.25
COMPLETION_FILTERS_THRESHOLD = 0.05

# score weights
WEIGHT_STICKINESS = 1.5
WEIGHT_CLIFF = 1.0
WEIGHT_COMPLETION_HIGH = 0.7
WEIGHT_COMPLETION_LOW = 0.3

# composite-score thresholds. Asymmetric: Hooks at +1.5 reachable from
# stickiness +1 alone; Filters at -1.0 reachable from cliff -1 alone.
# The asymmetry reflects the structural asymmetry of the cliff signal —
# it only ever pushes negative (large early or mid cliff → filter) and
# never positive (late large cliff routes to neutral, never sticky).
# Lowering the negative threshold lets cliff stand on its own at score
# -1.0, parallel to stickiness +1 being sufficient for Hooks. The size
# + position guards inside signal_value_cliff already filter for
# "meaningful signal" (≥ 20pp drop, early or mid position) before -1
# is emitted, so cliff-alone-at-threshold isn't admitting noise.
SCORE_HOOKS_THRESHOLD = 1.5
SCORE_FILTERS_THRESHOLD = -1.0

# Lean sub-bands (Phase 1c refinement — split Standard by score lean).
# Score in [+0.5, +1.5) → Usually hooks; (-1.0, -0.5] → Often filters.
# Boundaries inclusive on the lean side: at exactly ±0.5 the lean wins.
# This pulls directional games out of Mixed signals (which now fires
# only when strong signals truly cancel inside the middle band) and
# exposes the underlying lean.
SCORE_USUALLY_HOOKS_MIN = 0.5
SCORE_OFTEN_FILTERS_MAX = -0.5

# Marathon thresholds
MARATHON_PLAYTIME_MIN_HOURS = 50.0      # = 3000 minutes
MARATHON_COMPLETION_MAX = 0.10
```

## New / changed domain helpers (`app/hook_metrics.py`)

```python
def compute_cliff_position(achievements: list) -> Optional[float]:
    """Position of largest cliff, [0.0, 1.0]. None when cliff_metric None."""

def signal_value_stickiness(ratio: Optional[float]) -> int:
    """-1 / 0 / +1. NULL → 0 (no contribution to score)."""

def signal_value_cliff(metric: Optional[float], position: Optional[float]) -> int:
    """-1 (filters: large cliff in early or mid), 0 otherwise."""

def signal_value_completion(rate: Optional[float]) -> int:
    """-1 / 0 / +1. NULL → 0. Same thresholds for both confidences;
    weight differs at the score-aggregation layer."""

def compute_stickiness_signal(game) -> tuple:
    """Returns (badge_label, score, breakdown_dict).

    breakdown_dict maps signal_name -> {
        value: int (-1/0/+1),
        weight: float,
        contribution: float (value*weight),
        description: str (e.g. "high", "late & small", "high-conf, sticky"),
    }
    Game Detail header uses the breakdown to render per-signal lines."""
```

The `compute_stickiness_signal` return shape changes from Phase 1b's
`(badge, sticky_count, filters_hard_count)` to
`(badge, score, breakdown_dict)`. All callers must update atomically.

## New badge constants

```python
BADGE_HOOKS_PLAYERS       = "hooks_players"
BADGE_USUALLY_HOOKS       = "usually_hooks"        # refinement, post-initial ship
BADGE_FILTERS_EARLY       = "filters_early"
BADGE_OFTEN_FILTERS       = "often_filters"        # refinement, post-initial ship
BADGE_MARATHON            = "marathon"
BADGE_MIXED_SIGNALS       = "mixed_signals"
BADGE_STANDARD_ENGAGEMENT = "standard_engagement"
BADGE_LIMITED_DATA        = "limited_data"
```

The Phase 1b badge constants (`BADGE_STICKY`, `BADGE_AVERAGE`,
`BADGE_FILTERS_HARD`, `BADGE_INSUFFICIENT_DATA`) are retired — no
backward-compat aliases. The display surfaces use the new constants
directly.

## Tests

`tests/test_hook_phase1c.py` — covers:
- `compute_cliff_position` across early / mid / late, ties, edge n=4
- Each `signal_value_*` across thresholds and NULL
- `signal_value_cliff` early-large / mid-large / late-large /
  small-any / NULL
- `compute_stickiness_signal` for each game type:
  - Linear/mixed/expansion 3-signal: hooks / filters / marathon /
    mixed / standard / limited
  - Multiplayer/mmo/no_endpoint/sandbox stickiness-only path
  - Beta_playtest/early_access/unknown/software → Limited
- Order-of-evaluation precedence (Limited > Hooks/Filters > Marathon
  > Mixed > Standard)
- Marathon precedence over Mixed when both match
- Marathon requires high-confidence completion (low-conf with
  matching playtime/rate doesn't fire Marathon)
- Mixed signals tightening: low-conf completion ±1 alone doesn't
  promote to Mixed
- Limited data gate counts low-conf completion as populated
- Score-breakdown dict structure and arithmetic

`tests/test_hook_metrics.py` — extend with `compute_cliff_position`
basic cases (mirrors `compute_cliff_metric` envelope tests).

## CSS additions / renames

Badge variants in addition to the renamed Phase 1b classes:

```
.stickiness-badge--hooks_players        (replaces --sticky)
.stickiness-badge--usually_hooks        (refinement — desaturated green)
.stickiness-badge--filters_early        (replaces --filters_hard)
.stickiness-badge--often_filters        (refinement — desaturated amber)
.stickiness-badge--marathon             (new)
.stickiness-badge--mixed_signals        (new — split from --average)
.stickiness-badge--standard_engagement  (split from --average)
```

Same pattern for `.stickiness-pill--*`. Usually hooks / Often filters
use desaturated versions of their strong-category neighbors —
visually communicates "lean" without claiming the strong category.
Phase 1b CSS classes (`--sticky`, `--filters_hard`, `--average`) are
renamed in place to keep the stylesheet single-purpose.

Game Detail breakdown line styling: `.engagement-signal-breakdown` (the
indented per-signal list); `.engagement-signal-breakdown-row` per line.

## Templates

Modified:
- `app/templates/partials/game_detail_engagement.html` — new header
  line with score and per-signal breakdown
- `app/templates/library.html` — column header (no change to text;
  sort routing already handles the new badge set)
- `app/templates/partials/library_row.html` — six-way badge dispatch
- `app/templates/partials/game_card.html` — six-way pill dispatch

Routes:
- `app/routes/library.py` — `_STICKINESS_SORT_ORDER` updated for the
  new badges (priority: hooks > marathon > mixed > standard >
  filters_early > limited_data sinks last via null sentinel)

## Out of scope (Phase 1d+ if ever)

- Achievement-level breakdown of the cliff (which specific
  achievement marks the drop)
- Per-genre threshold variation
- Mode-specific badge weighting on Shortlist
- Time-decay of signals (does the badge change as a game ages)
- Reddit / community-derived signals (Phase 3)
- Hour-mapped progression curves (Phase 2)

## Migration / backfill

One-shot full force refresh post-merge to populate `cliff_position` for
all games where `cliff_metric` is currently populated. Same procedure as
Phase 1a checkpoint 4. ~25 minute background task.

## Done criteria

- `games.cliff_position` column populated for every game with
  `cliff_metric` populated (envelope match)
- Spot-check on 4-5 known-cliff games confirms position values feel
  correct (catches off-by-one / reversed-sort errors)
- All Phase 1c domain helpers pass tests
- Game Detail renders score + per-signal breakdown for eligible games;
  Limited data hides the section as before
- Library badge column shows six new labels with updated tooltips and
  sort order
- Shortlist card pill renders the six new labels
- Backlog rows untouched
- All existing pages still 200; Phase 1a / game-type / Phase 1b shipped
  behavior preserved (the latter now retired in favor of 1c)
- Distribution report compares Phase 1b → Phase 1c badge changes; ships
  if results land in target envelope (10–25% Hooks, 5–15% Filters
  early, ≤10% Marathon, Mixed+Standard ≤60%, ≤30% Limited)
