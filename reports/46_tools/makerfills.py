"""When is a maker fill profitable? All maker records, markout 60 s and to resolution (cents/share, maker direction),
split by: maker's side ahead of the strike or behind (Binance now vs Binance at window start), Binance move over the
3 s before the fill for/against the maker, second of the window, price of the maker's token.
usage: makerfills.py WINDOWS.jsonl BINANCE.json OURWALLET.json"""
import bisect, collections, json, sys
wins = [json.loads(l) for l in open(sys.argv[1])]
bn = {int(k): v for k, v in json.load(open(sys.argv[2])).items()}
ours = json.load(open(sys.argv[3]))[0]
def b(t):
    for k in range(0, 5):
        if t - k in bn: return bn[t - k]
A = collections.defaultdict(lambda: [0.0, 0.0, 0.0])      # key -> [shares, m60*sh, mres*sh]
for w in wins:
    ws, win = w["ws"], w["winner"]
    if win not in ("Up", "Down"): continue
    k0 = b(ws)
    if not k0: continue
    tr = w["trades"]; times = [t[0] for t in tr]; pus = [t[5] if t[3] == "Up" else 1 - t[5] for t in tr]
    outU = 1.0 if win == "Up" else 0.0
    for (t, wal, side, out, size, p, tk) in tr:
        if tk: continue
        rel = t - ws
        if rel < 0 or rel >= 300: continue
        d = 1 if (side == "BUY") == (out == "Up") else -1
        pu = p if out == "Up" else 1 - p
        own = p if side == "BUY" else 1 - p                   # price of the token the maker ends up long
        bt, b3 = b(t), b(t - 3)
        if not bt or not b3: continue
        dist = (bt / k0 - 1) * 1e4 * d                          # bp the maker's side is ahead of the strike
        mv3 = (bt / b3 - 1) * 1e4 * d                           # Binance move in the maker's favour, last 3 s
        tf = t + 60
        f = outU if tf >= ws + 300 else pus[bisect.bisect_right(times, tf) - 1]
        m60 = d * (f - pu) * 100; mres = d * (outU - pu) * 100
        keys = [("всё",),
                ("сторона", "впереди" if dist > 0.5 else "позади" if dist < -0.5 else "у страйка"),
                ("Binance 3с", "против нас ≥1бп" if mv3 <= -1 else "за нас ≥1бп" if mv3 >= 1 else "спокойно"),
                ("секунды", f"{rel//60*60:03d}-{rel//60*60+59}"),
                ("цена", "<0.30" if own < .3 else "0.30-0.45" if own < .45 else "0.45-0.55" if own <= .55 else "0.55-0.70" if own <= .7 else ">0.70"),
                ("впереди+спокойно", "да" if dist > 0.5 and abs(mv3) < 1 else "нет")]
        dd = "<-2" if dist < -2 else "-2..-0.5" if dist < -0.5 else "-0.5..0.5" if dist <= 0.5 else "0.5..2" if dist <= 2 else "2..5" if dist <= 5 else ">5"
        keys = [k for k in keys if k[0] in ("всё",)]
        keys.append(("расстояние до страйка, бп", dd))
        calm = abs(mv3) < 1
        if calm and dist > 0.5:
            keys.append(("впереди+спокойно, секунды", f"{rel//60*60:03d}-{rel//60*60+59}"))
            if 0.40 <= own <= 0.60: keys.append(("впереди+спокойно, цена 0.40-0.60", "да"))
        if calm and dist < -0.5 and 0.40 <= own <= 0.60: keys.append(("позади+спокойно, цена 0.40-0.60", "да"))
        if wal == ours: keys.append(("МЫ", "наши исполнения"))
        for k in keys:
            a = A[k]; a[0] += size; a[1] += m60 * size; a[2] += mres * size
tot = A[("всё",)][0]
for k in sorted(A, key=lambda k: (k[0] != "всё", k)):
    a = A[k]
    print(f"{' / '.join(k):40s} доля {a[0]/tot:6.1%}  маркаут-60 {a[1]/a[0]:+6.2f}¢  до исхода {a[2]/a[0]:+6.2f}¢")
