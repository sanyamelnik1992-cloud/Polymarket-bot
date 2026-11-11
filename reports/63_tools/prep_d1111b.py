"""Chainlink/Binance slices + winners (our TWAP rule) for period d1111b = 11 Nov 06:55 -> 17:55 UTC windows."""
import json

import pandas as pd

REC = "/home/pmbot/Polymarket-bot/data/rec"
OUT = "/root/research/d1111b"
T0_MS = 1794378300000          # 06:25 UTC Nov 11 (sigma warm-up before the first window 06:55)
T_FIRST, T_LAST = 1794380100, 1794419700     # windows 06:55 ... 17:55
dirs = ["20261111-0600", "20261111-1200", "20261111-1800"]
for name in ("chainlink", "binance"):
    with open(f"{OUT}_{name}.jsonl", "w") as g:
        for d in dirs:
            for line in open(f"{REC}/{d}/{name}.jsonl"):
                i = line.find('"rx":')
                if i > 0 and int(line[i + 5:i + 18]) >= T0_MS:
                    g.write(line)
cl = {}
for line in open(f"{OUT}_chainlink.jsonl"):
    e = json.loads(line)
    cl.setdefault(e["sym"].split("/")[0], {})[e["ts"] // 1000] = e["v"]


def mean(a, s0, s1):
    xs, last = [], None
    for s in range(s0 - 10, s0):
        last = cl[a].get(s, last)
    for s in range(s0, s1 + 1):
        v = cl[a].get(s, last)
        if v is None:
            return None
        xs.append(v)
        last = v
    return sum(xs) / len(xs)


rows = []
for a in ("btc", "eth", "sol", "xrp"):
    for T in range(T_FIRST, T_LAST + 1, 300):
        k, f = mean(a, T - 62, T - 3), mean(a, T + 238, T + 297)
        if k and f:
            rows.append({"asset": a, "window_start": T, "winner": "Up" if f >= k else "Down",
                         "price_to_beat": k, "final": f})
pd.DataFrame(rows).to_parquet(OUT + "_markets.parquet")
print("windows", len(rows), "last", max(r["window_start"] for r in rows))
