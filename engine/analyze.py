import json, pathlib
d = json.loads((pathlib.Path.cwd().parent/"dashboard/data.json").read_text())
e = d["epochs"]
peak = mx = 0
at = pk = 0
for p in e:
    peak = max(peak, p["nav"])
    dd = (peak - p["nav"]) / peak if peak else 0
    if dd > mx:
        mx, at, pk = dd, p["epoch"], peak
print(f"max drawdown {mx:.1%} at epoch {at} (peak {pk:,.0f})")
print()
print("epochs that paid a claim:")
for p in e:
    if p["losses"] > 0:
        print(f"  epoch {p['epoch']:>4}  paid {p['losses']:>12,.0f}  NAV after {p['nav']:>12,.0f}")
print()
print("largest events:")
for c in sorted(d["catastrophes"], key=lambda c: -c["subjectLoss"])[:8]:
    print(f"  epoch {c['epoch']:>4}  {c['perilName']:<10} {c['subjectLoss']:>8,.0f}M")
