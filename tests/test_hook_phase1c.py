"""Test fixtures for app.hook_metrics Phase 1c weighted-score signal.

Run with: uv run python tests/test_hook_phase1c.py

No pytest dependency — pure assertions, same pattern as other tests/.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.hook_metrics import (
    BADGE_FILTERS_EARLY,
    BADGE_HOOKS_PLAYERS,
    BADGE_LIMITED_DATA,
    BADGE_MARATHON,
    BADGE_MIXED_SIGNALS,
    BADGE_STANDARD_ENGAGEMENT,
    compute_stickiness_signal,
    signal_value_cliff,
    signal_value_completion,
    signal_value_stickiness,
)


def _game(**kw):
    """Stub game with all fields compute_stickiness_signal touches.
    SimpleNamespace + light helpers — no DB required."""
    defaults = {
        "appid": 999,
        "name": "Stub",
        "game_type": "linear",
        "completion_rate": None,
        "completion_rate_confidence": None,
        "cliff_metric": None,
        "cliff_position": None,
        "stickiness_ratio": None,
        "review_playtime_median": None,
        "hltb_main_hours": None,
        "hltb_completionist_hours": None,
        "playtime_minutes": 0,
        "app_type": None,
        "genres": "",
        "tags": "",
        "user_tags": "",
    }
    defaults.update(kw)
    g = SimpleNamespace(**defaults)
    g.genre_list = lambda: [x.strip() for x in g.genres.split(",") if x.strip()]
    g.tag_list = lambda: [x.strip() for x in g.tags.split(",") if x.strip()]
    g.user_tags_list = lambda: [x.strip() for x in g.user_tags.split(",") if x.strip()]
    return g


# ---------------------------------------------------------------------------
# signal_value_stickiness
# ---------------------------------------------------------------------------

def test_signal_value_stickiness_none_returns_zero():
    assert signal_value_stickiness(None) == 0


def test_signal_value_stickiness_high_returns_plus_one():
    assert signal_value_stickiness(0.90) == 1
    assert signal_value_stickiness(1.0) == 1


def test_signal_value_stickiness_low_returns_minus_one():
    assert signal_value_stickiness(0.50) == -1
    assert signal_value_stickiness(0.0) == -1


def test_signal_value_stickiness_mid_returns_zero():
    assert signal_value_stickiness(0.51) == 0
    assert signal_value_stickiness(0.81) == 0
    assert signal_value_stickiness(0.89) == 0


# ---------------------------------------------------------------------------
# signal_value_cliff — position-aware
# ---------------------------------------------------------------------------

def test_signal_value_cliff_null_metric_returns_zero():
    assert signal_value_cliff(None, 0.5) == 0
    assert signal_value_cliff(None, None) == 0


def test_signal_value_cliff_small_returns_zero():
    # < 20pp doesn't qualify regardless of position.
    assert signal_value_cliff(5.0, 0.0) == 0
    assert signal_value_cliff(19.99, 0.5) == 0
    assert signal_value_cliff(0.0, 1.0) == 0


def test_signal_value_cliff_large_early_returns_minus_one():
    assert signal_value_cliff(20.0, 0.0) == -1
    assert signal_value_cliff(35.0, 0.30) == -1


def test_signal_value_cliff_large_mid_returns_minus_one():
    assert signal_value_cliff(25.0, 0.31) == -1
    assert signal_value_cliff(30.0, 0.50) == -1
    assert signal_value_cliff(20.0, 0.69) == -1


def test_signal_value_cliff_large_late_returns_zero():
    # Completionist gate, not abandonment.
    assert signal_value_cliff(20.0, 0.70) == 0
    assert signal_value_cliff(35.0, 0.85) == 0
    assert signal_value_cliff(25.0, 1.0) == 0


# ---------------------------------------------------------------------------
# signal_value_completion — same thresholds for both confidences
# ---------------------------------------------------------------------------

def test_signal_value_completion_null_returns_zero():
    assert signal_value_completion(None) == 0


def test_signal_value_completion_high_returns_plus_one():
    assert signal_value_completion(0.25) == 1
    assert signal_value_completion(0.50) == 1


def test_signal_value_completion_low_returns_minus_one():
    assert signal_value_completion(0.0) == -1
    assert signal_value_completion(0.05) == -1


def test_signal_value_completion_mid_returns_zero():
    assert signal_value_completion(0.06) == 0
    assert signal_value_completion(0.10) == 0
    assert signal_value_completion(0.24) == 0


# ---------------------------------------------------------------------------
# compute_stickiness_signal — Hooks players (3-signal type)
# ---------------------------------------------------------------------------

def test_signal_linear_all_sticky_returns_hooks_players():
    # stickiness +1 (1.5) + cliff 0 (small) + completion +1 high (0.7) = 2.2
    g = _game(
        game_type="linear",
        cliff_metric=5.0, cliff_position=0.5,
        stickiness_ratio=0.95,
        completion_rate=0.30, completion_rate_confidence="high",
    )
    badge, score, breakdown = compute_stickiness_signal(g)
    assert badge == BADGE_HOOKS_PLAYERS
    assert abs(score - 2.2) < 0.001
    assert breakdown["stickiness"]["value"] == 1
    assert breakdown["completion"]["value"] == 1


def test_signal_stickiness_alone_reaches_hooks_threshold():
    # stickiness +1 = +1.5 → exactly at SCORE_HOOKS_THRESHOLD → Hooks.
    # No cliff or completion data — but stickiness is populated, so not
    # limited-data (only 2/3 missing < majority).
    g = _game(
        game_type="linear",
        cliff_metric=5.0, cliff_position=0.5,  # populated, neutral
        stickiness_ratio=0.95,
        completion_rate=None,                  # unpopulated
    )
    badge, score, _ = compute_stickiness_signal(g)
    assert badge == BADGE_HOOKS_PLAYERS
    assert abs(score - 1.5) < 0.001


# ---------------------------------------------------------------------------
# compute_stickiness_signal — Filters early
# ---------------------------------------------------------------------------

def test_signal_filters_early_via_score():
    # stickiness -1 (-1.5) + cliff -1 early (-1.0) + completion -1 high (-0.7) = -3.2
    g = _game(
        game_type="linear",
        cliff_metric=25.0, cliff_position=0.1,
        stickiness_ratio=0.30,
        completion_rate=0.02, completion_rate_confidence="high",
    )
    badge, score, _ = compute_stickiness_signal(g)
    assert badge == BADGE_FILTERS_EARLY
    assert abs(score - (-3.2)) < 0.001


def test_signal_late_cliff_with_filters_stickiness_does_not_filter():
    # stickiness -1 (-1.5) + cliff late (0) + completion null (0) = -1.5.
    # Past the -1.0 threshold either way → BADGE_FILTERS_EARLY. The
    # interesting bit: late cliff contributes 0, so the score stays at
    # -1.5 (stickiness alone). If late cliff incorrectly contributed -1,
    # score would be -2.5; this test guards that regression.
    g = _game(
        game_type="linear",
        cliff_metric=30.0, cliff_position=0.9,  # late large — neutral
        stickiness_ratio=0.30,
        completion_rate=None,
    )
    badge, score, _ = compute_stickiness_signal(g)
    assert badge == BADGE_FILTERS_EARLY
    assert abs(score - (-1.5)) < 0.001


def test_signal_late_cliff_alone_does_not_filter():
    # All neutral — late cliff is neutral, not filter. Score 0 → middle bucket.
    g = _game(
        game_type="linear",
        cliff_metric=35.0, cliff_position=0.95,  # late large
        stickiness_ratio=0.75,                   # neutral
        completion_rate=0.10, completion_rate_confidence="high",  # neutral
    )
    badge, score, _ = compute_stickiness_signal(g)
    assert score == 0.0
    assert badge == BADGE_STANDARD_ENGAGEMENT  # no strong signals


# ---------------------------------------------------------------------------
# compute_stickiness_signal — Marathon
# ---------------------------------------------------------------------------

def test_signal_marathon_high_conf_low_completion_long_playtime():
    # 60h playtime + high-conf completion 0.05 → Marathon. Score should
    # be in middle bucket. cliff/stickiness mid; completion -1 low → -0.7.
    g = _game(
        game_type="linear",
        cliff_metric=10.0, cliff_position=0.5,            # neutral
        stickiness_ratio=0.75,                            # neutral
        completion_rate=0.05, completion_rate_confidence="high",  # filters
        review_playtime_median=60 * 60,                   # 60h
    )
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_MARATHON


def test_signal_marathon_requires_high_confidence_completion():
    # Same shape as Marathon but completion is low-confidence → Marathon
    # doesn't fire. The composite signal: stickiness 0 + cliff 0 +
    # completion -1*0.3 = -0.3 (in band). Low-conf completion at -1 still
    # contributes to score but doesn't trigger Marathon. Standard
    # engagement wins (no qualifying strong signal — stickiness/cliff
    # both 0; high-conf completion absent).
    g = _game(
        game_type="linear",
        cliff_metric=10.0, cliff_position=0.5,
        stickiness_ratio=0.75,
        completion_rate=0.05, completion_rate_confidence="low",
        review_playtime_median=60 * 60,
    )
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_STANDARD_ENGAGEMENT


def test_signal_marathon_requires_long_playtime():
    # Below 50h playtime → Marathon doesn't fire even with high-conf
    # low completion. Falls to Often filters (score -0.7 lands in the
    # negative lean band; leans win over Mixed when the lean is clear).
    g = _game(
        game_type="linear",
        cliff_metric=10.0, cliff_position=0.5,
        stickiness_ratio=0.75,
        completion_rate=0.05, completion_rate_confidence="high",
        review_playtime_median=20 * 60,                   # 20h
    )
    from app.hook_metrics import BADGE_OFTEN_FILTERS
    badge, _, _ = compute_stickiness_signal(g)
    # Score: 0 + 0 + -0.7 = -0.7, in (-1.0, -0.5] → Often filters.
    assert badge == BADGE_OFTEN_FILTERS


def test_signal_marathon_wins_over_mixed_when_both_match():
    # Marathon condition met AND stickiness has strong signal — Marathon
    # takes precedence per spec.
    g = _game(
        game_type="linear",
        cliff_metric=10.0, cliff_position=0.5,
        stickiness_ratio=0.95,                            # +1 strong
        completion_rate=0.05, completion_rate_confidence="high",
        review_playtime_median=80 * 60,                   # 80h
    )
    badge, _, _ = compute_stickiness_signal(g)
    # Score: 1.5 + 0 + -0.7 = 0.8 (in band). Marathon condition:
    # playtime >= 50h ✓, high-conf completion < 0.10 ✓ → Marathon wins.
    assert badge == BADGE_MARATHON


# ---------------------------------------------------------------------------
# compute_stickiness_signal — Mixed signals vs Standard engagement
# ---------------------------------------------------------------------------

def test_signal_usually_hooks_via_high_conf_completion_alone():
    # All else neutral, high-conf completion +1 → score 0.7. The lean
    # buckets win over Mixed when the score has a clear directional
    # lean; this case lands in Usually hooks.
    from app.hook_metrics import BADGE_USUALLY_HOOKS
    g = _game(
        game_type="linear",
        cliff_metric=10.0, cliff_position=0.5,
        stickiness_ratio=0.75,
        completion_rate=0.30, completion_rate_confidence="high",
    )
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_USUALLY_HOOKS


def test_signal_mixed_when_strong_signals_cancel_to_neutral():
    # stickiness +1 (1.5) + cliff -1 early (-1.0) = +0.5 score. Under
    # the new ordering this lands in Usually hooks (lean wins over Mixed
    # when the lean is clear). Mixed signals is reserved for strong
    # signals that genuinely cancel — score in [-0.5, +0.5] strictly.
    from app.hook_metrics import BADGE_USUALLY_HOOKS
    g = _game(
        game_type="linear",
        cliff_metric=25.0, cliff_position=0.1,
        stickiness_ratio=0.95,
        completion_rate=None,
    )
    badge, score, _ = compute_stickiness_signal(g)
    assert abs(score - 0.5) < 0.001
    assert badge == BADGE_USUALLY_HOOKS


def test_signal_mixed_signals_when_strong_signals_truly_cancel():
    # high-conf completion +1 (0.7) + cliff -1 mid (-1.0) = -0.3 score.
    # Two strong signals point in opposite directions but the composite
    # lands strictly inside [-0.5, +0.5] — that's Mixed signals.
    g = _game(
        game_type="linear",
        cliff_metric=25.0, cliff_position=0.5,
        stickiness_ratio=0.75,
        completion_rate=0.30, completion_rate_confidence="high",
    )
    badge, score, _ = compute_stickiness_signal(g)
    assert abs(score - (-0.3)) < 0.001
    assert badge == BADGE_MIXED_SIGNALS


def test_signal_filters_early_via_cliff_alone():
    # cliff -1 alone scores -1.0, hits SCORE_FILTERS_THRESHOLD exactly
    # (the asymmetric -1.0 threshold introduced after observing that
    # cliff is one-sided — it never contributes +1). The size + position
    # guards inside signal_value_cliff already filter for "meaningful
    # signal" (≥ 20pp early or mid), so cliff-alone at -1.0 is intended
    # to qualify as Filters early without needing a second supporting
    # signal.
    g = _game(
        game_type="linear",
        cliff_metric=30.0, cliff_position=0.0,
        stickiness_ratio=0.75,                            # neutral
        completion_rate=None,                             # absent
    )
    badge, score, _ = compute_stickiness_signal(g)
    assert abs(score - (-1.0)) < 0.001
    assert badge == BADGE_FILTERS_EARLY


def test_signal_low_conf_completion_alone_does_not_promote_to_mixed():
    # Low-conf completion +1 contributes -0.3 to 0.3 score but does NOT
    # qualify as a strong signal. With everything else neutral → Standard.
    g = _game(
        game_type="linear",
        cliff_metric=10.0, cliff_position=0.5,
        stickiness_ratio=0.75,
        completion_rate=0.30, completion_rate_confidence="low",
    )
    badge, score, _ = compute_stickiness_signal(g)
    assert abs(score - 0.3) < 0.001
    assert badge == BADGE_STANDARD_ENGAGEMENT


def test_signal_standard_when_all_neutral():
    g = _game(
        game_type="linear",
        cliff_metric=10.0, cliff_position=0.5,
        stickiness_ratio=0.75,
        completion_rate=0.10, completion_rate_confidence="high",
    )
    badge, score, _ = compute_stickiness_signal(g)
    assert score == 0.0
    assert badge == BADGE_STANDARD_ENGAGEMENT


# ---------------------------------------------------------------------------
# compute_stickiness_signal — Usually hooks / Often filters lean buckets
# ---------------------------------------------------------------------------

def test_signal_usually_hooks_at_score_threshold():
    # Score = exactly +0.5 → Usually hooks (boundary inclusive on the lean side).
    from app.hook_metrics import BADGE_USUALLY_HOOKS
    g = _game(
        game_type="linear",
        cliff_metric=25.0, cliff_position=0.1,            # -1 (-1.0)
        stickiness_ratio=0.95,                            # +1 (+1.5)
        completion_rate=None,                             # 0
    )
    badge, score, _ = compute_stickiness_signal(g)
    assert abs(score - 0.5) < 0.001
    assert badge == BADGE_USUALLY_HOOKS


def test_signal_often_filters_via_strong_negative_mix():
    # stickiness -1 (-1.5) + cliff 0 + high-conf completion +1 (+0.7)
    # = -0.8 score, in (-1.0, -0.5] → Often filters.
    from app.hook_metrics import BADGE_OFTEN_FILTERS
    g = _game(
        game_type="linear",
        cliff_metric=10.0, cliff_position=0.5,
        stickiness_ratio=0.30,
        completion_rate=0.30, completion_rate_confidence="high",
    )
    badge, score, _ = compute_stickiness_signal(g)
    assert abs(score - (-0.8)) < 0.001
    assert badge == BADGE_OFTEN_FILTERS


def test_signal_marathon_wins_over_usually_hooks():
    # Marathon conditions met AND score in Usually hooks range — Marathon
    # takes precedence per spec.
    g = _game(
        game_type="linear",
        cliff_metric=10.0, cliff_position=0.5,
        stickiness_ratio=0.95,                            # +1
        completion_rate=0.05, completion_rate_confidence="high",  # Marathon: <0.10 ✓
        review_playtime_median=80 * 60,                   # 80h, Marathon: ≥50h ✓
    )
    badge, score, _ = compute_stickiness_signal(g)
    # Score: 1.5 + 0 + -0.7 = 0.8 (would be Usually hooks without Marathon).
    assert abs(score - 0.8) < 0.001
    assert badge == BADGE_MARATHON


def test_signal_usually_hooks_just_below_hooks_threshold():
    # stickiness 0 + cliff -1 mid (-1.0) + high-conf completion +1 (+0.7)
    # + low-conf nothing = -0.3 — wrong direction. Use:
    # stickiness +1 (+1.5) + cliff -1 mid (-1.0) + low-conf completion +1 (+0.3)
    # = +0.8 → in [+0.5, +1.5) → Usually hooks.
    from app.hook_metrics import BADGE_USUALLY_HOOKS
    g = _game(
        game_type="linear",
        cliff_metric=25.0, cliff_position=0.5,            # -1 mid (-1.0)
        stickiness_ratio=0.95,                            # +1 (+1.5)
        completion_rate=0.30, completion_rate_confidence="low",  # +1 low (+0.3)
    )
    badge, score, _ = compute_stickiness_signal(g)
    assert abs(score - 0.8) < 0.001
    assert badge == BADGE_USUALLY_HOOKS


# ---------------------------------------------------------------------------
# compute_stickiness_signal — single-signal (multiplayer/mmo/no_endpoint/sandbox)
# ---------------------------------------------------------------------------

def test_signal_multiplayer_stickiness_high_returns_hooks():
    # Cliff/completion suppressed; stickiness alone drives. +1 * 1.5 = 1.5 → Hooks.
    g = _game(
        game_type="multiplayer",
        cliff_metric=25.0, cliff_position=0.0,            # ignored
        stickiness_ratio=0.95,
        completion_rate=0.0, completion_rate_confidence="high",  # ignored
    )
    badge, score, breakdown = compute_stickiness_signal(g)
    assert badge == BADGE_HOOKS_PLAYERS
    assert abs(score - 1.5) < 0.001
    # cliff/completion absent from breakdown for these types.
    assert "cliff" not in breakdown
    assert "completion" not in breakdown


def test_signal_mmo_low_stickiness_returns_filters_early():
    g = _game(game_type="mmo", stickiness_ratio=0.30)
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_FILTERS_EARLY


def test_signal_no_endpoint_neutral_stickiness_returns_standard():
    g = _game(game_type="no_endpoint", stickiness_ratio=0.75)
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_STANDARD_ENGAGEMENT


def test_signal_sandbox_null_stickiness_returns_limited():
    g = _game(game_type="sandbox", stickiness_ratio=None)
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_LIMITED_DATA


def test_signal_no_endpoint_marathon_can_fire():
    # Sandbox/no_endpoint games are the canonical Marathon case. Even
    # though they're single-signal for the score, Marathon checks raw
    # metrics (rpm + high-conf completion) which can still be populated.
    g = _game(
        game_type="no_endpoint",
        stickiness_ratio=0.75,                            # neutral
        completion_rate=0.05, completion_rate_confidence="high",
        review_playtime_median=80 * 60,
    )
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_MARATHON


# ---------------------------------------------------------------------------
# compute_stickiness_signal — ineligible types short-circuit to Limited data
# ---------------------------------------------------------------------------

def test_signal_beta_playtest_always_limited():
    g = _game(
        game_type="beta_playtest",
        cliff_metric=0.0, cliff_position=0.5,
        stickiness_ratio=1.0,
        completion_rate=0.50, completion_rate_confidence="high",
    )
    badge, score, breakdown = compute_stickiness_signal(g)
    assert badge == BADGE_LIMITED_DATA
    assert score == 0.0
    assert breakdown == {}


def test_signal_early_access_always_limited():
    g = _game(game_type="early_access", stickiness_ratio=0.95)
    assert compute_stickiness_signal(g)[0] == BADGE_LIMITED_DATA


def test_signal_unknown_always_limited():
    g = _game(game_type="unknown", stickiness_ratio=0.95)
    assert compute_stickiness_signal(g)[0] == BADGE_LIMITED_DATA


def test_signal_software_always_limited():
    g = _game(game_type="software")
    assert compute_stickiness_signal(g)[0] == BADGE_LIMITED_DATA


# ---------------------------------------------------------------------------
# compute_stickiness_signal — Limited data gate
# ---------------------------------------------------------------------------

def test_signal_limited_when_majority_unpopulated():
    # All three signals NULL → 3 of 3 missing → Limited data.
    g = _game(
        game_type="linear",
        cliff_metric=None, cliff_position=None,
        stickiness_ratio=None,
        completion_rate=None,
    )
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_LIMITED_DATA


def test_signal_limited_when_two_of_three_unpopulated():
    # 2 of 3 missing → meets ceil(3/2)=2 threshold → Limited data.
    g = _game(
        game_type="linear",
        cliff_metric=5.0, cliff_position=0.5,             # populated
        stickiness_ratio=None,                            # missing
        completion_rate=None,                             # missing
    )
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_LIMITED_DATA


def test_signal_low_conf_completion_counts_as_populated_for_limited_gate():
    # Stickiness null, cliff null, completion populated (low-conf).
    # 1 populated of 3 < majority → still 2 missing → Limited.
    # Confirms low-conf counts but doesn't help when only one signal
    # is populated.
    g = _game(
        game_type="linear",
        cliff_metric=None,
        stickiness_ratio=None,
        completion_rate=0.10, completion_rate_confidence="low",
    )
    badge, _, _ = compute_stickiness_signal(g)
    assert badge == BADGE_LIMITED_DATA


def test_signal_two_populated_one_missing_not_limited():
    # 2 of 3 populated, 1 missing < majority threshold → not Limited.
    # Score: stickiness 0 + cliff 0 + completion absent = 0 → Standard.
    g = _game(
        game_type="linear",
        cliff_metric=10.0, cliff_position=0.5,
        stickiness_ratio=0.75,
        completion_rate=None,
    )
    badge, _, _ = compute_stickiness_signal(g)
    assert badge != BADGE_LIMITED_DATA
    assert badge == BADGE_STANDARD_ENGAGEMENT


# ---------------------------------------------------------------------------
# Score breakdown structure
# ---------------------------------------------------------------------------

def test_breakdown_contains_keys_for_all_evaluated_signals():
    g = _game(
        game_type="linear",
        cliff_metric=25.0, cliff_position=0.1,
        stickiness_ratio=0.95,
        completion_rate=0.30, completion_rate_confidence="high",
    )
    _, _, breakdown = compute_stickiness_signal(g)
    assert set(breakdown) == {"stickiness", "cliff", "completion"}
    for entry in breakdown.values():
        assert set(entry) == {"value", "weight", "contribution", "description"}


def test_breakdown_contributions_sum_to_score():
    g = _game(
        game_type="linear",
        cliff_metric=25.0, cliff_position=0.1,
        stickiness_ratio=0.95,
        completion_rate=0.30, completion_rate_confidence="high",
    )
    _, score, breakdown = compute_stickiness_signal(g)
    assert abs(score - sum(e["contribution"] for e in breakdown.values())) < 1e-9


def test_breakdown_low_conf_completion_uses_lower_weight():
    g = _game(
        game_type="linear",
        cliff_metric=10.0, cliff_position=0.5,
        stickiness_ratio=0.75,
        completion_rate=0.30, completion_rate_confidence="low",
    )
    _, _, breakdown = compute_stickiness_signal(g)
    # Confirms low-conf hits the 0.3 weight, not 0.7.
    assert breakdown["completion"]["weight"] == 0.3
    assert breakdown["completion"]["contribution"] == 0.3  # 1 * 0.3


def test_breakdown_descriptions_format():
    g = _game(
        game_type="linear",
        cliff_metric=25.0, cliff_position=0.1,
        stickiness_ratio=0.95,
        completion_rate=0.30, completion_rate_confidence="high",
    )
    _, _, breakdown = compute_stickiness_signal(g)
    assert breakdown["stickiness"]["description"] == "high"
    assert breakdown["cliff"]["description"] == "early & large"
    assert breakdown["completion"]["description"] == "high-conf, sticky"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    test_funcs = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    print(f"Running {len(test_funcs)} test(s)…")
    failures = []
    for fn in test_funcs:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as exc:
            failures.append((fn.__name__, exc))
            print(f"  ✗ {fn.__name__}: {exc}")
        except Exception as exc:
            failures.append((fn.__name__, exc))
            print(f"  ✗ {fn.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        sys.exit(1)
    print(f"\nAll {len(test_funcs)} tests passed.")


if __name__ == "__main__":
    main()
