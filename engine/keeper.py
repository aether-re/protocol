"""
Aether Re — keeper.

Drives the on-chain epoch cycle:

    1. generate this epoch's events from the committed seed
    2. publish them to EventOracle
    3. call Settlement.settleEpoch
    4. run the allocator over renewable layers
    5. call Settlement.renew

Events come from the same simulator the calibration used, seeded with the
secret whose keccak256 is already committed on-chain. Nothing here can change
the event stream: the seed is fixed and the parameters are hashed.

    python3 keeper.py --once        run a single epoch and stop
    python3 keeper.py               run continuously
    python3 keeper.py --status      print state, send nothing
"""

from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from simulator import MVP_PARAMS, simulate_year
from market import (
    BPS, SCALE, MIN_BUFFER_BPS, MAX_JUNIOR_BPS, MAX_REGION_BPS, MAX_LINE_BPS,
    TERM_EPOCHS, EPOCHS_PER_YEAR, LayerDef, load_layers,
)
import math

RPC = "https://testrpc.xlayer.tech/terigon"
CHAIN_ID = 1952
EPOCH_SECONDS = 30 * 60          # 30 min wall clock == 1 simulated quarter
EVENT_QUARTER = 2                # events land in Q3 of each simulated year

ROOT = Path(__file__).resolve().parent.parent


# --- config -----------------------------------------------------------------

def load_env() -> dict:
    env = {}
    for name in (".env", ".seed"):
        p = ROOT / "contracts" / name
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    pk = os.environ.get("PRIVATE_KEY")
    if not pk:
        sys.exit("PRIVATE_KEY not set. Run: export PRIVATE_KEY=0x...")
    env["PRIVATE_KEY"] = pk
    for required in ("SETTLEMENT", "ORACLE", "REGISTRY", "VAULT", "SEED"):
        if required not in env:
            sys.exit(f"{required} missing from contracts/.env or contracts/.seed")
    return env


def load_abis() -> dict:
    out = {}
    for name in ("Settlement", "EventOracle", "RiskLayerRegistry", "AgentVault"):
        p = ROOT / "contracts" / "out" / f"{name}.sol" / f"{name}.json"
        out[name] = json.loads(p.read_text())["abi"]
    return out


# --- allocator --------------------------------------------------------------

def offered_rol_bps(d: LayerDef, year: int, dislocation_fn) -> int:
    return max(1, round(d.technical_el * d.tranche_multiple * dislocation_fn(year, d.layer_id) * BPS))


