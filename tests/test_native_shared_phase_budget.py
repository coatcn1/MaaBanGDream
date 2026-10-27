from types import SimpleNamespace

import pytest

from agent.realtime.native_shared_phase_budget import NativeSharedPhaseBudget


def effect(sequence, delta, total, **overrides):
    return SimpleNamespace(**(dict(run_id='run', owner='feedback', state='applied', request_sequence=sequence,
                                  delta_ms=delta, owner_cumulative_phase_ms=total) | overrides))


def credit(budget, amount):
    step = 15 if amount > 0 else -15
    for _ in range(int(abs(amount) / 15)):
        sequence = budget.feedback_sequence + 1
        assert budget.observe_executed_feedback(effect(sequence, step, budget.feedback_total + step),
            execution_confirmed=True, before_after_confirmed=True)


def step(budget, raw, bounds, visual):
    remaining = budget.remaining(raw, bounds, visual)
    if remaining is None:
        return 0
    error, (lo, hi) = remaining
    return 15 if error >= 15 and lo >= 15 else -15 if error <= -15 and hi <= -15 else 0


def test_initial_feedback_baseline_is_retained_and_neutral_raw_cannot_undo_it():
    budget = NativeSharedPhaseBudget('run', feedback_total=60)
    assert step(budget, 0, (-10,10), 0) == 0
    assert budget.feedback_baseline == 60 and budget.feedback_total == 60


def test_foreign_credit_applies_to_scalar_bounds_and_remaining_threshold_together():
    budget = NativeSharedPhaseBudget('run')
    credit(budget, 60)
    assert budget.remaining(100, (90,110), 30) == (10, (0,20))
    assert step(budget, 100, (25,175), 30) == 0
    assert step(budget, 100, (90,110), 30) == 0
    assert step(budget, 110, (105,115), 30) == 15


def test_raw_return_only_returns_visual_debt_and_preserves_legal_foreign_effect():
    budget = NativeSharedPhaseBudget('run')
    credit(budget, 15)
    visual, steps = 30, []
    for _ in range(4):
        delta = step(budget, 0, (0,0), visual)
        steps.append(delta)
        visual += delta
    assert steps == [-15,-15,0,0] and visual == 0
    assert budget.feedback_total == 15 and budget.feedback_credit == 15


def test_reverse_never_creates_opposite_credit_or_exceeds_signed_raw_target():
    budget = NativeSharedPhaseBudget('run')
    credit(budget, 60)
    visual = 30
    for _ in range(12):
        visual += step(budget, -100, (-100,-100), visual)
    assert visual == -90 and budget.feedback_total == 60
    for amount in range(-180,181,15):
        budget.feedback_credit = amount
        values = [budget.target(raw) for raw in range(-200,201)]
        assert values == sorted(values)
        assert all(min(0,raw) <= value <= max(0,raw) for raw,value in zip(range(-200,201),values))


@pytest.mark.parametrize('proof', ['execution', 'before-after', 'missing-sequence', 'foreign-run'])
def test_unresolved_effect_cannot_be_credited_or_silently_drop_existing_ledger(proof):
    budget = NativeSharedPhaseBudget('run')
    credit(budget, 15)
    other = effect(2,15,30)
    kwargs = dict(execution_confirmed=proof != 'execution', before_after_confirmed=proof != 'before-after')
    if proof == 'missing-sequence':
        other = effect(3,15,45)
    if proof == 'foreign-run':
        other = effect(2,15,30,run_id='foreign')
    assert not budget.observe_executed_feedback(other, **kwargs)
    assert budget.remaining(100, (90,110), 30) is None
    assert budget.feedback_credit == 15 and budget.feedback_total == 15


def test_effect_dedup_conflict_and_unbridged_reference_preserve_debt():
    budget = NativeSharedPhaseBudget('run')
    credit(budget, 15)
    assert budget.observe_executed_feedback(effect(1,15,15), execution_confirmed=True, before_after_confirmed=True)
    assert budget.feedback_credit == 15
    assert not budget.observe_executed_feedback(effect(1,-15,-15), execution_confirmed=True, before_after_confirmed=True)
    assert budget.feedback_credit == 15
    reference = object()
    budget.unresolved_reason = None
    assert budget.reference_ready(reference)
    assert not budget.reference_ready(object())
    assert budget.feedback_credit == 15 and budget.feedback_total == 15
