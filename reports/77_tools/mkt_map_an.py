"""Market-wide taker edge map of the Up/Down windows in mkt_taker.json (from mkt_map.py): time in the window x price, net of the taker fee."""
import json, collections
d = json.load(open("mkt_taker.json"))
FEE = 0.07
cells = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0, set()])
capped = 0
for s, v in d.items():
    dur = 900 if "-15m-" in s else 300
    start = int(s.rsplit("-", 1)[1]); end = start + dur
    if len(v["rows"]) >= 3000: capped += 1
    for r in v["rows"]:
        if r["side"] != "BUY": continue
        el = (r["t"] - start) / dur
        if not 0 <= el <= 1.05: continue
        won = r["o"] == v["win"]
        px, sh = r["px"], r["sz"]
        fee = FEE * px * (1 - px)
        tb = "0-20%" if el < .2 else "20-40%" if el < .4 else "40-60%" if el < .6 else "60-80%" if el < .8 else "80-90%" if el < .9 else "90-97%" if el < .97 else "97-100%"
        pb = "<.1" if px < .1 else ".1-.3" if px < .3 else ".3-.5" if px < .5 else ".5-.7" if px < .7 else ".7-.85" if px < .85 else ".85-.94" if px < .94 else ".94-.98" if px < .98 else ">=.98"
        kind = "15m" if dur == 900 else "5m"
        for key in ((kind, tb, pb), (kind, tb, "all"), (kind, "all", pb)):
            c = cells[key]; c[0] += sh; c[1] += (won - px) * sh; c[2] += fee * sh; c[3] += 1; c[4].add(s)
print("windows with >= 3000 taker trades (truncated):", capped)
for kind in ("5m", "15m"):
    print(f"\n{kind}: taker BUY edge net of fee, cents a share (n trades) -- all takers of the market, not selected")
    tbs = ["0-20%", "20-40%", "40-60%", "60-80%", "80-90%", "90-97%", "97-100%", "all"]
    pbs = ["<.1", ".1-.3", ".3-.5", ".5-.7", ".7-.85", ".85-.94", ".94-.98", ">=.98", "all"]
    print(f"{'elapsed':8s}" + "".join(f"{p:>15s}" for p in pbs))
    for tb in tbs:
        line = f"{tb:8s}"
        for pb in pbs:
            c = cells.get((kind, tb, pb)) if not (tb == "all" and pb == "all") else None
            line += f"{'':>15s}" if not c or c[3] < 40 else f"{100 * (c[1] - c[2]) / c[0]:+7.2f} ({c[3]:5d})"
        print(line)