def choose_lines(defs_by_id, layers_on_chain, nav, idle, year, dislocation_fn):
    """Agent v1: risk-adjusted margin, correlation-penalised.

    Mirrors alloc_agent_v0 in market.py, which beat equal-weight and yield-max
    on return, drawdown, and Sharpe over 95 simulated years.
    """
    budget = max(0, idle - (nav * MIN_BUFFER_BPS) // BPS)
    if budget <= 0:
        return {}

    deployed = sum(l["collateralRemaining"] for l in layers_on_chain.values()
                   if l["state"] != 2) or 1
    by_region: dict[int, int] = {}
    junior = 0
    for lid, l in layers_on_chain.items():
        if l["state"] == 2:
            continue
        d = defs_by_id[lid]
        by_region[d.region_id] = by_region.get(d.region_id, 0) + l["collateralRemaining"]
        if d.tranche == 0:
            junior += l["collateralRemaining"]

    candidates = [lid for lid, l in layers_on_chain.items() if l["state"] == 2]
    if not candidates:
        return {}

    def score(lid: int) -> float:
        d = defs_by_id[lid]
        margin = offered_rol_bps(d, year, dislocation_fn) / BPS - d.technical_el
        region_share = by_region.get(d.region_id, 0) / deployed
        return (margin / math.sqrt(d.technical_el)) * (1.0 - 0.8 * region_share)

    total_after = deployed + budget
    out = {}
    for lid in sorted(candidates, key=score, reverse=True):
        if score(lid) <= 0:
            continue
        d = defs_by_id[lid]
        lim = layers_on_chain[lid]["exhaustion"] - layers_on_chain[lid]["attachment"]
        caps = [budget, (lim * MAX_LINE_BPS) // BPS]
        if d.tranche == 0:
            caps.append(max(0, (total_after * MAX_JUNIOR_BPS) // BPS - junior))
        caps.append(max(0, (total_after * MAX_REGION_BPS) // BPS - by_region.get(d.region_id, 0)))
        collateral = max(0, min(caps))
        line_bps = min(MAX_LINE_BPS, (collateral * BPS) // lim) if lim else 0
        if line_bps < 100:          # skip dust: sub-1% lines waste a renewal slot
            continue
        actual = (lim * line_bps) // BPS
        out[lid] = line_bps
        budget -= actual
        by_region[d.region_id] = by_region.get(d.region_id, 0) + actual
        if d.tranche == 0:
            junior += actual
    return out


# --- chain helpers ----------------------------------------------------------

class Keeper:
    def __init__(self):
        env = load_env()
        abis = load_abis()
        self.w3 = Web3(Web3.HTTPProvider(RPC))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.acct = self.w3.eth.account.from_key(env["PRIVATE_KEY"])
        self.seed = bytes.fromhex(env["SEED"].removeprefix("0x"))

        A = self.w3.to_checksum_address
        self.settlement = self.w3.eth.contract(A(env["SETTLEMENT"]), abi=abis["Settlement"])
        self.oracle = self.w3.eth.contract(A(env["ORACLE"]), abi=abis["EventOracle"])
        self.registry = self.w3.eth.contract(A(env["REGISTRY"]), abi=abis["RiskLayerRegistry"])
        self.vault = self.w3.eth.contract(A(env["VAULT"]), abi=abis["AgentVault"])

        self.defs = load_layers(str(ROOT / "engine" / "layer_table.json"))
        self.defs_by_id = {d.layer_id: d for d in self.defs}

    def send(self, fn, label: str):
        tx = fn.build_transaction({
            "from": self.acct.address,
            "nonce": self.w3.eth.get_transaction_count(self.acct.address),
            "chainId": CHAIN_ID,
            "gas": 8_000_000,
            "gasPrice": self.w3.eth.gas_price,
        })
        signed = self.acct.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        r = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
        status = "ok" if r.status == 1 else "REVERTED"
        print(f"  {label}: {status}  gas={r.gasUsed:,}  {h.hex()[:18]}")
        return r

    def read_layers(self) -> dict:
        out = {}
        n = self.registry.functions.layerCount().call()
        for i in range(n):
            l = self.registry.functions.get(i).call()
            out[i] = {
                "peril": l[0], "region": l[1], "tranche": l[2],
                "attachment": l[3], "exhaustion": l[4],
                "technicalELBps": l[5], "rateOnLine": l[6], "linePercent": l[7],
                "collateralPosted": l[8], "collateralRemaining": l[9],
                "accruedPremium": l[10], "termStart": l[11], "termEnd": l[12],
                "state": l[13],
            }
        return out

    def events_for_epoch(self, epoch: int):
        """Only Q3 of each simulated year carries catastrophes."""
        if epoch % EPOCHS_PER_YEAR != EVENT_QUARTER:
            return [], []
        year = epoch // EPOCHS_PER_YEAR
        evs = simulate_year(self.seed, MVP_PARAMS, year)
        perils = [e.peril_id for e in evs]
        losses = [int(e.subject_loss * SCALE) for e in evs]
        return perils, losses

    def dislocation_fn(self, year, layer_id):
        from simulator import dislocation
        return dislocation(self.seed, MVP_PARAMS, year, layer_id)

    def status(self):
        epoch = self.settlement.functions.epoch().call()
        nav = self.vault.functions.totalAssets().call()
        idle = self.vault.functions.idleAssets().call()
        layers = self.read_layers()
        names = ("ACTIVE", "EXHAUSTED", "EXPIRED")
        print(f"\nepoch {epoch}   NAV {nav/1e6:,.2f} USDC   idle {idle/1e6:,.2f}")
        for i, l in layers.items():
            d = self.defs_by_id[i]
            print(f"  [{i}] {d.name:<18} {names[l['state']]:<10} "
                  f"line {l['linePercent']/100:>5.1f}%  "
                  f"coll {l['collateralRemaining']/1e6:>10,.0f}  "
                  f"prem {l['accruedPremium']/1e6:>8,.0f}  term->{l['termEnd']}")

    def run_epoch(self):
        epoch = self.settlement.functions.epoch().call()
        year = epoch // EPOCHS_PER_YEAR
        perils, losses = self.events_for_epoch(epoch)
        print(f"\n=== epoch {epoch} (sim year {year}) — {len(perils)} event(s) ===")

        if perils:
            for p, s in zip(perils, losses):
                print(f"  event: peril {p}  subject loss {s/SCALE:,.0f} notional")
            self.send(self.oracle.functions.publish(epoch, perils, losses), "publish")

        self.send(self.settlement.functions.settleEpoch(perils, losses), "settleEpoch")

        nav = self.vault.functions.totalAssets().call()
        idle = self.vault.functions.idleAssets().call()
        layers = self.read_layers()
        lines = choose_lines(self.defs_by_id, layers, nav, idle, year, self.dislocation_fn)

        if lines:
            ids = sorted(lines)
            for lid in ids:
                print(f"  renew [{lid}] {self.defs_by_id[lid].name} at {lines[lid]/100:.1f}%")
            self.send(
                self.settlement.functions.renew(ids, [lines[i] for i in ids]),
                "renew",
            )
        else:
            print("  no renewals this epoch")

        print(f"  NAV {nav/1e6:,.2f} USDC")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--interval", type=int, default=EPOCH_SECONDS)
    args = ap.parse_args()

    k = Keeper()
    print(f"keeper {k.acct.address}  ->  settlement {k.settlement.address}")

    if args.status:
        k.status()
        return
    if args.once:
        k.run_epoch()
        return

    while True:
        try:
            k.run_epoch()
        except Exception as e:
            print(f"  ERROR: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
