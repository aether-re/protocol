"""Hand-computed fixtures for each waterfall step. Run: python3 -m pytest -q"""

import pytest
from settlement import (
    BPS, Layer, LayerState, Event, SystemState,
    accrue_premium, apply_losses, expire_and_release,
    compute_nav, settle_epoch, renew,
)

USDC = 10**6
M = USDC  # 1 notional unit ($1M) == 1 USDC unit at 1:1000 scale, times 10^6


def mk_layer(layer_id=1, attach=400*M, exhaust=800*M, rol=800, line=5000,
             term_end=4, state=LayerState.ACTIVE, peril=0):
    limit = exhaust - attach
    collateral = (limit * line) // BPS
    return Layer(
        layer_id=layer_id, peril=peril, region=0, tranche=0,
        attachment=attach, exhaustion=exhaust,
        rate_on_line=rol, line_percent=line,
        collateral_posted=collateral, collateral_remaining=collateral,
        accrued_premium=0, term_start=0, term_end=term_end, state=state,
    )


def mk_state(layers, idle=0, cedent=10**15, epoch=0, shares=1000*USDC):
    return SystemState(
        epoch=epoch, idle_usdc=idle, total_shares=shares,
        cedent_balance=cedent, cedent_iou=0,
        layers={l.layer_id: l for l in layers},
    )


# --- Step 1: premium --------------------------------------------------------

def test_premium_scales_by_line_and_quarter():
    # limit 400M, line 50%, ROL 8% -> annual 400*0.5*0.08 = 16M, quarterly 4M
    layer = mk_layer(attach=400*M, exhaust=800*M, rol=800, line=5000)
    s = mk_state([layer])
    accrue_premium(s)
    assert layer.accrued_premium == 4 * M


def test_exhausted_layer_accrues_nothing():
    layer = mk_layer(state=LayerState.EXHAUSTED)
    layer.collateral_remaining = 0
    s = mk_state([layer])
    accrue_premium(s)
    assert layer.accrued_premium == 0


def test_cedent_shortfall_becomes_iou_not_halt():
    layer = mk_layer()
    s = mk_state([layer], cedent=1 * M)
    accrue_premium(s)
    assert s.cedent_balance == 0
    assert s.cedent_iou == 3 * M       # needed 4M, had 1M
    assert layer.accrued_premium == 4 * M


# --- Step 3: losses ---------------------------------------------------------

def test_payout_scales_by_line_percent():
    # attach 400M, event 600M -> gross 200M, line 50% -> payout 100M
    layer = mk_layer(attach=400*M, exhaust=800*M, line=5000)
    s = mk_state([layer])
    apply_losses(s, [Event(1, 0, 600*M)])
    assert layer.collateral_remaining == (200 - 100) * M


def test_gross_clamped_at_limit():
    # event 5000M vastly exceeds exhaustion; gross capped at limit 400M
    layer = mk_layer(attach=400*M, exhaust=800*M, line=5000)
    s = mk_state([layer])
    apply_losses(s, [Event(1, 0, 5000*M)])
    assert layer.collateral_remaining == 0
    assert layer.state == LayerState.EXHAUSTED


def test_erosion_is_cumulative_within_term():
    # two events, each eroding 50M of a 200M collateral position
    layer = mk_layer(attach=400*M, exhaust=800*M, line=5000)
    s = mk_state([layer])
    apply_losses(s, [Event(1, 0, 500*M), Event(2, 0, 500*M)])
    # each: gross 100M, line 50% -> 50M
    assert layer.collateral_remaining == (200 - 100) * M


def test_clamp_is_on_remaining_not_limit():
    """The critical one. A partially eroded layer cannot pay more than it holds."""
    layer = mk_layer(attach=400*M, exhaust=800*M, line=5000)
    s = mk_state([layer])
    apply_losses(s, [Event(1, 0, 700*M)])   # gross 300M, line -> 150M
    assert layer.collateral_remaining == 50 * M
    apply_losses(s, [Event(2, 0, 800*M)])   # gross 400M, line -> 200M, but only 50M left
    assert layer.collateral_remaining == 0
    assert layer.state == LayerState.EXHAUSTED


