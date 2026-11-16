"""Realized edge of top Up/Down wallets by role (taker / maker), price and time in the window.
usage: python edge.py trades_crypto.json outcomes.json prof_crypto-month.json"""
import json, sys, collections, re
tr = json.load(open(sys.argv[1])); oc = json.load(open(sys.argv[2])); prof = {r["w"]: r for f in sys.argv[3].split(",") for r in json.load(open(f))}
FEE = 0.07
def info(s):
    m = re.search(r"-updown-(5m|15m|4h)-(\d{10})$", s)
    return (m.group(1), int(m.group(2)) + {"5m": 300, "15m": 900, "4h": 14400}[m.group(1)]) if m else (None, None)
agg = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0])          # shares, payoff - cost, fees, n
per_w = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0]))
for w, rows in tr.items():
    for r in rows:
        k, e = info(r["s"])
        if not k or r["side"] != "BUY" or r["s"] not in oc: continue
        won = oc[r["s"]] == r["o"]
        sh, px = r["sz"], r["px"]
        fee = FEE * px * (1 - px) * sh if r["tk"] else 0.0
        d = 300 if k == "5m" else 900 if k == "15m" else 14400
        frac = 1 - (e - r["t"]) / d
        pb = "<.2" if px < .2 else ".2-.4" if px < .4 else ".4-.6" if px < .6 else ".6-.8" if px < .8 else ".8-.9" if px < .9 else ".9-.95" if px < .95 else ">=.95"
        tb = "first half" if frac < .5 else "50-80%" if frac < .8 else "80-95%" if frac < .95 else "last 5%"
        for key in ((("TAKER" if r["tk"] else "MAKER"), "price", pb), (("TAKER" if r["tk"] else "MAKER"), "time", tb), (("TAKER" if r["tk"] else "MAKER"), "all", "")):
            for tgt in (agg[key], per_w[w][key]):
                tgt[0] += sh; tgt[1] += (won - px) * sh; tgt[2] += fee; tgt[3] += 1
print("ALL top crypto wallets, buys on 5m/15m/4h Up/Down (edge = payoff - price, cents a share; taker fee 0.07 p(1-p) subtracted):")
for key in sorted(agg):
    s, g, f, n = agg[key]
    print(f"  {key[0]:5s} {key[1]:5s} {key[2]:10s} buys {n:6d} shares {s:9.0f}  gross {100 * g / s:+6.2f}c  net {100 * (g - f) / s:+6.2f}c")
print("\nper wallet (all buys):")
for w in sorted(per_w, key=lambda w: -prof.get(w, {}).get("pnl", 0)):
    line = []
    for role in ("TAKER", "MAKER"):
        x = per_w[w].get((role, "all", ""))
        if x and x[3] >= 30:
            line.append(f"{role} n {x[3]:5d} net {100 * (x[1] - x[2]) / x[0]:+6.2f}c")
    if line:
        p = prof.get(w, {})
        print(f"  {w[:8]} pnl30 {p.get('pnl', 0):7.0f} rebate30 {p.get('reb', 0):6.0f} | " + " | ".join(line))
