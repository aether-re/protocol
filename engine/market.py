"""
Aether Re — market loop and baseline allocators.

Wires the simulator to the settlement engine and runs three allocators in
parallel on an identical event stream under identical constraints. The only
permitted difference between them is the allocation rule.

    python3 market.py [years]
"""

from __future__ import annotations
import json
import sys
import math
from dataclasses import dataclass, field

from simulator import MVP_PARAMS, simulate_year, dislocation, keccak256
from settlement import (
    BPS, Layer, LayerState, Event, SystemState,
    settle_epoch, compute_nav, renew,
)

# Live run seed — deliberately NOT the calibration seed. Commit this hash
# before the scored run begins.
LIVE_SEED = keccak256(b"aether-re/live/v1")

SCALE = 10**9              # 1 notional unit ($1M) == 1000 USDC == 1e9 base units
EPOCHS_PER_YEAR = 4
TERM_EPOCHS = 4
SEED_CAPITAL = 3_200_000 * 10**6

# Constraints — shared by every allocator (spec 7).
MIN_BUFFER_BPS = 1_200     # 12% of NAV held idle
MAX_JUNIOR_BPS = 3_500     # 35% of deployed capital
MAX_REGION_BPS = 6_000     # 60%: with only two regions a 40% cap idles ~30% of capital
MAX_LINE_BPS = 10_000    # 100%: cash drag dominates below this; see build log


@dataclass
class LayerDef:
    layer_id: int
    peril_id: int
    region_id: int
    tranche: int
    name: str
    attachment: int
    exhaustion: int
    technical_el: float
    tranche_multiple: float

    @property
    def limit(self) -> int:
        return self.exhaustion - self.attachment


def load_layers(path: str = "layer_table.json") -> list[LayerDef]:
    with open(path) as f:
        data = json.load(f)
    return [
        LayerDef(
            layer_id=r["layer_id"], peril_id=r["peril_id"], region_id=r["region_id"],
            tranche=r["tranche"], name=r["name"],
            attachment=int(r["attachment"] * SCALE),
            exhaustion=int(r["exhaustion"] * SCALE),
            technical_el=r["technical_el"], tranche_multiple=r["tranche_multiple"],
        )
        for r in data["layers"]
    ]


def offered_rol_bps(defs: LayerDef, year: int) -> int:
    """technicalEL x trancheMultiple x dislocation, in bps."""
    d = dislocation(LIVE_SEED, MVP_PARAMS, year, defs.layer_id)
    return max(1, round(defs.technical_el * defs.tranche_multiple * d * BPS))


# --- constraint layer (identical for all allocators) ------------------------

