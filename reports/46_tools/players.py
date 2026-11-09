"""Who trades the 5m Up/Down market and who wins. usage: players.py WINDOWS.jsonl BINANCE.json OURWALLET.json
Trade = [ts, wallet, side, outcome, size, price, is_taker]. P&L per wallet and window at the real outcome:
cash flow + net shares of the winner (selling = short, which also covers split-and-sell), minus taker fee
0.07*p*(1-p) per share, plus maker rebate 20% of it. Markout: Up-equivalent price later (last trade, forward-filled;
after the window end = the outcome) minus our price, in the trade's direction, cents per share."""
import bisect, collections, json, sys

wins = [json.loads(l) for l in open(sys.argv[1])]
bn = {int(k): v for k, v in json.load(open(sys.argv[2])).items()}
ours = json.load(open(sys.argv[3]))[0]
FEE = 0.07

def sh(w):
    return "МЫ" if w == ours else w[2:8] + "…" + w[-3:]

def fee(p):
    return FEE * p * (1 - p)

def bnret(t, back):
    a, b = bn.get(t - back), bn.get(t)
    return (b / a - 1) * 1e4 if a and b else None

W = collections.defaultdict(lambda: collections.defaultdict(float))   # wallet stats
WW = collections.defaultdict(set)                                       # wallet -> windows
both = collections.Counter()
lastmin = collections.defaultdict(list)                                 # lock-like buys in the last minute
cp = collections.defaultdict(lambda: collections.defaultdict(float))    # our counterparties
vol_by_rel = collections.Counter()
tot = collections.Counter()
for w in wins:
    ws, win = w["ws"], w["winner"]
    if win not in ("Up", "Down"):
        continue
    tr = w["trades"]
    outU = 1.0 if win == "Up" else 0.0
    # Up-equivalent last-trade price path for markouts
    times = [t[0] for t in tr]
    pus = [t[5] if t[3] == "Up" else 1 - t[5] for t in tr]
    def price_at(t):
        if t >= ws + 300:
            return outU
        i = bisect.bisect_right(times, t) - 1
        return pus[i] if i >= 0 else None
    per = collections.defaultdict(lambda: {"cash": 0.0, "Up": 0.0, "Down": 0.0, "sides": set()})
    takers_by_tx = {}
    for i, (t, wal, side, out, size, p, tk) in enumerate(tr):
        rel = t - ws
        d = 1 if (side == "BUY") == (out == "Up") else -1          # direction in Up terms
        pu = p if out == "Up" else 1 - p
        sgn = 1 if side == "BUY" else -1
        e = per[wal]
        e["cash"] -= sgn * size * p
        e[out] += sgn * size
        if side == "BUY":
            e["sides"].add(out)
        e["cash"] += -size * fee(p) if tk else 0.2 * size * fee(p)
        s = W[wal]
        s["vol"] += size; s["usd"] += size * p; s["n"] += 1
        s["maker"] += 0 if tk else size
        s["relw"] += rel * size
        s["last"] += size if rel >= 240 else 0
        vol_by_rel[min(rel, 299) // 30 * 30] += size
        tot["taker" if tk else "maker"] += size
        for h, key in ((10, "m10"), (60, "m60")):
            f = price_at(t + h)
            if f is not None:
                s[key] += d * (f - pu) * 100 * size; s[key + "w"] += size
        if tk:
            r = bnret(t, 3)
            if r is not None:
                s["bn3"] += d * r * size; s["bn3w"] += size
        if side == "BUY" and rel >= 240 and p >= 0.90:
            lastmin[wal].append((rel, p, size, out == win, tk))
        WW[wal].add(ws)
    for wal, e in per.items():
        pnl = e["cash"] + e[win]
        W[wal]["pnl"] += pnl
        W[wal]["pos_w"] += 1 if pnl > 0 else 0
        if len(e["sides"]) == 2:
            both[wal] += 1
    # our counterparties: takers that hit our maker fills
    tx = {}
    for t in tr:
        pass
nw = sum(1 for w in wins if w["winner"] in ("Up", "Down"))
print(f"окон {nw}, сделок {sum(len(w['trades']) for w in wins)}, кошельков {len(W)}, объём {sum(s['vol'] for s in W.values())/2:,.0f} акций"
      f" (мейкер {tot['maker']:,.0f} / тейкер {tot['taker']:,.0f})")
vols = sorted((s["vol"] for s in W.values()), reverse=True)
print(f"доля топ-10 кошельков в объёме: {sum(vols[:10])/sum(vols):.0%}, топ-50: {sum(vols[:50])/sum(vols):.0%}")
print("объём по секундам окна:", " ".join(f"{k}-{k+29}:{v/sum(vol_by_rel.values()):.0%}" for k, v in sorted(vol_by_rel.items())))

def row(w):
    s = W[w]; n = len(WW[w])
    mk = s["maker"] / s["vol"] if s["vol"] else 0
    m10 = s["m10"] / s["m10w"] if s["m10w"] else float("nan")
    m60 = s["m60"] / s["m60w"] if s["m60w"] else float("nan")
    b3 = s["bn3"] / s["bn3w"] if s["bn3w"] else float("nan")
    return (f"{sh(w):12s} окон {n:3d} объём {s['vol']:8.0f} ${s['usd']:8.0f} мейкер {mk:4.0%} обе стороны {both[w]/n:4.0%} "
            f"сек {s['relw']/s['vol']:5.0f} посл.мин {s['last']/s['vol']:4.0%} м10 {m10:+5.1f}¢ м60 {m60:+5.1f}¢ "
            f"bn3 {b3:+5.2f}бп P&L {s['pnl']:+8.1f}$ ({100*s['pnl']/max(s['usd'],1):+5.1f}%) плюс.окон {s['pos_w']/n:4.0%}")
print("\n== топ-15 по объёму"); [print(row(w)) for w in sorted(W, key=lambda w: -W[w]["vol"])[:15]]
big = [w for w in W if len(WW[w]) >= 20]
print("\n== топ-10 по прибыли (≥20 окон)"); [print(row(w)) for w in sorted(big, key=lambda w: -W[w]["pnl"])[:10]]
print("\n== худшие 8 (≥20 окон)"); [print(row(w)) for w in sorted(big, key=lambda w: W[w]["pnl"])[:8]]
if ours in W:
    print("\n== мы"); print(row(ours))
json.dump({"lastmin": {k: v for k, v in lastmin.items()}, "W": {k: dict(v) for k, v in W.items()},
           "nwin": {k: len(v) for k, v in WW.items()}, "both": dict(both)}, open("players_stats.json", "w"))
