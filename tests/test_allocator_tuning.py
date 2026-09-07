from crypto_bot.evaluation.allocator_tuning import default_candidate_rules
from crypto_bot.strategy.allocator import AllocationRules


def test_tuning_grid_is_small_coarse_and_contains_controls():
    rows = default_candidate_rules(AllocationRules())
    names = [name for name, _ in rows]
    assert "raw-4-style" in names
    assert "asymmetric-no-band" in names
    assert "band-005" in names
    assert len(rows) <= 16

    raw = dict(rows)["raw-4-style"]
    assert raw.neutral_confirm_days == 1
    assert raw.fast_entry_gap_pct == 0
    assert raw.slow_bull_mixed_to_recovery is False

    candidate = dict(rows)["band-005"]
    assert candidate.recovery_confirm_days == 1
    assert candidate.neutral_confirm_days == 2
    assert candidate.recovery_reentry_lockout_days == 0
    assert candidate.fast_entry_gap_pct == 0.05
    assert candidate.fast_exit_gap_pct == -0.05
