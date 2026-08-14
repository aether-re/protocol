"""
Aether Re — replay verifier.

Proves the catastrophe events published on-chain are exactly those implied by
the pre-committed seed and parameter set. Nothing was tuned after the fact.

What it checks:

  1. keccak256(seed) == the seedCommitment stored in EventOracle
  2. keccak256(parameter set) == the paramHash stored in EventOracle
  3. every event published on-chain matches the simulator's output for that
     epoch, event for event, to the base unit

Any mismatch fails loudly. A pass means the operator could not have shaped the
loss experience, because the commitment predates the first published event and
the seed reproduces the stream exactly.

    python3 replay.py                 verify using the on-chain revealed seed
    python3 replay.py --seed 0x...    verify a seed supplied directly
    python3 replay.py --local         use contracts/.seed (operator self-check)
"""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from web3 import Web3

from simulator import MVP_PARAMS, simulate_year, keccak256
from market import SCALE, EPOCHS_PER_YEAR

RPC = "https://testrpc.xlayer.tech/terigon"
EVENT_QUARTER = 2
ROOT = Path(__file__).resolve().parent.parent

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def read_env(key: str, filename: str) -> str | None:
    p = ROOT / "contracts" / filename
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", help="0x-prefixed 32-byte seed")
    ap.add_argument("--local", action="store_true", help="read seed from contracts/.seed")
    ap.add_argument("--oracle", help="EventOracle address")
    args = ap.parse_args()

    oracle_addr = args.oracle or read_env("ORACLE", ".env")
    if not oracle_addr:
        sys.exit("EventOracle address not found. Pass --oracle 0x...")

    w3 = Web3(Web3.HTTPProvider(RPC))
    abi = json.loads(
        (ROOT / "contracts" / "out" / "EventOracle.sol" / "EventOracle.json").read_text()
    )["abi"]
    oracle = w3.eth.contract(w3.to_checksum_address(oracle_addr), abi=abi)

    commitment = oracle.functions.seedCommitment().call()
    param_hash = oracle.functions.paramHash().call()
    revealed = oracle.functions.revealed().call()

    print(f"\nEventOracle {oracle_addr}")
    print(f"  seed commitment  0x{commitment.hex()}")
    print(f"  parameter hash   0x{param_hash.hex()}")
    print(f"  revealed         {revealed}")

    # --- resolve the seed --------------------------------------------------

    if args.seed:
        seed_hex = args.seed
        source = "supplied"
    elif args.local:
        seed_hex = read_env("SEED", ".seed")
        source = "contracts/.seed (operator self-check, NOT proof)"
        if not seed_hex:
            sys.exit("no SEED in contracts/.seed")
    elif revealed:
        seed_hex = "0x" + oracle.functions.revealedSeed().call().hex()
        source = "on-chain reveal"
    else:
        sys.exit(
            "\nSeed not yet revealed on-chain. The run is still in progress.\n"
            "Verification becomes possible once the operator calls reveal()."
        )

    seed = bytes.fromhex(seed_hex.removeprefix("0x"))
    print(f"  seed source      {source}")

    failures = []

    # --- check 1: seed matches the commitment ------------------------------

    computed = keccak256(seed)
    ok_seed = computed == commitment
    print(f"\n[{'PASS' if ok_seed else 'FAIL'}] seed matches commitment")
    if not ok_seed:
        print(f"       computed 0x{computed.hex()}")
        failures.append("seed does not match commitment")

    # --- check 2: parameters match the committed hash ----------------------

    computed_params = MVP_PARAMS.param_hash()
    ok_params = computed_params == "0x" + param_hash.hex()
    print(f"[{'PASS' if ok_params else 'FAIL'}] parameters match committed hash")
    if not ok_params:
        print(f"       computed {computed_params}")
        failures.append("parameter set does not match committed hash")

    if failures:
        print(f"\n{RED}VERIFICATION FAILED{RESET}")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    # --- check 3: replay every published event -----------------------------

    count = oracle.functions.eventCount().call()
    last_epoch = oracle.functions.lastEpoch().call()
    print(f"\nReplaying {count} published events across {last_epoch + 1} epochs\n")

    on_chain: dict[int, list[tuple[int, int]]] = {}
    for i in range(count):
        ev = oracle.functions.events(i).call()
        on_chain.setdefault(ev[0], []).append((ev[1], ev[2]))

    checked = mismatched = 0
    for epoch in range(last_epoch + 1):
        published = on_chain.get(epoch, [])

        if epoch % EPOCHS_PER_YEAR != EVENT_QUARTER:
            expected = []
        else:
            year = epoch // EPOCHS_PER_YEAR
            expected = [
                (e.peril_id, int(e.subject_loss * SCALE))
                for e in simulate_year(seed, MVP_PARAMS, year)
            ]

        if published == expected:
            checked += len(published)
            if published:
                print(f"  {GREEN}ok{RESET}   epoch {epoch:>4}  {len(published)} event(s)")
        else:
            mismatched += 1
            print(f"  {RED}FAIL{RESET} epoch {epoch:>4}")
            print(f"       on-chain {published}")
            print(f"       expected {expected}")

    print()
    if mismatched:
        print(f"{RED}VERIFICATION FAILED{RESET} — {mismatched} epoch(s) diverge")
        sys.exit(1)

    print(f"{GREEN}VERIFICATION PASSED{RESET}")
    print(f"  {checked} events reproduced exactly from the committed seed")
    print(f"  across {last_epoch + 1} epochs, no divergence")
    if args.local:
        print(f"\n{DIM}  Note: seed read locally. This is an operator self-check.")
        print(f"  Third-party proof requires the on-chain reveal.{RESET}")


if __name__ == "__main__":
    main()
