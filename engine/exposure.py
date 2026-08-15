import json, pathlib
d = json.loads((pathlib.Path.cwd().parent/"dashboard/data.json").read_text())
for target in (82, 114):
    f = next((x for x in d["forecasts"] if x["epoch"] == target), None)
    if f:
        print(f"epoch {target}: predicted {f['predictedBps']/100:.2f}%  "
              f"realised {f['realizedBps']/100 if f['resolved'] else '?'}%")
        print(f"  {f['rationale']}")
    e = next((x for x in d["epochs"] if x["epoch"] == target), None)
    if e:
        print(f"  paid {e['losses']:,.0f} on NAV {e['nav']:,.0f}")
    print()
