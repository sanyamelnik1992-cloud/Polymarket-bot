"""Maker bids on the decided side in seconds 240-294 by price level and second, for 'decided' = Binance distance
of the bought side >= 2 bp from the strike proxy and |Binance 3 s move| < 1 bp. usage: lockprice.py WINDOWS BINANCE"""
import collections, json, sys
wins = [json.loads(l) for l in open(sys.argv[1])]
bn = {int(k): v for k, v in json.load(open(sys.argv[2])).items()}
def b(t):
    for k in range(5):
        if t - k in bn: return bn[t - k]
fee = lambda p: 0.07 * p * (1 - p)
M = collections.defaultdict(lambda: [0.0, 0.0, 0.0, set(), 0])
for w in wins:
    ws, win = w["ws"], w["winner"]
    if win not in ("Up", "Down"): continue
    k0 = b(ws)
    if not k0: continue
    for (t, wal, side, out, size, p, tk) in w["trades"]:
        rel = t - ws
        if tk or side != "BUY" or not (240 <= rel < 295) or not (0.85 <= p <= 0.99): continue
        bt, b3 = b(t), b(t - 3)
        if not bt or not b3: continue
        d = 1 if out == "Up" else -1
        dist = (bt / k0 - 1) * 1e4 * d; mv3 = (bt / b3 - 1) * 1e4 * d
        if dist < 2 or abs(mv3) >= 1: continue
        won = out == win
        pnl = size * ((1 - p) if won else -p) + 0.2 * size * fee(p)
        pb = "0.85-0.91" if p < 0.915 else "0.92-0.94" if p < 0.945 else "0.95-0.96" if p < 0.965 else "0.97" if p < 0.975 else "0.98" if p < 0.985 else "0.99"
        sb = "240-259" if rel < 260 else "260-279" if rel < 280 else "280-294"
        for k in [("цена", pb), ("секунды", sb)]:
            a = M[k]; a[0] += size; a[1] += size if won else 0; a[2] += pnl; a[3].add(ws); a[4] += 0 if won else 1
for k in sorted(M):
    a = M[k]
    print(f"  {' / '.join(k):18s} окон {len(a[3]):3d} акций {a[0]:7.0f} выиграло {a[1]/a[0]:7.2%} проигр.сделок {a[4]:3d}  {100*a[2]/a[0]:+6.2f}¢ на акцию")
