"""
Aether Re — reference settlement engine.

Spec: aether-re-spec-v4.md section 4.4.

This is the authoritative implementation of the epoch waterfall. The Solidity
contract must agree with it exactly on every input; see tests/test_differential.py.

All monetary amounts are integers in the smallest unit (USDC 6dp). Never floats.
Basis points (bps) are integers: 10_000 bps = 100%.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable

BPS = 10_000
EPOCHS_PER_YEAR = 4


class LayerState(IntEnum):
    ACTIVE = 0
    EXHAUSTED = 1
    EXPIRED = 2


@dataclass
class Layer:
    layer_id: int
    peril: int
    region: int
    tranche: int              # 0 JUNIOR, 1 MEZZ, 2 SENIOR
    attachment: int           # notional units
    exhaustion: int           # notional units
    rate_on_line: int         # annual, bps of limit
    line_percent: int         # vault's share, bps
    collateral_posted: int
    collateral_remaining: int
    accrued_premium: int
    term_start: int
    term_end: int
    state: LayerState

    @property
    def limit(self) -> int:
        return self.exhaustion - self.attachment

    @property
    def value(self) -> int:
        """Step 4: layerValue = collateralRemaining + accruedPremium."""
        return self.collateral_remaining + self.accrued_premium


@dataclass
class Event:
    event_id: int
    peril: int
    subject_loss: int         # notional units


@dataclass
class SystemState:
    epoch: int
    idle_usdc: int
    total_shares: int
    cedent_balance: int
    cedent_iou: int
    layers: dict[int, Layer]

    def total_value(self) -> int:
        """Closed-system invariant: this must never change during settlement.

        Premium and losses only move value between the vault side and the
        cedent side. If this drifts, the waterfall has a leak.
        """
        vault = self.idle_usdc + sum(l.value for l in self.layers.values())
        return vault + self.cedent_balance - self.cedent_iou


# --- Step 1 -----------------------------------------------------------------

def accrue_premium(s: SystemState) -> list[tuple[int, int]]:
    """Premium always accrues before losses. EXHAUSTED layers accrue nothing.

    Returns [(layer_id, amount)] for logging.
    """
    accruals = []
    for layer in _iter_layers(s):
        if layer.state != LayerState.ACTIVE:
            continue
        # term_end is exclusive: on risk for [term_start, term_end). Without
        # this the layer earns a fifth quarter of premium on a four-epoch term,
        # because accrual runs before expiry within the same epoch.
        if s.epoch >= layer.term_end:
            continue
        premium = (
            layer.limit * layer.line_percent * layer.rate_on_line
        ) // (BPS * BPS * EPOCHS_PER_YEAR)
        if premium == 0:
            continue
        _pay_from_cedent(s, premium)
        layer.accrued_premium += premium
        accruals.append((layer.layer_id, premium))
    return accruals


# --- Step 3 -----------------------------------------------------------------

def apply_losses(s: SystemState, events: Iterable[Event]) -> list[tuple[int, int, int]]:
    """Sequential erosion. Clamp on collateral_remaining, NOT limit.

    Returns [(event_id, layer_id, payout)].
    """
    payouts = []
    for event in events:
        for layer in _iter_layers(s):
            if layer.state != LayerState.ACTIVE:
                continue
            if s.epoch >= layer.term_end:
                continue  # off risk, awaiting expiry sweep
            if layer.peril != event.peril:
                continue

            gross = min(max(event.subject_loss - layer.attachment, 0), layer.limit)
            if gross == 0:
                continue

            payout = min(
                (gross * layer.line_percent) // BPS,
                layer.collateral_remaining,
            )
            if payout == 0:
                continue

            layer.collateral_remaining -= payout
            s.cedent_balance += payout
            if layer.collateral_remaining == 0:
                layer.state = LayerState.EXHAUSTED
            payouts.append((event.event_id, layer.layer_id, payout))
    return payouts


# --- Step 5 -----------------------------------------------------------------

def expire_and_release(s: SystemState) -> list[tuple[int, int]]:
    """Sweeps ACTIVE *and* EXHAUSTED layers at term end.

    An exhausted layer still holds accrued_premium belonging to the vault.
    Skipping it here burns that premium silently.
    """
    released = []
    for layer in _iter_layers(s):
        if layer.state == LayerState.EXPIRED:
            continue
        if s.epoch < layer.term_end:
            continue
        amount = layer.value
        s.idle_usdc += amount
        layer.collateral_remaining = 0
        layer.accrued_premium = 0
        layer.state = LayerState.EXPIRED
        released.append((layer.layer_id, amount))
    return released


# --- Step 6 -----------------------------------------------------------------

def compute_nav(s: SystemState) -> int:
    """EXHAUSTED layers still count — their accrued premium is real."""
    return s.idle_usdc + sum(
        l.value for l in s.layers.values() if l.state != LayerState.EXPIRED
    )


def share_price(s: SystemState, precision: int = 10**18) -> int:
    if s.total_shares == 0:
        return precision
    return (compute_nav(s) * precision) // s.total_shares


# --- Step 8 -----------------------------------------------------------------

def renew(s: SystemState, target_lines: dict[int, int], term_length: int = 4) -> list[tuple[int, int]]:
    """Commit idle capital to layers starting terms next epoch.

    target_lines maps layer_id -> line_percent in bps. Caller is responsible
    for constraint checking; this function only refuses to overdraw idle.
    """
    committed = []
    for layer_id in sorted(target_lines):
        layer = s.layers[layer_id]
        if layer.state != LayerState.EXPIRED:
            continue
        line = target_lines[layer_id]
        collateral = (layer.limit * line) // BPS
        if collateral == 0 or collateral > s.idle_usdc:
            continue
        s.idle_usdc -= collateral
        layer.line_percent = line
        layer.collateral_posted = collateral
        layer.collateral_remaining = collateral
        layer.accrued_premium = 0
        layer.term_start = s.epoch + 1
        layer.term_end = s.epoch + 1 + term_length
        layer.state = LayerState.ACTIVE
        committed.append((layer_id, collateral))
    return committed


# --- Full epoch -------------------------------------------------------------

@dataclass
class EpochResult:
    epoch: int
    accruals: list = field(default_factory=list)
    payouts: list = field(default_factory=list)
    released: list = field(default_factory=list)
    nav: int = 0
    share_price: int = 0


def settle_epoch(s: SystemState, events: Iterable[Event]) -> EpochResult:
    """Steps 1-6. Agent decision (7) and renewal (8) are called separately."""
    opening_total = s.total_value()

    r = EpochResult(epoch=s.epoch)
    r.accruals = accrue_premium(s)
    r.payouts = apply_losses(s, events)
    r.released = expire_and_release(s)
    r.nav = compute_nav(s)
    r.share_price = share_price(s)

    assert s.total_value() == opening_total, (
        f"conservation violated at epoch {s.epoch}: "
        f"{opening_total} -> {s.total_value()}"
    )
    return r


# --- internals --------------------------------------------------------------

def _iter_layers(s: SystemState):
    """Deterministic iteration order. The Solidity contract must match this,
    and the RNG stream depends on it (spec section 6)."""
    return [s.layers[k] for k in sorted(s.layers)]


def _pay_from_cedent(s: SystemState, amount: int) -> None:
    """Never halt the loop on shortfall — accrue an IOU (spec 4.6)."""
    if s.cedent_balance >= amount:
        s.cedent_balance -= amount
    else:
        shortfall = amount - s.cedent_balance
        s.cedent_balance = 0
        s.cedent_iou += shortfall
