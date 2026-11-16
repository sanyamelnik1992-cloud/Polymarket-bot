"""Every wallet that bought a side priced >= 0.85 in the last 20 % of the Up/Down windows of mkt_taker.json (from mkt_map.py):
windows, buys, lost, shares, net $ after the taker fee; and per window, the FIRST such buy (one sample per window) with a
bootstrap interval. Prints wallet prefixes only on the console."""
import json, collections, random
d = json.load(open("mkt_taker.json")); FEE = 0.07
W = collections.defaultdict(lambda: [0, 0, 0.0, 0.0, set()])
for s, v in d.items():
    dur = 900 if "-15m-" in s else 300; start = int(s.rsplit("-", 1)[1])
    for r in v["rows"]:
        if r["side"] != "BUY" or r["px"] < 0.85 or not 0.8 <= (r["t"] - start) / dur <= 1.0: continue
        won = r["o"] == v["win"]; w = W[r["w"]]
        w[0] += 1; w[1] += not won; w[2] += r["sz"]; w[3] += (won - r["px"] - FEE * r["px"] * (1 - r["px"])) * r["sz"]; w[4].add(s)
print("wallets:", len(W))
for k, v in sorted(W.items(), key=lambda kv: -len(kv[1][4]))[:20]:
    print(f"  {k[:8]} windows {len(v[4]):4d} buys {v[0]:5d} lost {v[1]:3d} shares {v[2]:7.0f} net ${v[3]:+8.1f} = {100 * v[3] / v[2]:+5.2f}c")
for kind, dur in (("5m", 300), ("15m", 900)):
    for lo, hi in ((0.85, 0.94), (0.94, 0.98), (0.98, 1.0)):
        res = []
        for s, v in d.items():
            if f"-{kind}-" not in s: continue
            start = int(s.rsplit("-", 1)[1])
            rows = sorted((r for r in v["rows"] if r["side"] == "BUY" and lo <= r["px"] < hi and 0.8 <= (r["t"] - start) / dur <= 1.0), key=lambda r: r["t"])
            if rows:
                r = rows[0]; res.append(((r["o"] == v["win"]) - r["px"] - FEE * r["px"] * (1 - r["px"]), r["o"] == v["win"]))
        if not res: continue
        random.seed(1); n = len(res)
        boot = sorted(sum(random.choice(res)[0] for _ in range(n)) / n for _ in range(2000))
        print(f"{kind} first buy {lo:.2f}-{hi:.2f} in the last 20 %: windows {n} lost {sum(not x[1] for x in res)} "
              f"edge {100 * sum(x[0] for x in res) / n:+.2f}c  90 % [{100 * boot[100]:+.2f}, {100 * boot[1900]:+.2f}]")
