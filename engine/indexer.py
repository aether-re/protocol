"""
Aether Re — dashboard indexer.

Pulls event logs and current state from X Layer and writes dashboard/data.json.

The public X Layer RPC caps eth_getLogs at a 100-block range, so a full rescan
costs one request per 100 blocks per event type and grows with the chain head.
Raw logs are cached in dashboard/_cache.json and only new blocks are fetched on
each run. The first run is slow; every run after is seconds.

    python3 indexer.py            index new blocks once
    python3 indexer.py --watch    keep indexing every 30s
    python3 indexer.py --reset    discard cache and rescan from DEPLOY_BLOCK
"""

from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

from web3 import Web3

RPC = "https://testrpc.xlayer.tech/terigon"
CHUNK = 100                      # hard cap on this RPC
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dashboard" / "data.json"
CACHE = ROOT / "dashboard" / "_cache.json"

PERIL_NAMES = {0: "FL_WIND", 1: "GULF_WIND", 2: "EU_WIND"}
REGION_NAMES = {0: "US Southeast", 1: "Europe"}
TRANCHE_NAMES = {0: "Junior", 1: "Mezz", 2: "Senior"}
STATE_NAMES = {0: "active", 1: "exhausted", 2: "expired"}
SCALE = 10**9
USDC = 10**6
SEED_CAPITAL = 3_200_000

SOURCES = [
    ("Settlement", "EpochSettled"),
    ("Settlement", "LayerCommitted"),
    ("EventOracle", "EventPublished"),
    ("AgentLog", "ForecastPublished"),
    ("AgentLog", "ForecastResolved"),
]


def read_env() -> dict:
    env = {}
    for line in (ROOT / "contracts" / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def abi(name: str):
    return json.loads(
        (ROOT / "contracts" / "out" / f"{name}.sol" / f"{name}.json").read_text()
    )["abi"]


def load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except Exception:
            pass
    return {"lastBlock": 0, "logs": {}}


def fetch_range(contract, event_name: str, lo: int, hi: int, label: str):
    """One request per CHUNK blocks. Prints progress; a cold scan is slow."""
    out = []
    total = hi - lo + 1
    done = 0
    cur = lo
    while cur <= hi:
        top = min(cur + CHUNK - 1, hi)
        try:
            for e in getattr(contract.events, event_name)().get_logs(
                from_block=cur, to_block=top
            ):
                row = {k: (v.hex() if isinstance(v, (bytes, bytearray)) else v)
                       for k, v in dict(e["args"]).items()}
                row["_block"] = e["blockNumber"]
                out.append(row)
        except Exception as e:
            print(f"\n  warn {label} {cur}-{top}: {str(e)[:70]}")
        done += top - cur + 1
        cur = top + 1
        pct = 100 * done // max(1, total)
        sys.stdout.write(f"\r  {label:<20} {pct:>3}%  ({len(out)} new)")
        sys.stdout.flush()
    print()
    return out


def refresh_cache(w3, contracts, env, reset: bool) -> dict:
    cache = {"lastBlock": 0, "logs": {}} if reset else load_cache()
    head = w3.eth.block_number
    deploy = int(env.get("DEPLOY_BLOCK", head - 10_000))
    start = max(deploy, cache["lastBlock"] + 1)

    if start > head:
        print(f"  cache current at block {cache['lastBlock']}")
        return cache

    print(f"  fetching blocks {start} to {head} ({head - start + 1} blocks)")
    for cname, ename in SOURCES:
        key = f"{cname}.{ename}"
        fresh = fetch_range(contracts[cname], ename, start, head, ename)
        cache["logs"].setdefault(key, []).extend(fresh)

    cache["lastBlock"] = head
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache))
    return cache


