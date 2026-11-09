"""Last minute: buys at 0.90-0.98 in seconds 240-299 (the TWAP-lock zone). Win rate and P&L per share after fees for
takers and makers, by second and price; how often per window; the biggest lock takers.
usage: lastmin.py WINDOWS.jsonl"""
import collections, json, sys
wins = [json.loads(l) for l in open(sys.argv[1])]
fee = lambda p: 0.07 * p * (1 - p)
A = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0])   # key -> [shares, won shares, pnl, n]
who = collections.defaultdict(lambda: [0.0, 0.0, 0.0, [], 0])
win_has = 0; nwin = 0; avail = []
for w in wins:
    ws, win = w["ws"], w["winner"]
    if win not in ("Up", "Down"): continue
    nwin += 1; got = 0.0
    for (t, wal, side, out, size, p, tk) in w["trades"]:
        rel = t - ws
        if side != "BUY" or not (240 <= rel < 300) or not (0.90 <= p <= 0.98): continue
        won = out == win
        pnl = size * ((1 - p) if won else -p) + (-size * fee(p) if tk else 0.2 * size * fee(p))
        role = "тейкер" if tk else "мейкер"
        sec = "240-259" if rel < 260 else "260-279" if rel < 280 else "280-299"
        pb = "0.90-0.94" if p < 0.945 else "0.95-0.97" if p < 0.975 else "0.98"
        for k in [(role,), (role, sec), (role, pb)]:
            a = A[k]; a[0] += size; a[1] += size if won else 0; a[2] += pnl; a[3] += 1
        if tk:
            e = who[wal]; e[0] += size; e[1] += size if won else 0; e[2] += pnl; e[3].append(rel); e[4] += 1
            if won: got += size
    if got > 0: win_has += 1; avail.append(got)
print(f"окон {nwin}; окон, где тейкеры купили победителя по 0.90-0.98 в последнюю минуту: {win_has} ({win_has/nwin:.0%}), "
      f"медиана {sorted(avail)[len(avail)//2]:.0f} акций на окно")
for k in sorted(A):
    a = A[k]
    print(f"{' / '.join(k):22s} покупок {a[3]:5d} акций {a[0]:8.0f} выиграло {a[1]/a[0]:6.2%}  P&L {a[2]:+8.1f}$ = {100*a[2]/a[0]:+5.2f}¢ на акцию")
print("крупнейшие тейкеры «замка» (по акциям):")
for wal, e in sorted(who.items(), key=lambda kv: -kv[1][0])[:8]:
    rels = sorted(e[3])
    print(f"  {wal[2:8]}…{wal[-3:]}  покупок {e[4]:4d} акций {e[0]:7.0f} выиграло {e[1]/e[0]:6.2%} P&L {e[2]:+7.1f}$ ({100*e[2]/e[0]:+.2f}¢/акц)  медиана секунды {rels[len(rels)//2]}")