def test_below_attachment_no_payout():
    layer = mk_layer(attach=400*M)
    s = mk_state([layer])
    apply_losses(s, [Event(1, 0, 399*M)])
    assert layer.collateral_remaining == layer.collateral_posted


def test_wrong_peril_untouched():
    layer = mk_layer(peril=0)
    s = mk_state([layer])
    apply_losses(s, [Event(1, 1, 5000*M)])
    assert layer.collateral_remaining == layer.collateral_posted


# --- Steps 5 & 6: lifecycle and NAV ----------------------------------------

def test_exhausted_layer_retains_premium_and_counts_in_nav():
    layer = mk_layer(term_end=10)
    s = mk_state([layer])
    accrue_premium(s)                       # +4M premium
    apply_losses(s, [Event(1, 0, 9000*M)])  # wipes collateral
    assert layer.state == LayerState.EXHAUSTED
    assert layer.accrued_premium == 4 * M
    assert compute_nav(s) == 4 * M          # premium still counts


def test_exhausted_layer_returns_premium_at_expiry():
    layer = mk_layer(term_end=1)
    s = mk_state([layer], epoch=0)
    accrue_premium(s)                       # on risk at epoch 0
    apply_losses(s, [Event(1, 0, 9000*M)])
    s.epoch = 1                             # term_end reached, now off risk
    expire_and_release(s)
    assert layer.state == LayerState.EXPIRED
    assert s.idle_usdc == 4 * M             # would be 0 if EXHAUSTED were skipped


def test_expiry_returns_collateral_plus_premium():
    layer = mk_layer(term_end=1)
    s = mk_state([layer], epoch=0)
    accrue_premium(s)
    s.epoch = 1
    expire_and_release(s)
    assert s.idle_usdc == 200 * M + 4 * M


def test_premium_accrues_before_losses_in_same_epoch():
    layer = mk_layer(term_end=10)
    s = mk_state([layer])
    settle_epoch(s, [Event(1, 0, 9000*M)])
    assert layer.accrued_premium == 4 * M   # earned despite being wiped


# --- Invariant --------------------------------------------------------------

def test_conservation_holds_across_random_epochs():
    import random
    rng = random.Random(42)
    layers = [mk_layer(layer_id=i, term_end=100, peril=i % 3) for i in range(1, 10)]
    s = mk_state(layers, idle=500*M)
    opening = s.total_value()
    for epoch in range(50):
        s.epoch = epoch
        events = [
            Event(epoch*10+j, rng.randrange(3), rng.randrange(0, 1200)*M)
            for j in range(rng.randrange(0, 3))
        ]
        settle_epoch(s, events)   # asserts conservation internally
    assert s.total_value() == opening


def test_renew_refuses_to_overdraw_idle():
    layer = mk_layer(state=LayerState.EXPIRED)
    layer.collateral_remaining = 0
    s = mk_state([layer], idle=1 * M)
    committed = renew(s, {1: 5000})   # needs 200M, has 1M
    assert committed == []
    assert s.idle_usdc == 1 * M


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_term_earns_exactly_four_quarters():
    """A four-epoch term must earn four quarters of premium, not five.

    termEnd is exclusive. Accrual runs before expiry within an epoch, so
    without the guard the layer accrues once more on the epoch it expires.
    """
    layer = mk_layer(term_end=4)
    s = mk_state([layer], epoch=0)
    for epoch in range(6):
        s.epoch = epoch
        settle_epoch(s, [])
    assert s.idle_usdc == 200 * M + 16 * M
    assert layer.state == LayerState.EXPIRED


def test_off_risk_layer_absorbs_no_losses():
    layer = mk_layer(term_end=2)
    s = mk_state([layer], epoch=2)
    apply_losses(s, [Event(1, 0, 9000*M)])
    assert layer.collateral_remaining == layer.collateral_posted