def build(w3, contracts, env, cache: dict) -> dict:
    logs = cache["logs"]
    settlement = contracts["Settlement"]
    registry = contracts["RiskLayerRegistry"]
    vault = contracts["AgentVault"]
    cedent = contracts["SimCedent"]
    oracle = contracts["EventOracle"]
    alog = contracts["AgentLog"]

    raw = sorted(logs.get("Settlement.EpochSettled", []), key=lambda x: x["epoch"])
    seen = set()
    epochs = []
    cum_premium = cum_losses = 0
    for a in raw:
        if a["epoch"] in seen:
            continue
        seen.add(a["epoch"])
        cum_premium += a["premium"]
        cum_losses += a["losses"]
        epochs.append({
            "epoch": a["epoch"],
            "premium": a["premium"] / USDC,
            "losses": a["losses"] / USDC,
            "released": a["released"] / USDC,
            "nav": a["nav"] / USDC,
            "cumPremium": cum_premium / USDC,
            "cumLosses": cum_losses / USDC,
        })

    cats = [{
        "id": a["eventId"],
        "epoch": a["epoch"],
        "peril": a["peril"],
        "perilName": PERIL_NAMES.get(a["peril"], str(a["peril"])),
        "subjectLoss": a["subjectLoss"] / SCALE,
    } for a in sorted(logs.get("EventOracle.EventPublished", []),
                      key=lambda x: (x["epoch"], x["eventId"]))]

    forecasts: dict[int, dict] = {}
    for a in logs.get("AgentLog.ForecastPublished", []):
        forecasts[a["epoch"]] = {
            "epoch": a["epoch"],
            "predictedBps": a["expectedLossBps"],
            "confidenceBps": a["confidenceBps"],
            "rationale": a["rationale"],
            "realizedBps": None,
            "resolved": False,
        }
    for a in logs.get("AgentLog.ForecastResolved", []):
        if a["epoch"] in forecasts:
            forecasts[a["epoch"]]["realizedBps"] = a["realizedLossBps"]
            forecasts[a["epoch"]]["resolved"] = True

    commits = [{
        "epoch": a["epoch"],
        "layerId": a["layerId"],
        "linePercent": a["linePercent"] / 100,
        "collateral": a["collateral"] / USDC,
    } for a in sorted(logs.get("Settlement.LayerCommitted", []),
                      key=lambda x: (x["epoch"], x["layerId"]))]

    layers = []
    for i in range(registry.functions.layerCount().call()):
        l = registry.functions.get(i).call()
        layers.append({
            "id": i,
            "peril": l[0],
            "perilName": PERIL_NAMES.get(l[0], str(l[0])),
            "region": l[1],
            "regionName": REGION_NAMES.get(l[1], str(l[1])),
            "tranche": l[2],
            "trancheName": TRANCHE_NAMES[l[2]],
            "name": f"{PERIL_NAMES.get(l[0])}_{TRANCHE_NAMES[l[2]].upper()}",
            "attachment": l[3] / SCALE,
            "exhaustion": l[4] / SCALE,
            "limit": (l[4] - l[3]) / SCALE,
            "technicalElBps": l[5],
            "rateOnLineBps": l[6],
            "linePercent": l[7] / 100,
            "collateralPosted": l[8] / USDC,
            "collateralRemaining": l[9] / USDC,
            "accruedPremium": l[10] / USDC,
            "termStart": l[11],
            "termEnd": l[12],
            "state": STATE_NAMES[l[13]],
            "erosion": (1 - l[9] / l[8]) if l[8] else 0,
        })

    nav = vault.functions.totalAssets().call()
    idle = vault.functions.idleAssets().call()
    resolved = [f for f in forecasts.values() if f["resolved"]]

    return {
        "generatedAt": int(time.time()),
        "chain": {"id": 1952, "name": "X Layer testnet", "head": cache["lastBlock"]},
        "contracts": {k: env.get(k, "") for k in
                      ("VAULT", "REGISTRY", "SETTLEMENT", "ORACLE",
                       "CEDENT", "AGENTLOG", "USDC")},
        "verification": {
            "seedCommitment": "0x" + oracle.functions.seedCommitment().call().hex(),
            "paramHash": "0x" + oracle.functions.paramHash().call().hex(),
            "revealed": oracle.functions.revealed().call(),
            "eventCount": oracle.functions.eventCount().call(),
        },
        "vault": {
            "epoch": settlement.functions.epoch().call(),
            "nav": nav / USDC,
            "idle": idle / USDC,
            "deployed": (nav - idle) / USDC,
            "seedCapital": SEED_CAPITAL,
            "totalReturn": (nav / USDC) / SEED_CAPITAL - 1,
            "cumPremium": cum_premium / USDC,
            "cumLosses": cum_losses / USDC,
            "lossRatio": (cum_losses / cum_premium) if cum_premium else 0,
        },
        "cedent": {
            "balance": cedent.functions.balance().call() / USDC,
            "iou": cedent.functions.iou().call() / USDC,
        },
        "calibration": {
            "ratioBps": alog.functions.calibrationRatioBps().call(),
            "resolvedCount": alog.functions.resolvedCount().call(),
            "eventQuarterCount": sum(1 for f in resolved if f["epoch"] % 4 == 2),
        },
        "layers": layers,
        "epochs": epochs,
        "catastrophes": cats,
        "forecasts": sorted(forecasts.values(), key=lambda f: f["epoch"]),
        "commits": commits,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()

    env = read_env()
    w3 = Web3(Web3.HTTPProvider(RPC))
    A = w3.to_checksum_address
    contracts = {
        "Settlement": w3.eth.contract(A(env["SETTLEMENT"]), abi=abi("Settlement")),
        "RiskLayerRegistry": w3.eth.contract(A(env["REGISTRY"]), abi=abi("RiskLayerRegistry")),
        "AgentVault": w3.eth.contract(A(env["VAULT"]), abi=abi("AgentVault")),
        "SimCedent": w3.eth.contract(A(env["CEDENT"]), abi=abi("SimCedent")),
        "EventOracle": w3.eth.contract(A(env["ORACLE"]), abi=abi("EventOracle")),
        "AgentLog": w3.eth.contract(A(env["AGENTLOG"]), abi=abi("AgentLog")),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    reset = args.reset

    while True:
        try:
            cache = refresh_cache(w3, contracts, env, reset)
            reset = False
            data = build(w3, contracts, env, cache)
            OUT.write_text(json.dumps(data, indent=1))
            v = data["vault"]
            print(f"  epoch {v['epoch']}  NAV {v['nav']:,.0f} USDC  "
                  f"{len(data['epochs'])} epochs  {len(data['catastrophes'])} cats  "
                  f"{len(data['forecasts'])} forecasts")
        except Exception as e:
            print(f"  ERROR: {e}")
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
