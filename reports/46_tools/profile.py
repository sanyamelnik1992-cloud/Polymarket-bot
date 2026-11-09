"""Profile chosen wallets (prefix…suffix as printed by players.py). usage: profile.py WINDOWS.jsonl OURWALLET.json PFX..."""
import bisect, collections, json, statistics as st, sys
wins = [json.loads(l) for l in open(sys.argv[1])]
ours = json.load(open(sys.argv[2]))[0]
pfx = sys.argv[3:]
def match(w):
    for p in pfx:
        if "…" not in p: continue
        a, b = p.split("…")
        if w[2:2 + len(a)] == a and w.endswith(b): return p
    return "МЫ" if w == ours and "МЫ" in pfx else None
P = collections.defaultdict(lambda: collections.defaultdict(float))
sizes = collections.defaultdict(list)
imb = collections.defaultdict(list)
for w in wins:
    ws, win = w["ws"], w["winner"]
    if win not in ("Up", "Down"): continue
    tr = w["trades"]; times = [t[0] for t in tr]; pus = [t[5] if t[3] == "Up" else 1 - t[5] for t in tr]
    outU = 1.0 if win == "Up" else 0.0
    def price_at(t):
        if t >= ws + 300: return outU
        i = bisect.bisect_right(times, t) - 1
        return pus[i] if i >= 0 else None
    net = collections.defaultdict(lambda: {"Up": 0.0, "Down": 0.0, "cash": 0.0, "g": 0.0})
    for (t, wal, side, out, size, p, tk) in tr:
        k = match(wal)
        if not k: continue
        rel = t - ws; s = P[k]; sgn = 1 if side == "BUY" else -1
        s[f"{side}_{out}"] += size
        s["tk"] += size if tk else 0
        b = "pre" if rel < 0 else f"{min(rel,299)//60*60:03d}"
        s["v_" + b] += size
        d = 1 if (side == "BUY") == (out == "Up") else -1
        pu = p if out == "Up" else 1 - p
        f = price_at(t + 60)
        if f is not None:
            s["m60_" + b] += d * (f - pu) * 100 * size; s["m60w_" + b] += size
        own = p                               # price of the token they trade
        pb = "<.4" if own < 0.4 else ".4-.6" if own <= 0.6 else ">.6"
        s["px_" + pb] += size
        sizes[k].append(size)
        e = net[k]; e[out] += sgn * size; e["cash"] -= sgn * size * p; e["g"] += size
    for k, e in net.items():
        pnl = e["cash"] + e[win]
        dirn = abs(e["Up"] - e["Down"])
        P[k]["pnl"] += pnl; P[k]["n"] += 1
        pair = min(max(e["Up"], 0), max(e["Down"], 0))
        imb[k].append(dirn / max(e["g"], 1e-9))
        # P&L split: directional part = what the unpaired remainder made
        P[k]["dir_pnl"] += (e["Up"] - e["Down"]) * (1 if win == "Up" else -1) * 0.5
for k in pfx:
    s = P[k]
    if not s: print(k, "нет данных"); continue
    v = sum(s[f"{a}_{b}"] for a in ("BUY", "SELL") for b in ("Up", "Down"))
    print(f"== {k}: окон {s['n']:.0f}, P&L {s['pnl']:+.0f}$, тейкер {s['tk']/v:.0%}, медиана сделки {st.median(sizes[k]):.0f} акций")
    print("   покупки Up/Down {:.0%}/{:.0%}, продажи Up/Down {:.0%}/{:.0%}".format(*(s[f'{a}_{b}']/v for a in ('BUY','SELL') for b in ('Up','Down'))))
    print("   цена своей стороны: <0.4 {:.0%}, 0.4-0.6 {:.0%}, >0.6 {:.0%}".format(*(s['px_'+x]/v for x in ('<.4','.4-.6','>.6'))))
    print("   объём по минутам окна: " + "  ".join(f"{b}:{s['v_'+b]/v:.0%} (м60 {s['m60_'+b]/s['m60w_'+b]:+.1f}¢)" for b in ("pre","000","060","120","180","240") if s['m60w_'+b]))
    print(f"   перекос позиции в конце окна: медиана {st.median(imb[k]):.0%} от объёма окна")
