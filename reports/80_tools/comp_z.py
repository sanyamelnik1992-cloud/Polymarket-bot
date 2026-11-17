"""The competitor's entries in our terms: lead (bp), z with a 1 h sigma, time left; and the lowest z over his window after entry."""
import json, collections, numpy as np, time
from zmod import *
W = {w["slug"]: w for w in json.load(open("windows.json"))}
E = json.load(open("comp_entries.json"))
rows = []
for e in E:
    w = W.get(e["s"])
    if not w: continue
    t = e["t"]
    lead, sg, _ = z_at(w, t)
    s1h = sigma(w["asset"], t, 3600)
    z = lead / (s1h * np.sqrt(tau(w, t)))
    side_ok = (lead > 0) == (e["o"] == "Up")
    # min z of his side over the rest of the window (how close it came to flipping)
    zmin = min((z_at(w, tt)[0] * (1 if e["o"] == "Up" else -1)) / (sigma(w["asset"], tt, 3600) * np.sqrt(tau(w, tt))) for tt in range(t, w["end"] - 3, 5))
    rows.append(dict(kind=w["kind"], left=w["end"] - t, lead=lead if e["o"] == "Up" else -lead, z=z if e["o"] == "Up" else -z, s=s1h, px=e["px"], won=e["won"], side_ok=bool(side_ok), zmin=zmin, slug=e["s"], t=t))
json.dump(rows, open("comp_z.json", "w"), default=float)
for kind in ("5m", "15m", "1h"):
    r = [x for x in rows if x["kind"] == kind]
    zs = sorted(x["z"] for x in r); ls = sorted(x["lead"] for x in r)
    q = lambda a, f: a[int(f * (len(a) - 1))]
    print(f"{kind}: n={len(r)} lost={sum(not x['won'] for x in r)} his side = Binance leader: {sum(x['side_ok'] for x in r)}/{len(r)}")
    print(f"   z (1h sigma) p05 {q(zs,.05):.1f} p25 {q(zs,.25):.1f} med {q(zs,.5):.1f} p75 {q(zs,.75):.1f}   lead bp p05 {q(ls,.05):.1f} med {q(ls,.5):.1f}")
    print("   z buckets:", sorted(collections.Counter(min(8, int(x["z"])) for x in r).items()))
print("\nlosses:")
for x in rows:
    if not x["won"]:
        print(" ", x["slug"], time.strftime("%m-%d %H:%M:%S", time.gmtime(x["t"])), f"left {x['left']}s lead {x['lead']:.1f}bp z {x['z']:.2f} sigma {x['s']:.2f} px {x['px']}")
