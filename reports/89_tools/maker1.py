"""Maker fills of a wallet in BTC 5m / 15m windows: price vs our Binance fair at the fill second, outcome PnL, markout,
and the Binance move just before the fill (against the maker = the taker was informed)."""
import json, sys, collections, time
from fairlib import p_up, has, mom, px
raw = json.load(open("who7_raw.json")); oc = json.load(open("outcomes.json"))
def rows_of(pref):
    w = [k for k in raw if k.startswith(pref)][0]
    tk = collections.Counter((x["transaction_hash"], x["outcome"], round(float(x["size"]), 4)) for x in raw[w]["taker"])
    out = []
    for x in sorted(raw[w]["all"], key=lambda x: x["timestamp"]):
        k = (x["transaction_hash"], x["outcome"], round(float(x["size"]), 4))
        if tk.get(k): tk[k] -= 1; continue
        s = x["slug"]
        if not s.startswith(("btc-updown-5m-", "btc-updown-15m-")) or s not in oc: continue
        dur = 300 if "-5m-" in s else 900; start = int(s.rsplit("-", 1)[1]); t = int(x["timestamp"])
        if not has("btc", t + 30) or not has("btc", start - 3700): continue
        pu0 = p_up("btc", start, dur, t, 0)
        if pu0 is None: continue
        pu5 = p_up("btc", start, dur, t + 5, 0); pu30 = p_up("btc", start, dur, t + 30, 0)
        side = x["outcome"]; p = float(x["price"]); n = float(x["size"]); buy = x["side"] == "BUY"
        f0 = pu0 if side == "Up" else 1 - pu0
        f5 = None if pu5 is None else (pu5 if side == "Up" else 1 - pu5)
        f30 = None if pu30 is None else (pu30 if side == "Up" else 1 - pu30)
        sg = 1 if buy else -1                  # +1: the maker gains when the side's value rises
        won = oc[s] == side
        out.append(dict(s=s, t=t, rel=t - start, dur=dur, side=side, buy=buy, p=p, n=n, usd=p * n,
                        edge=sg * (f0 - p), pnl=sg * n * ((1.0 if won else 0.0) - p),
                        mk5=None if f5 is None else sg * (f5 - f0), mk30=None if f30 is None else sg * (f30 - f0),
                        pre=sg * (1 if side == "Up" else -1) * mom("btc", t, 2)))
    return w, out
for pref in sys.argv[1:]:
    w, R = rows_of(pref)
    if not R: continue
    u = sum(r["usd"] for r in R); pn = sum(r["pnl"] for r in R)
    e = sum(r["edge"] * r["n"] for r in R) / sum(r["n"] for r in R)
    m5 = [r for r in R if r["mk5"] is not None]; m30 = [r for r in R if r["mk30"] is not None]
    mk5 = sum(r["mk5"] * r["n"] for r in m5) / sum(r["n"] for r in m5); mk30 = sum(r["mk30"] * r["n"] for r in m30) / sum(r["n"] for r in m30)
    span = (R[-1]["t"] - R[0]["t"]) / 3600
    # per window: both sides bought -> pair cost
    pw = collections.defaultdict(lambda: {"Up": [0, 0], "Down": [0, 0]})
    for r in R:
        if r["buy"]: pw[r["s"]][r["side"]][0] += r["n"]; pw[r["s"]][r["side"]][1] += r["usd"]
    pairs = [(min(v["Up"][0], v["Down"][0]), v) for v in pw.values() if v["Up"][0] and v["Down"][0]]
    pc = [ (v["Up"][1] / v["Up"][0] + v["Down"][1] / v["Down"][0]) for q, v in pairs]
    bal = [abs(v["Up"][0] - v["Down"][0]) / max(v["Up"][0] + v["Down"][0], 1e-9) for v in pw.values()]
    print(f"{pref}: fills {len(R)} in {span:.1f} h, ${u:.0f}; outcome pnl {pn:+.0f} ({100 * pn / u:+.1f} %, w/o rebates); "
          f"edge at fill vs our fair {100 * e:+.2f} c/share; markout +5 s {100 * mk5:+.2f} c, +30 s {100 * mk30:+.2f} c; "
          f"windows {len(pw)}, two-sided {len(pairs)}, avg pair cost {sum(pc) / max(len(pc), 1):.3f}, imbalance median {(sorted(bal)[len(bal) // 2] if bal else 0):.2f}")
    # by Binance move in the 2 s before the fill, against the maker
    for lo, hi in ((-99, -2), (-2, -0.5), (-0.5, 0.5), (0.5, 2), (2, 99)):
        g = [r for r in R if lo <= r["pre"] < hi]
        if not g: continue
        uu = sum(r["usd"] for r in g)
        print(f"     Binance 2 s before, toward the maker {lo:>4}..{hi:<4} bp: fills {len(g):5d} edge {100 * sum(r['edge'] * r['n'] for r in g) / sum(r['n'] for r in g):+6.2f} c  pnl {100 * sum(r['pnl'] for r in g) / uu:+6.1f} %")
