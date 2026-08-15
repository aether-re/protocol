# Aether Re

**An autonomous agent that underwrites catastrophe reinsurance on X Layer.**

[Live dashboard](https://aether-re.xnoxseyi.workers.dev/) · [@aether_re_](https://x.com/aether_re_) · X Layer testnet, chain 1952

---

Insurance companies buy insurance. When a hurricane wrecks fifty thousand homes in
Florida, the local insurer cannot pay every claim alone, so it has bought cover from
someone larger. That cover is sold in **layers**: one contract pays the slice of losses
between $576M and $1.3B, another the slice above it. Whoever posts capital against a
layer keeps the premium if no storm comes, and loses the capital if one does.

Aether Re is an agent that decides which layers are worth backing.

A simulated insurer buys cover from nine collateralised layers across three perils and
pays premium every epoch. When a catastrophe breaches a layer, it claims against that
layer's collateral. The vault's yield is therefore an accounting identity:

```
yield = premiums collected − losses paid
```

There is no other source. Nothing is minted, subsidised, or assumed.

---

## The part that is different

Catastrophe events here are simulated. That is normally where a project asks you to
trust it. Instead:

**The seed was committed before the run.** The random seed and every simulation
parameter were hashed into the `EventOracle` constructor before the first storm was
published. The seed stays sealed until the run ends. On reveal, one command regenerates
the entire event stream and checks it against what was published on-chain.

```
python3 engine/replay.py

  [PASS] seed matches commitment
  [PASS] parameters match committed hash
  39 events reproduced exactly from the committed seed
```

Both hashes matter. Committing the seed alone would leave us free to retune frequency
and severity afterwards.

| | |
|---|---|
| Seed commitment | `0x097c38be17afb6d93d776fc460c702c96b48f775bd1dcf8c8170093225c3a458` |
| Parameter hash | `0x6988eb75f0204da500c0e360b73e1521851d6d811d6e30afafc96acc42b3ec6f` |

**The agent's forecasts are falsifiable.** Before each epoch settles, the agent writes
an expected-loss forecast to `AgentLog`. After settlement the realised loss is written
against it. Neither can be revised: `publishForecast` reverts if a forecast already
exists for that epoch, `resolveForecast` reverts if already resolved.

---

## What the numbers actually say

Over 30 simulated years on testnet, across 25 scored event quarters:

| | |
|---|---|
| Cumulative predicted loss | 46.6% of NAV |
| Cumulative realised loss | 75.9% of NAV |
| Ratio | 0.61× — the agent **under**-predicts by 39% |
| Max drawdown | 44.5% |

And the figure worth more than any of those: **three quarters account for 97% of all
loss paid.** Remove them and the same agent, over the same window, looks like it
over-predicts by 18.8×.

That is catastrophe risk. The mean sits far above the median, so a well-calibrated
agent looks wrong in most quarters and gets it back in the rare bad one. Twenty-five
quarters is nowhere near enough to judge calibration, and we say so on the dashboard
rather than presenting the flattering framing.

We can make that argument honestly only because every forecast was timestamped before
its outcome. A backtest lets you choose which framing to show.

---

## How the layers were built

Attachment points were not chosen by hand. `engine/tower.py` simulates 100,000 years
from the committed parameters and reads attachment points off the occurrence exceedance
curve at **1-in-8, 1-in-25, 1-in-70 and 1-in-300** return periods, which is how real
catastrophe towers are structured. Expected loss falls out of the structure and sets
the pricing.

An earlier hand-picked table put senior tranches at 0.16–0.27% expected loss against a
0.5–1.5% target. The calibration run caught it.

| Peril | Region | λ / yr | Severity median | σ |
|---|---|---|---|---|
| FL_WIND | US Southeast | 0.60 | $180M | 0.90 |
| GULF_WIND | US Southeast | 0.50 | $150M | 0.85 |
| EU_WIND | Europe | 0.80 | $90M | 0.70 |

Florida and the Gulf share a regional season factor, so they co-move: measured
correlation of annual aggregate loss is **+0.42** within the region and **+0.00**
across. Europe is a genuine diversifier. An allocator that chases yield stacks into
both Atlantic perils and periodically detonates — in backtest it finished worst of
three despite chasing the highest headline rates.

The agent scores each layer on risk-adjusted margin, `margin / sqrt(expectedLoss)`,
with a penalty proportional to existing exposure in the same region. It beat
equal-weight and yield-max on return, drawdown and Sharpe over an identical event
stream.

---

## Constraints

Enforced in the keeper, visible in the decision log:

- Idle buffer ≥ 12% of NAV
- No single layer above 20% of NAV
- Junior tranches ≤ 35% of deployed capital
- Any one region ≤ 60% of deployed capital

Caps bind **at commitment**. Collateral committed to a live layer cannot be withdrawn
mid-term, which is true of real collateralised reinsurance, so concentration can drift
above a cap as other positions expire. The agent cannot unwind; it can only decline to
add. That is a property of the instrument and we do not paper over it.

---

## Contracts

X Layer testnet, chain 1952.

| Contract | Address |
|---|---|
| AgentVault | `0xDC78557b332B1AF7e157ab91D34f432F30481a53` |
| RiskLayerRegistry | `0xA1a61FA6528C72aE3e2515472BDB18FCa2F53106` |
| Settlement | `0x61789cD68720a9d3F10e0d0A439D94E61bF1d47F` |
| SimCedent | `0x022d34dB48Acde4214900df04fA2795bd6DF1d88` |
| EventOracle | `0x1700AE41EFd9Ce17A3b92EEb10D4470aF6E85675` |
| AgentLog | `0x8dC02883614009e0D93C39C5A21f0ce8c7d344B1` |
| MockUSDC | `0x82B7CE5992F87Ae64537d708d39Bd233B7aA7cfb` |

`Settlement.sol` is a direct port of `engine/settlement.py`. The Python version is the
reference implementation and carries a conservation assert: vault plus cedent value
must be identical before and after every epoch, since premium and losses only move
value between two sides of a closed system. Any change that leaks value fails loudly
instead of showing up as a NAV that is quietly 3% low.

---

## Running it

```bash
# reference engine and simulation
cd engine
python3 -m venv .venv && source .venv/bin/activate
pip install pycryptodome pytest web3

python3 -m pytest -q        # settlement waterfall, 17 tests
python3 tower.py 100000     # rebuild and re-verify the layer table
python3 market.py 95        # agent vs equal-weight vs yield-max
python3 replay.py           # verify published events against the seed

# contracts
cd ../contracts
forge build && forge test -vv
```

The keeper (`engine/keeper.py`) drives the epoch cycle: generate events from the
committed seed, publish to the oracle, settle, allocate, publish the next forecast.
The indexer (`engine/indexer.py`) pulls chain logs into `dashboard/data.json`.

---

## Status, plainly

This runs on X Layer testnet with a simulated counterparty and simulated catastrophes,
labelled as such throughout the interface. **It does not accept real money and you
cannot invest in it.**

Making it real needs three module swaps and one thing that is not a module swap:

| Testnet | Mainnet |
|---|---|
| `EventOracle` (Monte Carlo) | Parametric oracle — NOAA wind speed, USGS magnitude |
| `SimCedent` | A real cover buyer: captive, MGA, or parametric counterparty |
| Calibrated synthetic terms | Real quoted terms |

The thing that is not a module swap: accepting deposits against insurance-shaped risk
sits at the intersection of securities regulation, reinsurance licensing and money
transmission. Mainnet deployment uses the same contracts with the simulated
counterparty. Real capital waits on counsel and a real cedent, and we are not going to
skip that.

---

## Layout

```
engine/     simulator, settlement reference, calibration, agent, keeper, replay
contracts/  Solidity, Foundry
dashboard/  static front end, reads data.json
brand/      mark, avatar, banner
```

Built for the X Layer AI Season hackathon, AI-RWA track.
