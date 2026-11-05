"""How fast do Polymarket 5m market makers react to Binance moves?  (exchange timestamps on both sides)

Binance aggTrade event time E (recorder >= run5) vs Polymarket price_change engine timestamps:
for every sharp Binance move (> thr_bp within 200 ms, separated by >= 2 s) find the first change of the
Up-token best bid/ask in the direction of the move. Delay = PM engine ts - Binance E.
Physical floor: Tokyo -> London ~105 ms one way, so a delay well below that means anticipation (other venues)
or clock offset; ~110-150 ms means London-hosted makers reacting to Binance; > 300 ms = slow book.

usage: python scripts/mm_reaction_time.py REC_DIR [asset=btc] [thr_bp=2.0]
"""
import json
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}


def main(d, asset="btc", thr_bp=2.0):
    trades = []
    for line in open(f"{d}/binance_trades.jsonl"):
        e = json.loads(line)
        if e["sym"] == SYM[asset]:
            trades.append((e["E"], e["p"], e["rx"]))
    bn = pd.DataFrame(trades, columns=["E", "p", "rx"]).sort_values("E")
    print(f"binance trades {len(bn)}; rx-E median {np.median(bn.rx - bn.E):.0f} ms")
    # PM best bid/ask of the Up token by engine ts
    pm = defaultdict(list)
    for line in open(f"{d}/clob.jsonl"):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("e") != "pc":
            continue
        for k, _p, _s, _side, bb, ba in e["c"]:
            if k.startswith(asset + "-") and k.endswith("-U"):
                pm[k].append((e["ts"], bb, ba))
    # sharp moves on Binance (event time)
    s = pd.Series(bn.p.values, index=bn.E.values)
    s = s[~s.index.duplicated(keep="last")]
    t_idx = s.index.values
    p = s.values
    j = np.searchsorted(t_idx, t_idx - 200)
    ret = np.log(p / p[np.clip(j, 0, len(p) - 1)]) * 1e4
    events, last = [], -1e18
    for t, r in zip(t_idx, ret):
        if abs(r) >= thr_bp and t - last > 2000:
            events.append((t, np.sign(r)))
            last = t
    print(f"sharp moves (>= {thr_bp} bp in 200 ms): {len(events)}")
    rows = []
    for t, sgn in events:
        ws = int(t // 1000 - (t // 1000) % 300)
        k = f"{asset}-{ws}-U"
        series = pm.get(k)
        if not series:
            continue
        arr = np.array(series, dtype=float)
        before = arr[arr[:, 0] <= t]
        if len(before) == 0:
            continue
        bb0, ba0 = before[-1, 1], before[-1, 2]
        after = arr[(arr[:, 0] > t) & (arr[:, 0] <= t + 5000)]
        hit = None
        for ts, bb, ba in after:
            # Up gets more valuable when Binance rises: makers lift asks/bids; falls -> they drop
            moved = (sgn > 0 and (ba > ba0 or bb > bb0)) or (sgn < 0 and (bb < bb0 or ba < ba0))
            if moved:
                hit = ts - t
                break
        rows.append({"t": t, "dir": sgn, "delay_ms": hit, "rel_s": t / 1000 - ws})
    r = pd.DataFrame(rows)
    ok = r.dropna(subset=["delay_ms"])
    print(f"moves inside a 5m window: {len(r)}, book reacted within 5 s: {len(ok)}")
    if len(ok):
        print("delay Binance E -> first Polymarket quote move (ms):",
              ok.delay_ms.describe(percentiles=[.1, .25, .5, .75, .9]).round(0).to_dict())
        for lim in (50, 105, 150, 250, 500, 1000):
            print(f"   share reacted within {lim:4d} ms: {(ok.delay_ms <= lim).mean():.2f}")


if __name__ == "__main__" and "--xcorr" not in sys.argv:
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "btc", float(sys.argv[3]) if len(sys.argv) > 3 else 2.0)


def xcorr(d, asset="btc", bin_ms=10, lags=(-200, -100, -50, 0, 25, 50, 75, 100, 125, 150, 200, 250, 300, 400, 600, 1000)):
    """Lead-lag in exchange time: corr(Binance log-return in a 50 ms bucket, PM Up-mid change in the bucket
    starting `lag` ms later). The peak lag is the book's typical reaction delay to Binance."""
    bn = [(e["E"], e["p"]) for e in map(json.loads, open(f"{d}/binance_trades.jsonl")) if e["sym"] == SYM[asset]]
    bn = pd.DataFrame(bn, columns=["t", "p"]).sort_values("t")
    pm = []
    for line in open(f"{d}/clob.jsonl"):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("e") != "pc":
            continue
        for k, _p, _s, _side, bb, ba in e["c"]:
            if k.startswith(asset + "-") and k.endswith("-U") and bb and ba:
                ws = int(k.split("-")[1])
                if 0 <= e["ts"] / 1000 - ws < 300:     # only the live window
                    pm.append((e["ts"], (bb + ba) / 2))
    pm = pd.DataFrame(pm, columns=["t", "mid"]).sort_values("t")
    t0, t1 = max(bn.t.min(), pm.t.min()), min(bn.t.max(), pm.t.max())
    grid = np.arange(t0, t1, bin_ms)
    bp = pd.Series(bn.p.values, index=bn.t.values)
    bp = bp[~bp.index.duplicated(keep="last")].reindex(grid, method="ffill")
    mp = pd.Series(pm.mid.values, index=pm.t.values)
    mp = mp[~mp.index.duplicated(keep="last")].reindex(grid, method="ffill")
    w = 50 // bin_ms
    rb = np.log(bp).diff(w).values
    out = {}
    for lag in lags:
        sh = lag // bin_ms
        dm = (mp.shift(-sh) - mp.shift(-sh + w)).values
        ok = np.isfinite(rb) & np.isfinite(dm)
        out[lag] = np.corrcoef(rb[ok], dm[ok])[0, 1] if ok.sum() > 1000 else np.nan
    print(f"{asset}: corr(Binance 50ms return at E, PM mid change {bin_ms}ms-binned, by lag ms):")
    print("   " + ", ".join(f"{k}:{v:.3f}" for k, v in out.items()))
    best = max((v, k) for k, v in out.items() if np.isfinite(v))
    print(f"   peak at {best[1]} ms (corr {best[0]:.3f})")


if __name__ == "__main__" and "--xcorr" in sys.argv:
    xcorr(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else "btc")
