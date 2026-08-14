"""
Aether Re — layer calibration harness.

Spec: aether-re-spec-v4.md section 5.1.

Runs N simulated years, computes per-layer expected loss, annual attachment
probability, and annual exhaustion probability. Sets technicalEL from the
output and emits the frozen layer table as JSON for the contract constructor.

Acceptance: junior EL 5-10%, senior EL 0.5-1.5%.

    python3 calibrate.py [years]
"""

from __future__ import annotations
import json
import sys
from dataclasses import dataclass, asdict
from collections import defaultdict

from simulator import MVP_PARAMS, simulate, keccak256

CALIBRATION_SEED = keccak256(b"aether-re/calibration/v1")

TRANCHE_NAMES = ("JUNIOR", "MEZZ", "SENIOR")
TRANCHE_MULTIPLES = (1.6, 2.2, 3.5)


@dataclass
class LayerDef:
    layer_id: int
    peril_id: int
    peril_name: str
    region_id: int
    tranche: int
    attachment: float
    exhaustion: float

    @property
    def limit(self) -> float:
        return self.exhaustion - self.attachment

    @property
    def name(self) -> str:
        return f"{self.peril_name}_{TRANCHE_NAMES[self.tranche]}"


# Proposed table from spec 5.1 — this run decides whether it survives.
PROPOSED = [
    (0, "FL_WIND",   0, [(400, 800), (800, 1500), (1500, 2500)]),
    (1, "GULF_WIND", 0, [(320, 650), (650, 1200), (1200, 2000)]),
    (2, "EU_WIND",   1, [(180, 350), (350, 550),  (550, 900)]),
]


def build_layers() -> list[LayerDef]:
    layers, lid = [], 0
    for peril_id, peril_name, region_id, tranches in PROPOSED:
        for tranche, (attach, exhaust) in enumerate(tranches):
            layers.append(LayerDef(lid, peril_id, peril_name, region_id,
                                   tranche, float(attach), float(exhaust)))
            lid += 1
    return layers


def calibrate(years: int) -> tuple[list[LayerDef], dict]:
    layers = build_layers()
    by_peril: dict[int, list[LayerDef]] = defaultdict(list)
    for l in layers:
        by_peril[l.peril_id].append(l)

    total_loss = defaultdict(float)
    attach_years = defaultdict(int)
    exhaust_years = defaultdict(int)
    event_count = 0

    for year_events in simulate(CALIBRATION_SEED, MVP_PARAMS, years):
        event_count += len(year_events)
        year_loss: dict[int, float] = defaultdict(float)

        for ev in year_events:
            for layer in by_peril[ev.peril_id]:
                gross = min(max(ev.subject_loss - layer.attachment, 0.0), layer.limit)
                if gross <= 0:
                    continue
                # cumulative erosion within the year
                room = layer.limit - year_loss[layer.layer_id]
                year_loss[layer.layer_id] += min(gross, room)

        for lid, loss in year_loss.items():
            if loss > 0:
                total_loss[lid] += loss
                attach_years[lid] += 1
                if loss >= next(l.limit for l in layers if l.layer_id == lid) - 1e-9:
                    exhaust_years[lid] += 1

    results = {}
    for l in layers:
        el = total_loss[l.layer_id] / years / l.limit
        results[l.layer_id] = {
            "name": l.name,
            "expected_loss": el,
            "attach_prob": attach_years[l.layer_id] / years,
            "exhaust_prob": exhaust_years[l.layer_id] / years,
            "rate_on_line": el * TRANCHE_MULTIPLES[l.tranche],
        }

    return layers, {"results": results, "events_per_year": event_count / years}


def check(layers, results) -> list[str]:
    problems = []
    for l in layers:
        el = results[l.layer_id]["expected_loss"]
        if l.tranche == 0 and not (0.05 <= el <= 0.10):
            problems.append(f"{l.name}: junior EL {el:.2%} outside 5-10%")
        if l.tranche == 2 and not (0.005 <= el <= 0.015):
            problems.append(f"{l.name}: senior EL {el:.2%} outside 0.5-1.5%")
    for peril_id in {l.peril_id for l in layers}:
        els = [results[l.layer_id]["expected_loss"]
               for l in sorted((x for x in layers if x.peril_id == peril_id),
                               key=lambda x: x.tranche)]
        if not (els[0] > els[1] > els[2]):
            problems.append(f"peril {peril_id}: EL not monotonic across tranches")
    return problems


def main():
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    layers, out = calibrate(years)
    results = out["results"]

    print(f"\nCalibration: {years:,} simulated years")
    print(f"Seed:   0x{CALIBRATION_SEED.hex()}")
    print(f"Params: {MVP_PARAMS.param_hash()}")
    print(f"Events/year: {out['events_per_year']:.3f}\n")

    hdr = f"{'Layer':<20}{'Attach':>8}{'Limit':>8}{'EL':>9}{'P(att)':>9}{'P(exh)':>9}{'ROL':>9}"
    print(hdr)
    print("-" * len(hdr))
    for l in layers:
        r = results[l.layer_id]
        print(f"{r['name']:<20}{l.attachment:>8.0f}{l.limit:>8.0f}"
              f"{r['expected_loss']:>8.2%}{r['attach_prob']:>9.2%}"
              f"{r['exhaust_prob']:>9.2%}{r['rate_on_line']:>8.2%}")

    problems = check(layers, results)
    print()
    if problems:
        print("ACCEPTANCE FAILED:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("ACCEPTANCE PASSED — table may be frozen.")

    table = {
        "calibration_seed": "0x" + CALIBRATION_SEED.hex(),
        "param_hash": MVP_PARAMS.param_hash(),
        "years": years,
        "accepted": not problems,
        "layers": [
            {**asdict(l), "limit": l.limit, "name": l.name,
             "technical_el": results[l.layer_id]["expected_loss"],
             "rate_on_line_bps": round(results[l.layer_id]["rate_on_line"] * 10_000),
             "tranche_multiple": TRANCHE_MULTIPLES[l.tranche]}
            for l in layers
        ],
    }
    with open("layer_table.json", "w") as f:
        json.dump(table, f, indent=2)
    print("\nWrote layer_table.json")


if __name__ == "__main__":
    main()