def feasible_lines(defs_by_id, state: SystemState, candidates: list[int]) -> dict[int, int]:
    """Maximum line each candidate could take without breaching any constraint.

    Evaluated against current committed exposure, so it tightens as the
    portfolio fills up.
    """
    nav = compute_nav(state)
    deployable = max(0, state.idle_usdc - (nav * MIN_BUFFER_BPS) // BPS)

    deployed = 0
    junior = 0
    by_region: dict[int, int] = {}
    for lid, layer in state.layers.items():
        if layer.state == LayerState.EXPIRED:
            continue
        c = layer.collateral_remaining
        deployed += c
        if defs_by_id[lid].tranche == 0:
            junior += c
        by_region[defs_by_id[lid].region_id] = by_region.get(defs_by_id[lid].region_id, 0) + c

    out = {}
    for lid in candidates:
        d = defs_by_id[lid]
        caps = [deployable, (d.limit * MAX_LINE_BPS) // BPS]
        # Concentration caps are on post-commitment deployed capital; solving
        # exactly is circular, so bound against current deployed + headroom.
        total_after = deployed + deployable
        if d.tranche == 0:
            caps.append(max(0, (total_after * MAX_JUNIOR_BPS) // BPS - junior))
        caps.append(max(0, (total_after * MAX_REGION_BPS) // BPS - by_region.get(d.region_id, 0)))
        out[lid] = max(0, min(caps))
    return out


# --- allocators -------------------------------------------------------------

def alloc_equal_weight(defs_by_id, state, candidates, year):
    caps = feasible_lines(defs_by_id, state, candidates)
    if not candidates:
        return {}
    nav = compute_nav(state)
    budget = max(0, state.idle_usdc - (nav * MIN_BUFFER_BPS) // BPS)
    per = budget // len(candidates)
    return {lid: min(per, caps[lid]) for lid in candidates}


def alloc_yield_max(defs_by_id, state, candidates, year):
    """Highest offered ROL first. Correlation-blind by construction."""
    caps = feasible_lines(defs_by_id, state, candidates)
    nav = compute_nav(state)
    budget = max(0, state.idle_usdc - (nav * MIN_BUFFER_BPS) // BPS)
    out = {}
    for lid in sorted(candidates, key=lambda x: -offered_rol_bps(defs_by_id[x], year)):
        take = min(caps[lid], budget)
        if take > 0:
            out[lid] = take
            budget -= take
    return out


def alloc_agent_v0(defs_by_id, state, candidates, year):
    """Correlation-aware value allocator.

    Ranks by risk-adjusted margin (offered ROL over technical EL) and applies
    a penalty proportional to existing exposure in the same region, so the
    second Atlantic peril has to be materially cheaper to win capital.
    """
    caps = feasible_lines(defs_by_id, state, candidates)
    nav = compute_nav(state)
    budget = max(0, state.idle_usdc - (nav * MIN_BUFFER_BPS) // BPS)

    deployed = sum(l.collateral_remaining for l in state.layers.values()
                   if l.state != LayerState.EXPIRED) or 1
    by_region: dict[int, int] = {}
    for lid, layer in state.layers.items():
        if layer.state == LayerState.EXPIRED:
            continue
        r = defs_by_id[lid].region_id
        by_region[r] = by_region.get(r, 0) + layer.collateral_remaining

    def score(lid: int) -> float:
        """Risk-adjusted margin, correlation-penalised.

        margin / sqrt(EL) rather than raw margin: junior tranches carry the
        largest absolute margin but the variance scales faster, so raw margin
        loads the book into the volatile end of every tower. Dividing by
        sqrt(EL) prices that. The region term makes the second Atlantic peril
        earn its place rather than being taken on headline yield.

        Tested against four alternatives; this rule won on both annualised
        return and Sharpe. See the rule sweep in the build log.
        """
        d = defs_by_id[lid]
        margin = offered_rol_bps(d, year) / BPS - d.technical_el
        region_share = by_region.get(d.region_id, 0) / deployed
        return (margin / math.sqrt(d.technical_el)) * (1.0 - 0.8 * region_share)

    out = {}
    for lid in sorted(candidates, key=score, reverse=True):
        if score(lid) <= 0.0:          # never write business at negative margin
            continue
        take = min(caps[lid], budget)
        if take > 0:
            out[lid] = take
            budget -= take
    return out


ALLOCATORS = {
    "equal_weight": alloc_equal_weight,
    "yield_max": alloc_yield_max,
    "agent_v1": alloc_agent_v0,
}


# --- run --------------------------------------------------------------------

@dataclass
class Book:
    name: str
    state: SystemState
    nav_history: list[int] = field(default_factory=list)


def make_book(name: str, defs: list[LayerDef]) -> Book:
    layers = {}
    for i, d in enumerate(defs):
        layers[d.layer_id] = Layer(
            layer_id=d.layer_id, peril=d.peril_id, region=d.region_id, tranche=d.tranche,
            attachment=d.attachment, exhaustion=d.exhaustion,
            rate_on_line=0, line_percent=0,
            collateral_posted=0, collateral_remaining=0, accrued_premium=0,
            term_start=0, term_end=i % TERM_EPOCHS, state=LayerState.EXPIRED,
        )
    state = SystemState(
        epoch=0, idle_usdc=SEED_CAPITAL, total_shares=SEED_CAPITAL,
        cedent_balance=10**18, cedent_iou=0, layers=layers,
    )
    return Book(name=name, state=state)


def max_drawdown(series: list[int]) -> float:
    peak, worst = series[0], 0.0
    for v in series:
        peak = max(peak, v)
        worst = max(worst, (peak - v) / peak)
    return worst


def main():
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 95
    defs = load_layers()
    defs_by_id = {d.layer_id: d for d in defs}
    books = {name: make_book(name, defs) for name in ALLOCATORS}

    total_epochs = years * EPOCHS_PER_YEAR
    events_seen = 0

    for epoch in range(total_epochs):
        year = epoch // EPOCHS_PER_YEAR
        quarter = epoch % EPOCHS_PER_YEAR

        # Events for this year land in Q3 (peak season) to keep the mapping
        # simple and deterministic.
        year_events = simulate_year(LIVE_SEED, MVP_PARAMS, year) if quarter == 2 else []
        events = [
            Event(event_id=hash((year, e.peril_id, e.index)) & 0xFFFFFFFF,
                  peril=e.peril_id, subject_loss=int(e.subject_loss * SCALE))
            for e in year_events
        ]
        events_seen += len(events)

        for book in books.values():
            s = book.state
            s.epoch = epoch
            settle_epoch(s, events)

            candidates = [lid for lid, l in s.layers.items() if l.state == LayerState.EXPIRED]
            if candidates:
                targets = ALLOCATORS[book.name](defs_by_id, s, candidates, year)
                lines = {}
                for lid, collateral in targets.items():
                    d = defs_by_id[lid]
                    line_bps = min(MAX_LINE_BPS, (collateral * BPS) // d.limit)
                    if line_bps > 0:
                        s.layers[lid].rate_on_line = offered_rol_bps(d, year)
                        lines[lid] = line_bps
                renew(s, lines, term_length=TERM_EPOCHS)

            book.nav_history.append(compute_nav(s))

    print(f"\nMarket run — {years} simulated years, {total_epochs} epochs, "
          f"{events_seen} catastrophe events")
    print(f"Live seed:  0x{LIVE_SEED.hex()}")
    print(f"Param hash: {MVP_PARAMS.param_hash()}")
    print(f"Seed capital: {SEED_CAPITAL/10**6:,.0f} USDC\n")

    hdr = (f"{'Allocator':<16}{'Final NAV':>16}{'Ann.':>8}{'MaxDD':>9}"
           f"{'Sharpe':>8}{'Worst yr':>10}{'Deployed':>10}")
    print(hdr)
    print("-" * len(hdr))
    for book in books.values():
        h = book.nav_history
        final, start = h[-1], SEED_CAPITAL
        total_ret = final / start - 1
        annual = (final / start) ** (1 / years) - 1
        yearly = [h[i*4+3] for i in range(years)]
        rets = [yearly[i] / (yearly[i-1] or 1) - 1 for i in range(1, years)]
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets))
        s2 = book.state
        dep = sum(l.collateral_remaining for l in s2.layers.values()
                  if l.state != LayerState.EXPIRED) / max(1, compute_nav(s2))
        print(f"{book.name:<16}{final/10**6:>15,.0f}{annual:>8.2%}"
              f"{max_drawdown(h):>9.1%}{(mu/sd if sd else 0):>8.2f}"
              f"{min(rets):>10.1%}{dep:>10.1%}")

    with open("market_run.json", "w") as f:
        json.dump({
            "live_seed": "0x" + LIVE_SEED.hex(),
            "param_hash": MVP_PARAMS.param_hash(),
            "years": years,
            "nav": {b.name: b.nav_history for b in books.values()},
        }, f)
    print("\nWrote market_run.json")


if __name__ == "__main__":
    main()
