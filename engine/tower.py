"""
Aether Re — tower construction by return period.

Attachment points are set off the occurrence exceedance probability (OEP)
curve, which is how real catastrophe towers are structured: you pick return
periods, and expected loss falls out of the resulting structure. Targeting EL
directly over-constrains a contiguous tower and produces degenerate tranches.

    Junior attaches  1-in-8    (12.5% annual occurrence exceedance)
    Mezz   attaches  1-in-25   (4.0%)
    Senior attaches  1-in-70   (1.43%)
    Top of tower     1-in-300  (0.33%)

    python3 tower.py [years]
"""

from __future__ import annotations
import json
import sys
from collections import defaultdict

from simulator import MVP_PARAMS, simulate, keccak256
from calibrate import CALIBRATION_SEED, TRANCHE_MULTIPLES

RETURN_PERIODS = (8, 25, 70, 300)      # junior attach, mezz attach, senior attach, top
TRANCHE_NAMES = ("JUNIOR", "MEZZ", "SENIOR")


def year_max_losses(years: int) -> dict[int, list[float]]:
    """Largest single event per year, per peril — the OEP basis."""
    out = {p.peril_id: [0.0] * years for p in MVP_PARAMS.perils}
    for year, evs in enumerate(simulate(CALIBRATION_SEED, MVP_PARAMS, years)):
        for ev in evs:
            if ev.subject_loss > out[ev.peril_id][year]:
                out[ev.peril_id][year] = ev.subject_loss
    return out


def all_losses(years: int) -> dict[int, list[list[float]]]:
    out = {p.peril_id: [[] for _ in range(years)] for p in MVP_PARAMS.perils}
    for year, evs in enumerate(simulate(CALIBRATION_SEED, MVP_PARAMS, years)):
        for ev in evs:
            out[ev.peril_id][year].append(ev.subject_loss)
    return out


def oep_level(maxima: list[float], return_period: int) -> float:
    """Loss level exceeded once every `return_period` years."""
    s = sorted(maxima, reverse=True)
    rank = max(1, round(len(s) / return_period))
    return s[rank - 1]


def measure(losses_by_year, attach: float, limit: float):
    years = len(losses_by_year)
    total, att_yrs, exh_yrs = 0.0, 0, 0
    for year_losses in losses_by_year:
        year_loss = 0.0
        for sl in year_losses:
            gross = min(max(sl - attach, 0.0), limit)
            if gross > 0:
                year_loss += min(gross, limit - year_loss)
        if year_loss > 0:
            total += year_loss
            att_yrs += 1
            if year_loss >= limit - 1e-9:
                exh_yrs += 1
    return total / years / limit, att_yrs / years, exh_yrs / years


def main():
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    maxima = year_max_losses(years)
    losses = all_losses(years)

    print(f"\nTower construction — {years:,} simulated years")
    print(f"Return periods: junior 1-in-{RETURN_PERIODS[0]}, mezz 1-in-{RETURN_PERIODS[1]}, "
          f"senior 1-in-{RETURN_PERIODS[2]}, top 1-in-{RETURN_PERIODS[3]}\n")

    hdr = (f"{'Layer':<20}{'Attach':>8}{'Exhaust':>9}{'Limit':>8}"
           f"{'EL':>9}{'P(att)':>9}{'P(exh)':>9}{'exh/att':>9}{'ROL':>8}")
    print(hdr)
    print("-" * len(hdr))

    table, warnings = [], []
    for peril in MVP_PARAMS.perils:
        points = [round(oep_level(maxima[peril.peril_id], rp)) for rp in RETURN_PERIODS]
        for t in range(3):
            attach, exhaust = points[t], points[t + 1]
            limit = exhaust - attach
            el, pa, pe = measure(losses[peril.peril_id], attach, limit)
            rol = el * TRANCHE_MULTIPLES[t]
            ratio = pe / pa if pa else 0.0
            name = f"{peril.name}_{TRANCHE_NAMES[t]}"
            print(f"{name:<20}{attach:>8}{exhaust:>9}{limit:>8}"
                  f"{el:>8.2%}{pa:>9.2%}{pe:>9.2%}{ratio:>9.0%}{rol:>7.2%}")

            if ratio > 0.70:
                warnings.append(f"{name}: exh/att {ratio:.0%} — near-binary tranche")
            if limit < 20:
                warnings.append(f"{name}: limit {limit} too thin")

            table.append({
                "layer_id": len(table),
                "peril_id": peril.peril_id,
                "peril_name": peril.name,
                "region_id": peril.region_id,
                "tranche": t,
                "tranche_name": TRANCHE_NAMES[t],
                "name": name,
                "attachment": attach,
                "exhaustion": exhaust,
                "limit": limit,
                "technical_el": round(el, 6),
                "attach_prob": round(pa, 6),
                "exhaust_prob": round(pe, 6),
                "tranche_multiple": TRANCHE_MULTIPLES[t],
                "rate_on_line_bps": round(rol * 10_000),
            })

    els = {t: [r["technical_el"] for r in table if r["tranche"] == t] for t in range(3)}
    for t, (lo, hi) in enumerate([(0.04, 0.12), (0.015, 0.05), (0.004, 0.02)]):
        for r in (x for x in table if x["tranche"] == t):
            if not lo <= r["technical_el"] <= hi:
                warnings.append(
                    f"{r['name']}: EL {r['technical_el']:.2%} outside "
                    f"{lo:.1%}-{hi:.1%} for {TRANCHE_NAMES[t]}")
    for peril in MVP_PARAMS.perils:
        rows = sorted((r for r in table if r["peril_id"] == peril.peril_id),
                      key=lambda r: r["tranche"])
        if not rows[0]["technical_el"] > rows[1]["technical_el"] > rows[2]["technical_el"]:
            warnings.append(f"{peril.name}: EL not monotonic across tranches")

    total_limit = sum(r["limit"] for r in table)
    print(f"\nTotal limit across 9 layers: {total_limit:,} notional units")

    print()
    if warnings:
        print("ACCEPTANCE FAILED:")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("ACCEPTANCE PASSED — table may be frozen.")

    out = {
        "calibration_seed": "0x" + CALIBRATION_SEED.hex(),
        "param_hash": MVP_PARAMS.param_hash(),
        "years": years,
        "return_periods": list(RETURN_PERIODS),
        "accepted": not warnings,
        "total_limit": total_limit,
        "layers": table,
    }
    with open("layer_table.json", "w") as f:
        json.dump(out, f, indent=2)
    print("\nWrote layer_table.json")


if __name__ == "__main__":
    main()
