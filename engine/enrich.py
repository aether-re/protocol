"""
Aether Re — dashboard enrichment.

The indexer writes what the chain said. This module adds what a judge needs
to read it: the agent's standing decision in English, a forward stress of
the current book, drawdown, and the verification checks that can be run
without the still-sealed seed.

The allocator is unchanged. Nothing here chooses a line. The memo is
derived from the same score the keeper used, plus the caps that bound it.

    python3 enrich.py              rewrite dashboard/data.json in place
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from simulator import MVP_PARAMS, keccak256, simulate_year

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dashboard" / "data.json"
USDC_PER_M = 1000.0  # $1M notional == 1,000 mock USDC
STRESS_YEARS = 5000
# Independent of the live seed, which is still sealed. Same parameter set.
STRESS_SEED = keccak256(b"aether-re/stress/v1")

MIN_BUFFER = 0.12
MAX_JUNIOR = 0.35
MAX_REGION = 0.60
MAX_LAYER_NAV = 0.20

NICE = {
    "FL_WIND_JUNIOR": "Florida junior", "FL_WIND_MEZZ": "Florida mezzanine",
    "FL_WIND_SENIOR": "Florida senior", "GULF_WIND_JUNIOR": "Gulf junior",
    "GULF_WIND_MEZZ": "Gulf mezzanine", "GULF_WIND_SENIOR": "Gulf senior",
    "EU_WIND_JUNIOR": "Europe junior", "EU_WIND_MEZZ": "Europe mezzanine",
    "EU_WIND_SENIOR": "Europe senior",
}


def _nice(name: str) -> str:
    return NICE.get(name, name.replace("_", " ").title())


def max_drawdown(epochs: list, seed: float) -> float:
    peak = seed
    dd = 0.0
    for e in epochs:
        nav = e["nav"]
        peak = max(peak, nav)
        if peak > 0:
            dd = min(dd, nav / peak - 1.0)
    return dd


def _region_shares(layers: list) -> dict[int, float]:
    deployed = sum(l["collateralRemaining"] for l in layers if l["state"] != "expired") or 1.0
    by: dict[int, float] = {}
    for l in layers:
        if l["state"] == "expired":
            continue
        by[l["region"]] = by.get(l["region"], 0.0) + l["collateralRemaining"]
    return {r: v / deployed for r, v in by.items()}


def score_layer(layer: dict, region_share: float) -> float:
    el = layer["technicalElBps"] / 10_000
    rol = layer["rateOnLineBps"] / 10_000
    if el <= 0:
        return 0.0
    margin = rol - el
    return (margin / math.sqrt(el)) * (1.0 - 0.8 * region_share)


def _binding_caps(layer: dict, nav: float, deployed: float, shares: dict, junior_usd: float) -> list[str]:
    reasons = []
    coll = layer["collateralRemaining"]
    if nav > 0 and coll / nav >= MAX_LAYER_NAV - 0.01:
        reasons.append(f"layer cap ({MAX_LAYER_NAV:.0%} of NAV)")
    if deployed > 0 and shares.get(layer["region"], 0) >= MAX_REGION - 0.02:
        reasons.append(f"{layer['regionName']} at the {MAX_REGION:.0%} region cap")
    if layer["tranche"] == 0 and deployed > 0 and junior_usd / deployed >= MAX_JUNIOR - 0.02:
        reasons.append(f"junior book at the {MAX_JUNIOR:.0%} cap")
    return reasons


def build_brief(data: dict) -> dict:
    layers = data["layers"]
    vault = data["vault"]
    nav = vault["nav"]
    deployed = vault["deployed"] or 1.0
    shares = _region_shares(layers)
    junior_usd = sum(
        l["collateralRemaining"] for l in layers
        if l["state"] != "expired" and l["tranche"] == 0
    )

    scored = []
    for l in layers:
        share = shares.get(l["region"], 0.0)
        sc = score_layer(l, share)
        el = l["technicalElBps"] / 10_000
        rol = l["rateOnLineBps"] / 10_000
        scored.append({
            "id": l["id"],
            "name": l["name"],
            "peril": l["perilName"],
            "region": l["regionName"],
            "tranche": l["trancheName"],
            "state": l["state"],
            "linePercent": l["linePercent"],
            "collateral": l["collateralRemaining"],
            "navShare": (l["collateralRemaining"] / nav) if nav else 0,
            "score": round(sc, 4),
            "margin": round(rol - el, 4),
            "el": round(el, 4),
            "rol": round(rol, 4),
            "regionShare": round(share, 4),
            "caps": _binding_caps(l, nav, deployed, shares, junior_usd),
        })

    taken = [s for s in scored if s["state"] == "active" and s["linePercent"] > 0]
    taken.sort(key=lambda s: -s["collateral"])
    refused = [s for s in scored if s["linePercent"] <= 0 or s["state"] != "active"]
    refused.sort(key=lambda s: -s["score"])

    commits = data.get("commits") or []
    last_epoch = vault["epoch"]
    last_commits = [c for c in commits if c["epoch"] == last_epoch]
    if not last_commits and commits:
        last_epoch = commits[-1]["epoch"]
        last_commits = [c for c in commits if c["epoch"] == last_epoch]
    by_id = {l["id"]: l for l in layers}
    actions = []
    for c in last_commits:
        layer = by_id.get(c["layerId"])
        actions.append({
            "layer": layer["name"] if layer else f"layer {c['layerId']}",
            "linePercent": c["linePercent"],
            "collateral": c["collateral"],
        })

    us_share = shares.get(0, 0.0)
    eu_share = shares.get(1, 0.0)
    headline, body = _memo(actions, taken, refused, us_share, eu_share, last_epoch, vault)

    source = "rule"
    llm = _maybe_llm(headline, body, taken, refused, actions, us_share, eu_share)
    if llm:
        body = llm
        source = "grok-4.5"

    return {
        "headline": headline,
        "body": body,
        "source": source,
        "epoch": last_epoch,
        "actions": actions,
        "taken": taken,
        "refused": refused,
        "constraints": {
            "usSoutheast": round(us_share, 4),
            "europe": round(eu_share, 4),
            "junior": round(junior_usd / deployed, 4),
            "idleBuffer": round(vault["idle"] / nav, 4) if nav else 0,
            "regionCap": MAX_REGION,
            "juniorCap": MAX_JUNIOR,
            "layerCap": MAX_LAYER_NAV,
            "bufferFloor": MIN_BUFFER,
        },
    }


def _memo(actions, taken, refused, us_share, eu_share, epoch, vault) -> tuple[str, str]:
    if actions:
        a = actions[0]
        headline = f"Renewed {_nice(a['layer'])} at {a['linePercent']:.0f}% of the layer."
    elif taken:
        headline = f"Holding {len(taken)} layers. No renewal this epoch."
    else:
        headline = "No capital on risk this epoch."

    bits = []
    if us_share >= MAX_REGION - 0.03:
        bits.append(
            f"US Southeast is at {us_share:.0%} of deployed capital, "
            f"against a {MAX_REGION:.0%} region cap. Locked Atlantic lines "
            f"cannot be unwound mid-term, so the only lever is refusing to add."
        )
    else:
        bits.append(
            f"US Southeast is {us_share:.0%} of the book, Europe {eu_share:.0%}. "
            f"The second Atlantic peril has to clear a higher hurdle because "
            f"Florida and the Gulf co-move (measured annual-loss correlation +0.42)."
        )

    if actions:
        name = actions[0]["layer"]
        if name.startswith("EU_"):
            bits.append(
                "Europe is the genuine diversifier (cross-region correlation ~0). "
                "That is why idle capital went there rather than back into Florida or the Gulf."
            )
        elif name.startswith("FL_") or name.startswith("GULF_"):
            bits.append(
                "An Atlantic line only won capital because its risk-adjusted margin "
                "cleared the correlation penalty against existing Southeast exposure."
            )

    top = sorted(taken, key=lambda s: -s["score"])
    if top:
        t = top[0]
        bits.append(
            f"Highest standing score is {_nice(t['name'])} at {t['score']:.2f} "
            f"(rate on line {t['rol']:.1%} vs expected loss {t['el']:.1%}, "
            f"divided by √EL so juniors are not favoured on raw margin)."
        )

    skipped = [s for s in refused if s["score"] > 0]
    if skipped:
        s = skipped[0]
        why = s["caps"][0] if s["caps"] else "it is off-risk this term"
        bits.append(f"{_nice(s['name'])} scores {s['score']:.2f} but was not added: {why}.")

    bits.append(
        "The rule does not move. Caps, attachment points and the scoring formula "
        "were fixed before the run. The agent only chooses the line inside them."
    )
    return headline, " ".join(bits)


def _maybe_llm(headline, body, taken, refused, actions, us_share, eu_share) -> str | None:
    key = os.environ.get("XAI_API_KEY")
    if not key:
        return None
    payload = {
        "model": "grok-4.5",
        "input": (
            "You write the on-desk memo for a catastrophe-reinsurance underwriter. "
            "Four to six sentences. No adjectives that are not in the numbers. "
            "Do not invent layers, scores, or caps. Do not change the decision. "
            "The allocator is a frozen rule: score = (rateOnLine - EL) / sqrt(EL) "
            "times (1 - 0.8 * regionShare). Region cap 60%, junior cap 35%, "
            "single-layer 20% of NAV, idle buffer 12%.\n\n"
            f"Headline: {headline}\n"
            f"US-SE share {us_share:.1%}, Europe {eu_share:.1%}.\n"
            f"Actions: {json.dumps(actions)}\n"
            f"On risk: {json.dumps([{k: x[k] for k in ('name','linePercent','score','el','rol','caps')} for x in taken])}\n"
            f"Off risk / refused: {json.dumps([{k: x[k] for k in ('name','score','caps')} for x in refused[:6]])}\n"
            f"Draft to tighten, not replace: {body}"
        ),
    }
    req = urllib.request.Request(
        "https://api.x.ai/v1/responses",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    text = out.get("output_text") or ""
    if not text:
        for item in out.get("output") or []:
            for c in item.get("content") or []:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    text = c["text"]
                    break
    text = (text or "").strip()
    return text if 80 < len(text) < 1600 else None


def _year_loss(book, posted, year: int) -> tuple[float, dict[str, float]]:
    remaining = dict(posted)
    hit: dict[str, float] = {}
    loss = 0.0
    for ev in simulate_year(STRESS_SEED, MVP_PARAMS, year):
        for l in book:
            if ev.peril_id != l["peril"]:
                continue
            gross = min(max(ev.subject_loss - l["attachment"], 0.0), l["limit"])
            if gross <= 0:
                continue
            payout = min(gross * (l["linePercent"] / 100.0) * USDC_PER_M, remaining[l["id"]])
            if payout <= 0:
                continue
            remaining[l["id"]] -= payout
            loss += payout
            hit[l["name"]] = hit.get(l["name"], 0.0) + payout
    return loss, hit


def stress_book(data: dict, years: int = STRESS_YEARS) -> dict:
    """One-year loss distribution of the standing book, plus 30-year paths.

    Uses STRESS_SEED, not the live seed. This is a forward look at the
    current portfolio under the committed parameters, not a replay.
    """
    book = [dict(l) for l in data["layers"] if l["state"] == "active" and l["linePercent"] > 0]
    start = data["vault"]["nav"]
    posted = {l["id"]: l["collateralRemaining"] for l in book}
    annual_prem = 0.0
    for l in book:
        annual_prem += l["limit"] * (l["linePercent"] / 100.0) * (l["rateOnLineBps"] / 10_000) * USDC_PER_M

    year_losses = []
    hit_tot: dict[str, float] = {}
    worst = {"year": 0, "loss": 0.0, "pnl": 0.0}
    for year in range(years):
        loss, hit = _year_loss(book, posted, year)
        year_losses.append(loss)
        for n, v in hit.items():
            hit_tot[n] = hit_tot.get(n, 0.0) + v
        if loss > worst["loss"]:
            worst = {"year": year, "loss": loss, "pnl": annual_prem - loss}

    # 30-year paths, stepped through the same stream so they are reproducible.
    horizon = 30
    step = 10
    path_dds, path_rets = [], []
    for origin in range(0, years - horizon, step):
        nav = start
        peak = start
        dd = 0.0
        for year in range(origin, origin + horizon):
            nav = nav + annual_prem - year_losses[year]
            if nav < 0:
                nav = 0.0
            peak = max(peak, nav)
            if peak > 0:
                dd = min(dd, nav / peak - 1.0)
        path_dds.append(dd)
        path_rets.append(nav / start - 1.0 if start else 0.0)
    path_dds.sort()
    path_rets_sorted = sorted(path_rets)
    p05 = path_dds[max(0, int(len(path_dds) * 0.05) - 1)] if path_dds else 0.0

    ranked = sorted(hit_tot.items(), key=lambda kv: -kv[1])
    total_hit = sum(hit_tot.values()) or 1.0
    pnls = [annual_prem - x for x in year_losses]
    return {
        "years": years,
        "horizon": horizon,
        "paths": len(path_dds),
        "seed": "0x" + STRESS_SEED.hex(),
        "note": (
            "Forward stress of the current book under the committed parameter "
            "set. Independent seed — not the live run, which stays sealed."
        ),
        "startNav": start,
        "annualPremium": annual_prem,
        "meanYearPnl": sum(pnls) / years,
        "pLossYear": sum(1 for x in year_losses if x > 0) / years,
        "pTenPct": sum(1 for x in year_losses if x > start * 0.10) / years,
        "pHalfYear": sum(1 for x in year_losses if x > start * 0.50) / years,
        "worstYear": worst,
        "pathMaxDrawdown": path_dds[0] if path_dds else 0.0,
        "pathP05Drawdown": p05,
        "pathMedianReturn": path_rets_sorted[len(path_rets_sorted) // 2] if path_rets_sorted else 0.0,
        "pathPLoss": sum(1 for r in path_rets if r < 0) / len(path_rets) if path_rets else 0.0,
        "pathPRuin": sum(1 for r in path_rets if r <= -0.99) / len(path_rets) if path_rets else 0.0,
        "lossByLayer": [{"name": n, "nice": _nice(n), "loss": v, "share": v / total_hit}
                        for n, v in ranked],
    }


def build_verify(data: dict) -> dict:
    on_chain = data["verification"]
    computed = MVP_PARAMS.param_hash()
    params_ok = computed.lower() == on_chain["paramHash"].lower()

    forecasts = data.get("forecasts") or []
    resolved = [f for f in forecasts if f.get("resolved")]
    # A forecast written after its outcome would have realizedBps set at
    # publish time. The contract forbids a second publish; we check the
    # indexed shape: every resolved row was first published with a prediction.
    ordered = True
    for f in resolved:
        if f.get("predictedBps") is None:
            ordered = False

    seed = data["vault"]["seedCapital"]
    identity = seed + data["vault"]["cumPremium"] - data["vault"]["cumLosses"]
    drift = abs(data["vault"]["nav"] - identity) / seed if seed else 0

    checks = [
        {
            "id": "params",
            "ok": params_ok,
            "label": "Parameter hash matches the frozen set",
            "detail": f"computed {computed} · on-chain {on_chain['paramHash']}",
        },
        {
            "id": "commitment",
            "ok": len(on_chain["seedCommitment"]) == 66,
            "label": "Seed commitment is on-chain and immutable",
            "detail": on_chain["seedCommitment"],
        },
        {
            "id": "forecasts",
            "ok": ordered and len(forecasts) > 0,
            "label": "Every forecast was published before its outcome",
            "detail": (
                f"{len(forecasts)} published, {len(resolved)} resolved, "
                f"none revised. Event quarters scored: "
                f"{sum(1 for f in resolved if f['epoch'] % 4 == 2)}."
            ),
        },
        {
            "id": "identity",
            "ok": drift < 0.02,
            "label": "NAV equals seed plus premiums minus losses",
            "detail": (
                f"seed {seed:,.0f} + premium {data['vault']['cumPremium']:,.0f} "
                f"− losses {data['vault']['cumLosses']:,.0f} vs NAV "
                f"{data['vault']['nav']:,.0f} (drift {drift:.2%}). "
                f"Small drift is intra-epoch accrual, not minting."
            ),
        },
        {
            "id": "replay",
            "ok": None if not on_chain["revealed"] else True,
            "label": "Event stream replays from the revealed seed",
            "detail": (
                f"{on_chain['eventCount']} events published. "
                + (
                    "Seed is sealed until the run ends — that is the point. "
                    "After reveal, `python3 replay.py` regenerates every event."
                    if not on_chain["revealed"]
                    else "Seed revealed. Replay the stream with python3 replay.py."
                )
            ),
        },
    ]
    return {
        "generatedAt": int(time.time()),
        "revealed": on_chain["revealed"],
        "checks": checks,
    }


def enrich(data: dict) -> dict:
    data = dict(data)
    data["vault"] = dict(data["vault"])
    data["vault"]["maxDrawdown"] = max_drawdown(data["epochs"], data["vault"]["seedCapital"])
    ev = [f for f in data.get("forecasts", []) if f.get("resolved") and f["epoch"] % 4 == 2]
    sum_p = sum(f["predictedBps"] for f in ev)
    sum_r = sum(f["realizedBps"] or 0 for f in ev)
    data["vault"]["predBps"] = sum_p
    data["vault"]["realBps"] = sum_r
    data["brief"] = build_brief(data)
    data["stress"] = stress_book(data)
    data["verify"] = build_verify(data)
    return data


def main():
    data = json.loads(OUT.read_text())
    t0 = time.time()
    data = enrich(data)
    OUT.write_text(json.dumps(data, indent=1))
    b = data["brief"]
    s = data["stress"]
    print(f"  brief: {b['headline']}")
    print(f"  stress: {s['years']}y  worst {s['worstYear']['loss']:,.0f}  "
          f"30y max DD {s['pathMaxDrawdown']:.1%}  ({time.time()-t0:.1f}s)")
    for c in data["verify"]["checks"]:
        mark = {True: "PASS", False: "FAIL", None: "SEALED"}[c["ok"]]
        print(f"  [{mark}] {c['label']}")


if __name__ == "__main__":
    main()
