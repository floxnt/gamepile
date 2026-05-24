# Engagement Texture Probe Report

**Date:** 2026-05-24
**Scope:** Offline empirical test — does engagement texture estimation
(without completion identification) produce a signal that correlates
with actual user engagement behavior?
**Status:** FAIL — permanent retirement of dormant hook-point pipeline
is justified.

## 1. Predeclared formula and thresholds

All values locked before any probe data was seen. The formula was
committed in `scripts/engagement_texture_probe.py` with inline
comments before the active clock started.

### Signals (NO completion_rate, NO completion identification)

| Signal | Source | Weight | +1 threshold | -1 threshold |
|---|---|---|---|---|
| Stickiness ratio | Steam reviews (per-review playtime_at_review) | 1.5 | >= 0.90 | <= 0.50 |
| Cliff composite | Steam achievements (largest gap × position) | 1.0 | (never positive) | >= 20pp AND position < 0.70 |
| Median achievement unlock % | Steam achievements (already live) | 0.7 | >= 24.1% (P75) | <= 7.3% (P25) |

Median unlock % cut points derived from the live Cohort A distribution
(355 populated values): P25 = 7.3%, P75 = 24.1%.

### Bucket thresholds

| Bucket | Score range |
|---|---|
| Engaged retention | >= +1.5 |
| Above average | [+0.5, +1.5) |
| Standard | [-0.5, +0.5) |
| Below average | [-1.5, -0.5) |
| Early attrition | <= -1.5 |
| Limited | < 2 non-NULL signals |

### Predeclared pass/fail gates

**Distribution (Cohort A):**
1. >= 60% non-limited
2. 20–45% outside neutral (Engaged + Above + Below + Attrition)
3. No single bucket > 45%
4. Limited <= 35%

**Behavior (Cohort A, playtime >= 30 minutes):**
- B1: Top-bucket median normalized engagement >= 1.75× bottom-bucket
- B2: Bottom-bucket bounce rate >= 20pp higher than top-bucket
- B3: Spearman(score, normalized engagement) >= 0.25

## 2. Cohort A distribution

441 games: 343 linear, 98 mixed, 0 expansion.

| Bucket | Count | % | Gate check |
|---|---|---|---|
| Engaged retention | 83 | 18.8% | |
| Above average | 62 | 14.1% | |
| Standard | 122 | 27.7% | Gate 3: PASS (< 45%) |
| Below average | 78 | 17.7% | |
| Early attrition | 9 | 2.0% | |
| Limited | 87 | 19.7% | Gate 4: PASS (< 35%) |

Non-limited: 354/441 = **80.3%** — Gate 1: **PASS** (>= 60%)

Outside neutral: 232/441 = **52.6%** — Gate 2: **FAIL** (target 20–45%)

### Distribution gate verdict: FAIL

Gate 2 fails because the percentile-based median_unlock_pct thresholds
(P25/P75) mechanically force approximately half of all games with
achievement data to receive a non-zero median signal contribution.
Combined with stickiness_ratio's relatively permissive thresholds
(only the top 10% and bottom 50% are non-neutral), the score
distribution pushes too many games outside the Standard band. The
formula over-classifies: it produces labels for 52.6% of games, but
those labels don't carry behavioral validation (see below).

### Median achievement unlock % coverage gap

86/441 Cohort A games (19.5%) lack `median_achievement_unlock_pct`.
These are games with no Steam achievements (DLCs without separate
achievement lists, pre-achievement-era games, some indie titles).
Combined with the ~2 games lacking sufficient reviews, this produces
the 87-game Limited bucket. This is a structural coverage floor for
any signal that depends on achievement data.

## 3. Cohort A behavior validation

173 Cohort A games with playtime >= 30 minutes and a non-Limited
bucket assignment.

### Gate B1: Engagement ratio — PASS

| Bucket | n (played) | Median normalized engagement |
|---|---|---|
| Engaged retention | 40 | 0.698 |
| Attrition + Below | 42 | 0.353 |

Ratio: **1.98×** (threshold: 1.75×). This gate passes, but the
absolute values are notable: the top bucket's median user plays to
69.8% of HLTB main, while the bottom bucket plays to 35.3%. Both
are "continued" rather than "bounced" by the behavior classification
— the gap is in depth of engagement, not in bounce-vs-continue.

### Gate B2: Bounce rate difference — FAIL

| Bucket | n | Bounced | Bounce rate |
|---|---|---|---|
| Engaged retention | 40 | 6 | 15.0% |
| Attrition + Below | 42 | 9 | 21.4% |

Difference: **6.4 percentage points** (threshold: 20pp).

