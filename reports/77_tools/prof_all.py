"""30-day profile of every wallet in upnl.json and the 'systematic' subset: pnl > $5k, >= 1500 trades, <= $150 a trade,
>= 20 positive days of 30, drawdown < 25 % of the month's pnl. Writes prof_all.json and sys_wallets.json."""
import json, time
u = json.load(open("upnl.json")); now = time.time()
def daily(pts):
    by = {}
    for p in pts: by[time.strftime('%Y-%m-%d', time.gmtime(p["timestamp"]))] = p
    return [by[d] for d in sorted(by)]
rows = []
for w, pts in u.items():
    d = daily(pts); base = [p for p in d if p["timestamp"] < now - 30 * 86400]; b = base[-1] if base else None
    win = [p for p in d if p["timestamp"] >= now - 30 * 86400]
    if len(win) < 20 or not b: continue
    e = win[-1]; g = lambda k: (e.get(k) or 0) - (b.get(k) or 0)
    pnl, vol, n = g("economic_pnl"), g("volume_usdc"), g("trade_count")
    s = [b["economic_pnl"]] + [p["economic_pnl"] for p in win]; dd = [s[i] - s[i - 1] for i in range(1, len(s))]
    peak, mdd = -1e18, 0
    for x in s: peak = max(peak, x); mdd = min(mdd, x - peak)
    rows.append(dict(w=w, pnl=pnl, vol=vol, n=n, roi=pnl / vol if vol else 0, avg=vol / n if n else 0, pos=sum(x > 0 for x in dd),
                     neg=sum(x < 0 for x in dd), mdd=mdd, reb=g("maker_rebate"), rew=g("reward_income")))
json.dump(rows, open("prof_all.json", "w"))
sel = [r for r in rows if r["pnl"] > 5000 and r["n"] >= 1500 and r["avg"] <= 150 and r["pos"] >= 20 and r["mdd"] > -0.25 * r["pnl"]]
json.dump([r["w"] for r in sorted(sel, key=lambda r: -r["pnl"])], open("sys_wallets.json", "w"))
print("profiled", len(rows), "systematic", len(sel))
