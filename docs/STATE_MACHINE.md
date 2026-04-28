# Game State Machine — GamePile

Defines the lifecycle states a game can be in, valid transitions
between them, and what triggers each transition.

This document is canonical. When code disagrees with this document,
the code is wrong.

## States

- `never_played` — Steam shows zero playtime, user hasn't touched it
- `played_unclassified` — Steam shows playtime but user hasn't categorized
- `in_progress` — actively being played (user-set or high-confidence inference)
- `finished` — user completed the game
- `dropped` — user gave up on the game (with strength: `soft` / `strong`)
- `not_interested` — user dismissed without playing

## Orthogonal flag

- `blacklisted` (boolean) — never recommend, regardless of state.
  Persists across other state changes. Set by "Never recommend" action.

## Inference rules

Auto-inference only runs when `manually_set = false`. Once a user
explicitly sets status via the UI, `manually_set = true`, and auto
inference never overrides.

Definitions:
- "Recent activity" = `last_played_steam` within last 30 days
- All playtime values are in minutes

### Initial inference (when game is first added to game_state):

- `playtime == 0` → `never_played`
- `playtime < 30` → `played_unclassified`
- `playtime >= 30 AND playtime < (hltb_main_hours * 60) AND recent activity` → `in_progress`
- `playtime >= 30` (any other case) → `played_unclassified`

### Refresh inference (subsequent refreshes):

- If status was auto-inferred and Steam playtime crosses a threshold,
  the status may upgrade
- Status NEVER downgrades automatically (e.g., `in_progress` doesn't
  revert to `played_unclassified` because the user stopped playing)
- `played_unclassified` → `in_progress` if playtime newly crosses 30
  minutes AND recent activity AND `manually_set = false`

## Transitions

### From `never_played`:
- → `played_unclassified` — Steam playtime > 0 detected on refresh
- → `not_interested` — user clicks "Never recommend" or "Not feeling it"
- → `in_progress` — user clicks "I picked this" on a Shortlist recommendation
- → `finished` — user clicks "Already completed" (correcting historical data)

### From `played_unclassified`:
- → `in_progress` — user marks as in-progress, OR auto: playtime ≥ 30min + recent activity
- → `finished` — user marks as finished, OR auto: playtime ≥ HLTB completionist (only if not manually_set)
- → `dropped` (soft) — user marks "Bounced off it"
- → `dropped` (strong) — user marks "Not my thing"
- → `not_interested` — user dismisses

### From `in_progress`:
- → `finished` — user marks finished
- → `dropped` (soft) — user marks "Bounced off it"
- → `dropped` (strong) — user marks "Not my thing"
- → `not_interested` — user dismisses (rare)

### From `finished`:
- → `in_progress` — user replays and explicitly marks (rare)
- (typically terminal)

### From `dropped`:
- → `in_progress` — user "reconsidered" and resumes
- → `finished` — user picks back up and finishes (rare)
- → `not_interested` — user upgrades from "dropped" to "actively not interested"

### From `not_interested`:
- → `in_progress` — user changes mind and picks it
- (typically terminal)

## Mode eligibility by status

Which Shortlist modes consider a game eligible (assuming not blacklisted, in active library):

| Status | I only have tonight | Continue something | Comfort pick | Start something new | Surprise me |
|---|---|---|---|---|---|
| `never_played` | ✓ | ✗ | ✗ | ✓ | ✓ |
| `played_unclassified` (<30min) | ✓ | ✗ | ✓ (if top quartile) | ✓ | ✓ |
| `played_unclassified` (≥30min) | ✓ | ✗ | ✓ (if top quartile) | ✗ | ✓ |
| `in_progress` | ✓ | ✓ | ✓ (if top quartile) | ✗ | ✓ |
| `finished` | ✗ | ✗ | ✓ (if top quartile) | ✗ | ✓ |
| `dropped` (soft) | ✓ (low priority) | ✗ | ✗ | ✗ | ✓ (low priority) |
| `dropped` (strong) | ✗ | ✗ | ✗ | ✗ | ✗ |
| `not_interested` | ✗ | ✗ | ✗ | ✗ | ✗ |
| `blacklisted` (any state) | ✗ | ✗ | ✗ | ✗ | ✗ |

## Notes

- "Top quartile" for Comfort pick = playtime in top 25% of user's library,
  with a minimum floor of 600 minutes (10 hours), reduced incrementally if
  fewer than 5 candidates qualify.
- `blacklisted` is checked BEFORE any other filtering — it's the first
  exclusion in the recommender pipeline.
- Status changes from refresh do NOT trigger affinity updates.
  Only user actions (feedback flow, card buttons) trigger affinity changes.
