"""30-day profile of leaderboard wallets (exchange PnL series): pnl, volume, return, trades, rebates, positive days, drawdown.
usage: python prof.py crypto/month 30 60   (needs lb.json, upnl.json)"""
import json, time, sys, statistics
u = json.load(open("upnl.json")); lb = json.load(open("lb.json"))
cats = sys.argv[1].split(","); days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
now = time.time()
def daily(pts):
    by = {}
    for p in pts:
        by[time.strftime('%Y-%m-%d', time.gmtime(p["timestamp"]))] = p       # last point of each day
    return [by[d] for d in sorted(by)]
rows = []
seen = set()
for c in cats:
    for r in lb[c]:
        w = r["user_id"]
        if w in seen or not u.get(w): continue
        seen.add(w)
        d = daily(u[w])
        base = [p for p in d if p["timestamp"] < now - days * 86400]
        b = base[-1] if base else None
        win = [p for p in d if p["timestamp"] >= now - days * 86400]
        if len(win) < 3: continue
        e = win[-1]
        g = lambda k: (e.get(k) or 0) - ((b.get(k) or 0) if b else 0)
        pnl = g("economic_pnl"); vol = g("volume_usdc"); n = g("trade_count")
        series = [p["economic_pnl"] for p in ([b] if b else []) + win]
        dd = [series[i] - series[i - 1] for i in range(1, len(series))]
        pos = sum(x > 0 for x in dd); neg = sum(x < 0 for x in dd)
        peak, mdd = -1e18, 0
        for s in series:
            peak = max(peak, s); mdd = min(mdd, s - peak)
        rows.append(dict(w=w, cat=c, rank=r["rank"], pnl=pnl, vol=vol, n=n, roi=pnl / vol if vol else 0, avg=vol / n if n else 0,
                         reb=g("maker_rebate"), rew=g("reward_income"), fees=g("fees_paid"), pos=pos, neg=neg, mdd=mdd,
                         worst=min(dd) if dd else 0, posp=g("position_pnl"), age=len(d)))
json.dump(rows, open("prof_" + "_".join(c.replace("/", "-") for c in cats) + ".json", "w"))
rows.sort(key=lambda r: -r["pnl"])
print(f"{'#':>3s} {'pnl30':>9s} {'vol30':>10s} {'roi%':>6s} {'trades':>7s} {'avg$':>7s} {'rebate':>8s} {'reward':>8s} {'fees':>8s} {'+d/-d':>7s} {'maxDD':>9s} {'worst':>9s} {'age':>4s}")
for r in rows[:int(sys.argv[3]) if len(sys.argv) > 3 else 60]:
    print(f"{r['rank']:3d} {r['pnl']:9.0f} {r['vol']:10.0f} {100 * r['roi']:6.2f} {r['n']:7.0f} {r['avg']:7.1f} {r['reb']:8.0f} {r['rew']:8.0f} {r['fees']:8.0f} {r['pos']:3d}/{r['neg']:<3d} {r['mdd']:9.0f} {r['worst']:9.0f} {r['age']:4d}")
