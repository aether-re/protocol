import json, pathlib
d = json.loads((pathlib.Path.cwd().parent/"dashboard/data.json").read_text())
ev = [f for f in d["forecasts"] if f["resolved"] and f["epoch"] % 4 == 2]
p = sum(f["predictedBps"] for f in ev)
r = sum(f["realizedBps"] for f in ev)
print(f"event quarters scored : {len(ev)}")
print(f"cumulative predicted  : {p/100:.2f}%")
print(f"cumulative realised   : {r/100:.2f}%")
print(f"ratio predicted/real  : {p/r:.2f}x" if r else "ratio: n/a")
print()
big = sorted(ev, key=lambda f: -f["realizedBps"])[:3]
tail = sum(f["realizedBps"] for f in big)
print(f"top 3 quarters are {tail/r:.0%} of all realised loss" if r else "")
print(f"without them, ratio would be {p/(r-tail):.1f}x" if r-tail>0 else "")
