"""How the successful makers fill (reports/89 data: who7_raw.json, outcomes.json, bn1s.npz, fairlib.py in the current folder).

Maker fills of every wallet in BTC 5m / 15m windows, converted to the maker-LONG side: a maker BUY of X at p = long X at p; a maker SELL
of X at p = long the other side at 1 - p. Per fill: the side's fair value at the fill second (Binance, the interceptor's model), the outcome,
the second of the window, the Binance move toward the side over the last 3 s. Prints, per wallet and pooled over the wallets with enough
fills: share of volume and outcome pnl per share by fair value of the long side, by Binance move, by second of the window, by price - fair."""
import json, sys, collections
import numpy as np
from fairlib import p_up, has, mom
raw = json.load(open("who7_raw.json")); oc = json.load(open("outcomes.json"))
MIN_FILLS = int(sys.argv[1]) if len(sys.argv) > 1 else 300
PREF = sys.argv[2:]
def fills(w):
    tk = collections.Counter((x["transaction_hash"], x["outcome"], round(float(x["size"]), 4)) for x in raw[w]["taker"])
    out = []
    for x in sorted(raw[w]["all"], key=lambda x: x["timestamp"]):
        k = (x["transaction_hash"], x["outcome"], round(float(x["size"]), 4))
        if tk.get(k):
            tk[k] -= 1; continue
        s = x["slug"]
        if not s.startswith("btc-updown-5m-") or s not in oc: continue            # BTC 5m: the maker bot's market
        start = int(s.rsplit("-", 1)[1]); t = int(x["timestamp"])
        if not has("btc", t + 30) or not has("btc", start - 3700) or not (0 <= t - start < 300): continue
        pu = p_up("btc", start, 300, t, 0)
        if pu is None: continue
        pu5 = p_up("btc", start, 300, t + 5, 0); pu30 = p_up("btc", start, 300, t + 30, 0)
        side = x["outcome"]; p = float(x["price"]); n = float(x["size"]); buy = x["side"] == "BUY"
        f0 = pu if side == "Up" else 1 - pu
        # the maker-long side
        long_up = (side == "Up") == buy                      # BUY Up / SELL Down -> long Up
        pL = p if buy else 1 - p
        fu = lambda q: None if q is None else (q if long_up else 1 - q)
        fL = fu(pu); f5 = fu(pu5); f30 = fu(pu30)
        won = (oc[s] == "Up") == long_up
        out.append(dict(w=w[:6], rel=t - start, n=n, pL=pL, fL=fL, edge=fL - pL, res=(1.0 if won else 0.0) - pL,
                        mk5=None if f5 is None else f5 - fL, mk30=None if f30 is None else f30 - fL,
                        pre=(1 if long_up else -1) * mom("btc", t, 3), buy=buy, up=long_up, t=t, s=s))
    return out
R = []
for w in raw:
    if PREF and not any(w.startswith(p) for p in PREF): continue
    f = fills(w)
    if len(f) >= MIN_FILLS:
        R += f
        print(f"{w[:8]}: {len(f)} BTC 5m maker fills, {sum(x['n'] for x in f):.0f} shares, span {(f[-1]['t'] - f[0]['t']) / 3600:.1f} h")
if not R:
    sys.exit("no wallet with enough fills")
n = np.array([x["n"] for x in R]); W = np.minimum(n, 50)
def col(k): return np.array([np.nan if x[k] is None else x[k] for x in R], float)
fL, edge, res, rel, pre, pL, mk5, mk30 = (col(k) for k in ("fL", "edge", "res", "rel", "pre", "pL", "mk5", "mk30"))
wal = np.array([x["w"] for x in R])
print(f"\npooled: {len(R)} fills, {n.sum():.0f} shares; per share: edge {100 * (edge * W).sum() / W.sum():+.2f} c, outcome pnl {100 * (res * W).sum() / W.sum():+.2f} c, "
      f"markout 5 s {100 * np.nansum(mk5 * W) / W[np.isfinite(mk5)].sum():+.2f} c, 30 s {100 * np.nansum(mk30 * W) / W[np.isfinite(mk30)].sum():+.2f} c")
def tab(title, key, bins):
    print("\n" + title)
    tot = W.sum()
    for lo, hi in bins:
        m = (key >= lo) & (key < hi)
        if W[m].sum() < 0.01 * tot: continue
        ww = W[m]
        print(f"  {lo:>6}..{hi:<6} volume {100 * ww.sum() / tot:5.1f} %  edge {100 * (edge[m] * ww).sum() / ww.sum():+6.2f} c  outcome pnl {100 * (res[m] * ww).sum() / ww.sum():+6.2f} c  "
              f"mk5 {100 * np.nansum(mk5[m] * ww) / max(ww[np.isfinite(mk5[m])].sum(), 1e-9):+6.2f}  mk30 {100 * np.nansum(mk30[m] * ww) / max(ww[np.isfinite(mk30[m])].sum(), 1e-9):+6.2f}")
tab("by fair value of the LONG side (>= 0.5 = the favourite):", fL, [(0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)])
tab("by Binance move toward the long side over the last 3 s (bp):", pre, [(-99, -3), (-3, -1.5), (-1.5, -0.5), (-0.5, 0.5), (0.5, 1.5), (1.5, 3), (3, 99)])
tab("by second of the window:", rel, [(0, 20), (20, 60), (60, 100), (100, 140), (140, 180), (180, 230), (230, 300)])
tab("by edge (fair - price of the long side, c):", edge * 100, [(-100, -5), (-5, -2), (-2, 0), (0, 1), (1, 2), (2, 4), (4, 8), (8, 100)])
print("\nside balance: the long side is the favourite in", f"{100 * W[fL >= 0.5].sum() / W.sum():.1f} % of the volume; BUYs (maker bid filled) {100 * W[np.array([x['buy'] for x in R])].sum() / W.sum():.1f} %")
print("per window: shares bought on each side (median of two-sided windows), balance |U-D| / (U+D):")
pw = collections.defaultdict(lambda: [0.0, 0.0])
for x in R: pw[(x["w"], x["s"])][0 if x["up"] else 1] += x["n"]
two = [v for v in pw.values() if v[0] > 0 and v[1] > 0]
bal = sorted(abs(v[0] - v[1]) / (v[0] + v[1]) for v in pw.values())
print(f"  windows {len(pw)}, two-sided {len(two)} ({100 * len(two) / len(pw):.0f} %), median balance {bal[len(bal) // 2]:.2f}")
