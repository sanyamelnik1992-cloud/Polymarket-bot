"""Market-wide calibration of the temperature markets: every taker buy (YES and NO), realized edge after the fee by
price and by hours before the end of the local day. Not selected: all participants."""
import json, collections
days = json.load(open("days.json")); trades = json.load(open("trades.json")); FEE = 0.05
cells = collections.defaultdict(lambda: [0.0, 0.0, 0, set()])
for slug, day in days.items():
    end = day["start"] + 86400
    for b in day["buckets"]:
        for tr in trades.get(b["cid"], []):
            if tr["side"] != "BUY": continue
            px = tr["px"]; yes = tr["o"] == "Yes"
            win = b["won"] if yes else not b["won"]
            hl = (end - tr["t"]) / 3600
            lb = "after end" if hl < 0 else "<6h" if hl < 6 else "6-12h" if hl < 12 else "12-24h" if hl < 24 else "24-48h" if hl < 48 else ">48h"
            pb = "<.05" if px < .05 else ".05-.15" if px < .15 else ".15-.3" if px < .3 else ".3-.5" if px < .5 else ".5-.7" if px < .7 else ".7-.85" if px < .85 else ".85-.95" if px < .95 else ">=.95"
            e = (win - px - FEE * px * (1 - px)) * tr["sz"]
            for key in ((("YES" if yes else "NO"), lb, pb), ("ALL", lb, pb), ("ALL", "all", pb), ("ALL", lb, "all")):
                c = cells[key]; c[0] += tr["sz"]; c[1] += e; c[2] += 1; c[3].add(slug)
pbs = ["<.05", ".05-.15", ".15-.3", ".3-.5", ".5-.7", ".7-.85", ".85-.95", ">=.95", "all"]
lbs = [">48h", "24-48h", "12-24h", "6-12h", "<6h", "after end", "all"]
for side in ("ALL", "YES", "NO"):
    print(f"\n{side}: realized edge after fee, cents a share (number of events)")
    print(f"{'':10s}" + "".join(f"{p:>15s}" for p in pbs))
    for lb in lbs:
        line = f"{lb:10s}"
        for pb in pbs:
            c = cells.get((side, lb, pb))
            line += f"{'':>15s}" if not c or len(c[3]) < 15 else f"{100 * c[1] / c[0]:+7.2f} ({len(c[3]):4d})"
        print(line)
