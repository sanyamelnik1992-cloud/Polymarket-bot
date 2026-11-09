"""(1) Taker race: per window, first and last taker buy of the winner at 0.90-0.97 in seconds 240-299 -> how long
cheap asks live. (2) Maker lock: maker buys at 0.88-0.95 in seconds 240-295 by Binance distance of the bought side
from the strike proxy (Binance at window start) and Binance 3 s move. usage: lockmaker.py WINDOWS.jsonl BINANCE.json"""
import collections, json, sys
wins = [json.loads(l) for l in open(sys.argv[1])]
bn = {int(k): v for k, v in json.load(open(sys.argv[2])).items()}
def b(t):
    for k in range(5):
        if t - k in bn: return bn[t - k]
fee = lambda p: 0.07 * p * (1 - p)
spans, firsts = [], []
M = collections.defaultdict(lambda: [0.0, 0.0, 0.0, set()])
for w in wins:
    ws, win = w["ws"], w["winner"]
    if win not in ("Up", "Down"): continue
    k0 = b(ws)
    cheap = [t - ws for (t, wal, side, out, size, p, tk) in w["trades"]
             if tk and side == "BUY" and out == win and 240 <= t - ws < 300 and 0.90 <= p <= 0.97]
    if cheap:
        spans.append(max(cheap) - min(cheap)); firsts.append(min(cheap))
    if not k0: continue
    for (t, wal, side, out, size, p, tk) in w["trades"]:
        rel = t - ws
        if tk or side != "BUY" or not (240 <= rel < 295) or not (0.88 <= p <= 0.95): continue
        bt, b3 = b(t), b(t - 3)
        if not bt or not b3: continue
        d = 1 if out == "Up" else -1
        dist = (bt / k0 - 1) * 1e4 * d; mv3 = (bt / b3 - 1) * 1e4 * d
        won = out == win
        pnl = size * ((1 - p) if won else -p) + 0.2 * size * fee(p)
        db = "≤0" if dist <= 0 else "0-2" if dist <= 2 else "2-5" if dist <= 5 else ">5"
        calm = "спокойно" if abs(mv3) < 1 else "движение"
        for k in [("все",), ("расстояние", db), ("расст.+Binance", db, calm)]:
            a = M[k]; a[0] += size; a[1] += size if won else 0; a[2] += pnl; a[3].add(ws)
spans.sort(); firsts.sort()
q = lambda xs, f: xs[min(len(xs) - 1, int(f * len(xs)))]
print(f"гонка тейкеров: окон с дешёвыми продажами победителя {len(spans)}; первая покупка: медиана секунда {q(firsts,.5)}; "
      f"дешёвые продажи живут (последняя минус первая покупка): медиана {q(spans,.5)} с, p25 {q(spans,.25)} с, p75 {q(spans,.75)} с")
print("мейкерский «замок» (биды 0.88-0.95, секунды 240-294):")
for k in sorted(M):
    a = M[k]
    print(f"  {' / '.join(k):28s} окон {len(a[3]):3d} акций {a[0]:7.0f} выиграло {a[1]/a[0]:6.2%}  {100*a[2]/a[0]:+6.2f}¢ на акцию  P&L {a[2]:+7.0f}$")
