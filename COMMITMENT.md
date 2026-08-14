# Aether Re — Pre-Run Commitment

Catastrophe events in this system are **simulated**. To make that verifiable
rather than something you have to take on trust, the random seed and the full
simulation parameter set are committed here *before* any event is published.

The seed is revealed when the run ends. Anyone can then replay the simulation
and confirm the on-chain event stream matches — proving the losses were not
tuned to flatter the returns.

## Commitments

| | |
|---|---|
| Seed commitment | `0x097c38be17afb6d93d776fc460c702c96b48f775bd1dcf8c8170093225c3a458` |
| Parameter hash | `0x6988eb75f0204da500c0e360b73e1521851d6d811d6e30afafc96acc42b3ec6f` |
| Network | X Layer testnet (chain 1952) |

## Contracts

| Contract | Address |
|---|---|
| EventOracle | `0x1700AE41EFd9Ce17A3b92EEb10D4470aF6E85675` |
| RiskLayerRegistry | `0xA1a61FA6528C72aE3e2515472BDB18FCa2F53106` |
| AgentVault | `0xDC78557b332B1AF7e157ab91D34f432F30481a53` |
| Settlement | `0x61789cD68720a9d3F10e0d0A439D94E61bF1d47F` |
| SimCedent | `0x022d34dB48Acde4214900df04fA2795bd6DF1d88` |
| MockUSDC | `0x82B7CE5992F87Ae64537d708d39Bd233B7aA7cfb` |

Both hashes are stored immutably in the EventOracle constructor and can be
read on-chain:

    cast call 0x1700AE41EFd9Ce17A3b92EEb10D4470aF6E85675 \
      "seedCommitment()(bytes32)" --rpc-url https://testrpc.xlayer.tech/terigon

## Parameters (frozen)

Perils, with regional season factor sigma 0.60 and beta 1.00:

| Peril | Region | lambda | Severity median | Severity sigma |
|---|---|---|---|---|
| FL_WIND | US_SOUTHEAST | 0.60 | $180M | 0.90 |
| GULF_WIND | US_SOUTHEAST | 0.50 | $150M | 0.85 |
| EU_WIND | EUROPE | 0.80 | $90M | 0.70 |

Attachment points were set at 1-in-8 / 1-in-25 / 1-in-70 / 1-in-300 return
periods off the occurrence exceedance curve, calibrated over 100,000
simulated years. Full table in `engine/layer_table.json`.
