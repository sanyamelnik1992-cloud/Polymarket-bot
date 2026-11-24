import json, time, sys
up = json.load(open("upnl.json"))
days = float(sys.argv[1]) if len(sys.argv) > 1 else 7
now = time.time()
rows = []
for w, pts in up.items():
    if not pts: continue
    pts = sorted(pts, key=lambda p: p["timestamp"])
    base = [p for p in pts if p["timestamp"] <= now - days * 86400]
    win = [p for p in pts if p["timestamp"] > now - days * 86400]
    if len(win) < 2: continue
    b = base[-1] if base else {}
    e = win[-1]
    g = lambda k: (e.get(k) or 0) - (b.get(k) or 0)
    ser = [b.get("economic_pnl", 0)] + [p.get("economic_pnl") or 0 for p in win]
    dd = [ser[i] - ser[i - 1] for i in range(1, len(ser))]
    rows.append(dict(w=w, pnl=g("economic_pnl"), pos=g("position_pnl"), reb=g("maker_rebate"), rew=g("reward_income"),
                     fees=g("fees_paid"), vol=g("volume_usdc"), n=g("trade_count"), up=sum(x > 0 for x in dd), dn=sum(x < 0 for x in dd)))
json.dump(rows, open(f"prof{int(days)}.json", "w"))
rows.sort(key=lambda r: -r["pnl"])
print(f"keys of a point: {sorted(win[-1].keys())}")
print(f"{'wallet':10s} {'pnl':>9s} {'pos_pnl':>9s} {'rebate':>8s} {'reward':>7s} {'fees':>8s} {'volume':>10s} {'trades':>7s} {'$/tr':>6s} {'roi%':>6s} {'+d/-d':>6s}")
for r in rows[:45]:
    print(f"{r['w'][:10]} {r['pnl']:9.0f} {r['pos']:9.0f} {r['reb']:8.0f} {r['rew']:7.0f} {r['fees']:8.0f} {r['vol']:10.0f} {r['n']:7.0f} "
          f"{(r['vol'] / r['n'] if r['n'] else 0):6.1f} {(100 * r['pnl'] / r['vol'] if r['vol'] else 0):6.2f} {r['up']:2d}/{r['dn']:<2d}")