The signal does not distinguish between games the user bounces off
and games the user continues. The bounce rates are similar across
buckets — the score's composition of stickiness/cliff/median-unlock
does not predict the user's own bounce behavior.

### Gate B3: Spearman correlation — FAIL

Spearman rho between continuous score and normalized user engagement:
**0.1088** (threshold: 0.25, n=173).

Near-zero correlation. The engagement texture score has no meaningful
linear relationship with how much of a game the user actually plays
relative to its length. This is the most damning finding: a score
that doesn't correlate with the thing it's trying to predict is not
a useful signal regardless of how well its distribution looks.

### Behavior gate verdict: FAIL

One of three gates passed (B1: engagement ratio). The two that failed
(B2: bounce difference, B3: Spearman correlation) are the more
discriminating tests — B1 only compares bucket medians, while B2 and
B3 test whether the score actually predicts distinct user outcomes.

## 4. Diagnostic samples and smell test

### Engaged retention (score >= +1.5) — 83 games

Representative samples:

| Game | Score | Playtime | HLTB | Norm | Behavior | Sticky | Cliff | Med% |
|---|---|---|---|---|---|---|---|---|
| Portal 2 | +2.20 | 35h | 9h | 4.06 | continued | 0.93 | 9.5pp@0.50 | 24.5% |
| Braid | +2.20 | 6h | 5h | 1.11 | continued | 0.91 | 25.0pp@1.00 | 34.9% |
| Spec Ops: The Line | +2.20 | 5h | 6h | 0.82 | continued | 0.92 | 6.3pp@0.09 | 31.7% |
| The Stanley Parable | +2.20 | 0h | 1h | 0.00 | unplayed | 0.93 | 17.1pp@0.20 | 30.4% |
| Game Dev Tycoon | +2.20 | 1h | 9h | 0.16 | unclear | 0.92 | 8.2pp@0.13 | 27.8% |

The bucket correctly identifies well-regarded, widely-completed games.
However, it also contains games the user hasn't played (Stanley
Parable at 0h, Game Dev Tycoon at 1h) — the score measures population
engagement, not user engagement. This is structurally the same
overclaim problem that retired the original hook-point pipeline.

### Early attrition (score <= -1.5) — 9 games

| Game | Score | Playtime | HLTB | Norm | Behavior | Sticky | Cliff | Med% |
|---|---|---|---|---|---|---|---|---|
| Killing Floor | -2.20 | 2h | 18h | 0.13 | unclear | 0.47 | 4.5pp@0.00 | 3.7% |
| Tom Clancy's Splinter Cell Blacklist | -2.20 | 0h | 11h | 0.00 | unplayed | 0.50 | 0.6pp@0.57 | 3.0% |
| Zero Hour | -2.20 | 0h | 16h | 0.00 | unplayed | 0.45 | 3.4pp@0.05 | 2.2% |

**Smell test flag:** Killing Floor is a co-op horde shooter — its
game_type classification as "linear" is arguably wrong, and its low
stickiness ratio reflects its review population, not its retention
pattern for players who actually engage with co-op. Zero Hour is a
tactical shooter similar to Rainbow Six Siege, again misclassified.
The bottom bucket contains games that are arguably mistyped rather
than genuinely filtering.

### Below average — notable misclassification

Left 4 Dead 2: score -0.70, playtime 54h (5.29× HLTB main), behavior
"continued." A game the user has clearly played extensively, classified
as "Below average" engagement. This is the canonical failure case for
a population-based engagement signal: the population's low median
achievement unlock (6.4%) and mid-range stickiness ratio (0.80) don't
reflect this user's deep engagement.

### Smell test verdict

The diagnostic samples confirm two structural problems:

1. **Population signal ≠ user signal.** The score predicts what the
   Steam population does, not what this user does. Games the user has
   played extensively can land in Below average or Standard.

2. **Game-type misclassification bleeds through.** Co-op shooters and
   multiplayer-adjacent games classified as "linear" distort the
   bottom of the distribution.

Even if the numeric gates had passed, the smell test would flag the
Left 4 Dead 2 misclassification as a meta-validation failure.

## 5. Cohort B exploratory report

123 games: 21 sandbox, 36 no_endpoint, 23 mmo, 43 multiplayer.

| Bucket | Count | % |
|---|---|---|
| Engaged retention | 14 | 11.4% |
| Above average | 12 | 9.8% |
| Standard | 31 | 25.2% |
| Below average | 30 | 24.4% |
| Early attrition | 6 | 4.9% |
| Limited | 30 | 24.4% |

Per game type:

| Type | n | Engaged+Above | Standard | Below+Attrition | Limited |
|---|---|---|---|---|---|
| sandbox | 21 | 3 | 7 | 5 | 6 |
| no_endpoint | 36 | 12 | 10 | 11 | 3 |
| mmo | 23 | 5 | 7 | 3 | 8 |
| multiplayer | 43 | 6 | 7 | 17 | 13 |

**Observation:** Multiplayer games skew heavily toward Below/Attrition
(17/43 = 40%) and Limited (13/43 = 30%). This reflects the low median
achievement unlock % typical of multiplayer games (most players don't
pursue achievements in competitive/co-op contexts). The signal is
measuring achievement design, not engagement texture.

**Marathon validation check:** The original Phase 1c Marathon validation
showed RDR2, Civilization VI, and Total War: WARHAMMER correctly labeled.
Those games are in Cohort B (no_endpoint/sandbox). Under the new formula
they would need stickiness >= 0.90 AND median_unlock >= 24.1% to reach
Engaged retention. The Marathon pattern (high engagement, low completion)
doesn't map cleanly to this formula because low median_unlock_pct pushes
them toward attrition, not engagement.

**Verdict on Cohort B:** The formula designed for finite/progression
games does not transfer to open-ended games. A separate model for
open-ended engagement is not justified by this probe's findings — the
underlying problem (population signal ≠ user signal) applies equally
to Cohort B.

## 6. Cohort C

84 games hard-excluded:

| Type | Count |
|---|---|
| software | 3 |
| beta_playtest | 18 |
| early_access | 36 |
| unknown | 27 |

No scoring attempted. Count reported for completeness.

## 7. Final verdict

### Gate results

| Gate | Threshold | Actual | Result |
|---|---|---|---|
| Distribution 1 | >= 60% non-limited | 80.3% | PASS |
| Distribution 2 | 20–45% outside band | 52.6% | **FAIL** |
| Distribution 3 | No bucket > 45% | 27.7% (Standard) | PASS |
| Distribution 4 | Limited <= 35% | 19.7% | PASS |
| Behavior B1 | Engagement ratio >= 1.75× | 1.98× | PASS |
| Behavior B2 | Bounce diff >= 20pp | 6.4pp | **FAIL** |
| Behavior B3 | Spearman >= 0.25 | 0.1088 | **FAIL** |

Distribution gates: **FAIL** (1 of 4 failed)
Behavior gates: **FAIL** (2 of 3 failed)
Smell test: **FAIL** (Left 4 Dead 2 at -0.70 with 54h playtime)

### OVERALL: FAIL

**The engagement texture signal, even without the problematic
completion identification, does not correlate with actual user
engagement behavior.** The Spearman correlation of 0.11 is the
dispositive finding: a score with near-zero correlation to the thing
it's trying to predict cannot be improved by threshold tuning,
weight adjustment, or bucket relabeling. The signal is measuring
population-level achievement and review patterns that don't predict
individual user engagement.

### Root cause analysis

The original hook-point retirement identified completion_rate's 90%
low-confidence rate as the structural failure. This probe tested
whether removing completion identification and substituting
median_achievement_unlock_pct would produce a viable signal. The
answer is no, but the reason is deeper than the completion heuristic:

1. **Population statistics don't predict individual behavior.**
   Steam achievement unlock rates and reviewer playtime distributions
   describe what the population of all Steam players does. This user's
   engagement pattern (which games they bounce from, which they play
   deeply) is driven by personal taste, timing, mood, and context —
   none of which are captured by population-level metrics.

2. **The strongest signal (stickiness_ratio) has a narrow useful band.**
   90% of Cohort A games have stickiness_ratio between 0.51 and 0.89
   — the neutral zone. Only extreme values (very high or very low)
   contribute to the score, which limits the formula's discriminating
   power.

3. **Median achievement unlock % is a game-design metric, not an
   engagement metric.** It measures how many achievements the game's
   developer designed to be accessible vs. challenging. A game with
   50 grindy challenges has a low median; a game with 10 well-paced
   milestones has a high median. Neither tells you whether a specific
   user will enjoy or complete the game.

### Implication for permanent retirement

Per the predeclared decision framework:

> If Cohort A fails either gate: probe is the predicate for permanent
> retirement. Next round: DROP COLUMN on dormant hook fields, delete
> hook_metrics.py, delete dormant tests, delete dormant diagnostic
> scripts, remove COALESCE passthrough for dead fields.

The probe failed both gates. Permanent retirement of the dormant
hook-point pipeline is justified by the empirical evidence. The
product direction for GamePile's engagement signal surface is the
honest display-only stat (median achievement unlock %) that already
ships — a labeled fact the user interprets, not a behavioral
prediction the app makes.
