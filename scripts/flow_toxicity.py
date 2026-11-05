"""Who loses to whom on the 5m books, and how much does a fast (co-located) maker save?

For every public trade on a live 5m window (exchange match ts T) we compute, in "Up" terms:
  dir      +1 if the taker bought Up (BUY U or SELL D), -1 otherwise
  spread   what the maker earned vs the pre-trade mid:     -dir * (mid(T-) - p_up)
  m_Δ      the maker's markout Δ seconds later:             -dir * (mid(T+Δ) - p_up)
  bn_W     signed Binance log-move (bp) in a window before T, multiplied by dir
           (> 0 = the taker traded in the direction Binance had just moved = latency-informed flow)
Windows (Binance event time E, ms before T):
  late  [T-250, T]      a taker cannot have acted on this (150 ms taker delay + >= ~105 ms Tokyo->London)
  colo  [T-450, T-250]  visible to a London/Dublin maker in time to cancel (reacts ~110-130 ms after E),
                        but NOT to a slow maker (here: ~70 ms feed + 200 ms tick + 150 ms order = ~420 ms)
  early [T-2000, T-450] any maker could have reacted
Then: maker markout if it skips trades that follow a move > thr in the windows it can see in time,
for reaction R = 130 ms (Dublin/London), 420 ms (slow server) and none.

usage: python scripts/flow_toxicity.py REC_DIR [assets=btc,eth,sol] [--thr=1.0]
Uses binance_trades.jsonl (true event time E) when present, else binance.jsonl mids at rx-70 ms.
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
HORIZONS = (1, 5, 30)


def load_binance(d, assets):
    out = {}
    rev = {SYM[a]: a for a in assets}
    rows = defaultdict(list)
    if os.path.exists(f"{d}/binance_trades.jsonl"):
        src = "aggTrade E"
        for line in open(f"{d}/binance_trades.jsonl"):
            e = json.loads(line)
            a = rev.get(e["sym"])
            if a:
                rows[a].append((e["E"], e["p"]))
    else:
        src = "bookTicker rx-70"
        for line in open(f"{d}/binance.jsonl"):
            e = json.loads(line)
            a = rev.get(e["sym"])
            if a:
                rows[a].append((e["rx"] - 70, (e["bid"] + e["ask"]) / 2))
    for a, r in rows.items():
        x = np.array(r, dtype=float)
        x = x[np.argsort(x[:, 0], kind="stable")]
        out[a] = x
    return out, src


def load_clob(d, assets):
    mids = defaultdict(list)     # key-U -> [(ts, mid)]
    trades = []
    pre = tuple(a + "-" for a in assets)
    for line in open(f"{d}/clob.jsonl"):
        if '"e":"pc"' not in line and '"e":"trade"' not in line and '"e":"bba"' not in line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = e["e"]
        if t == "pc":
            for k, _p, _s, _side, bb, ba in e["c"]:
                if k.endswith("-U") and k.startswith(pre) and bb > 0 and ba > 0:
                    mids[k].append((e["ts"], (bb + ba) / 2))
        elif t == "bba":
            k = e["k"]
            if k.endswith("-U") and k.startswith(pre) and e["b"] > 0 and e["a"] > 0:
                mids[k].append((e["ts"], (e["b"] + e["a"]) / 2))
        elif t == "trade" and e["k"].startswith(pre) and e.get("side") in ("BUY", "SELL"):
            trades.append((e["k"], e["ts"], e["p"], e["s"], e["side"]))
    mids = {k: np.array(v, dtype=float) for k, v in mids.items()}
    for k in mids:
        mids[k] = mids[k][np.argsort(mids[k][:, 0], kind="stable")]
    return mids, trades


def px_at(arr, t, strict=False):
    i = np.searchsorted(arr[:, 0], t, side="left" if strict else "right") - 1
    return arr[i, 1] if i >= 0 else np.nan


def build(d, assets):
    bn, src = load_binance(d, assets)
    mids, trades = load_clob(d, assets)
    rows = []
    for k, ts, p, s, side in trades:
        asset, ws, x = k.split("-")
        ws = int(ws)
        rel = ts / 1000 - ws
        if not 0 <= rel < 300 or asset not in bn:
            continue
        mk = f"{asset}-{ws}-U"
        m = mids.get(mk)
        if m is None or len(m) < 2:
            continue
        p_up = p if x == "U" else 1 - p
        if not 0.03 <= p_up <= 0.97:
            continue
        dirn = 1 if (side == "BUY") == (x == "U") else -1
        r = {"asset": asset, "ws": ws, "rel": rel, "ts": ts, "p_up": p_up, "size": s, "dir": dirn,
             "spread": -dirn * (px_at(m, ts, strict=True) - p_up)}
        for h in HORIZONS:
            if rel + h <= 300:
                r[f"m{h}"] = -dirn * (px_at(m, ts + h * 1000) - p_up)
            else:
                r[f"m{h}"] = np.nan
        b = bn[asset]
        def mv(a_ms, b_ms):
            p0, p1 = px_at(b, ts - a_ms), px_at(b, ts - b_ms)
            return dirn * np.log(p1 / p0) * 1e4 if p0 > 0 and p1 > 0 else np.nan
        r["bn_late"] = mv(250, 0)
        r["bn_colo"] = mv(450, 250)
        r["bn_early"] = mv(2000, 450)
        r["bn_0_130"] = mv(130, 0)
        r["bn_130_420"] = mv(420, 130)
        r["bn_420_2000"] = mv(2000, 420)
        rows.append(r)
    return pd.DataFrame(rows), src


def wavg(g, col):
    ok = g[col].notna()
    return (g.loc[ok, col] * g.loc[ok, "size"]).sum() / g.loc[ok, "size"].sum() if ok.any() else np.nan


def report(df, thr):
    print(f"trades {len(df)}, shares {df['size'].sum():.0f}")
    print("size-weighted maker PnL per share (cents): spread vs pre-trade mid, markout at 1/5/30 s")
    print("  all:", {c: round(100 * wavg(df, c), 2) for c in ["spread", "m1", "m5", "m30"]})
    for w in ("late", "colo", "early"):
        col = f"bn_{w}"
        z = df[col]
        bins = [-np.inf, -thr, -0.25, 0.25, thr, np.inf]
        lab = [f"against<-{thr}", "small-", "flat", "small+", f"with>+{thr}"]
        g = df.assign(b=pd.cut(z, bins, labels=lab)).groupby("b", observed=True)
        t = pd.DataFrame({"n": g.size(), "share_vol": g["size"].sum() / df["size"].sum(),
                          "spread_c": g.apply(lambda x: 100 * wavg(x, "spread"), include_groups=False),
                          "m5_c": g.apply(lambda x: 100 * wavg(x, "m5"), include_groups=False),
                          "m30_c": g.apply(lambda x: 100 * wavg(x, "m30"), include_groups=False)})
        print(f"  Binance move (bp, signed with taker) in window '{w}':")
        print("    " + t.round(3).to_string().replace("\n", "\n    "))
    # maker that skips latency-informed trades it could have seen in time
    print(f"  maker that pulls the threatened side after a Binance move > {thr} bp it can see in time "
          "(PnL per share at 30 s, cents; volume kept):")
    base = wavg(df, "m30")
    for name, cols in [("no reaction", []), ("slow, R=420 ms", ["bn_420_2000"]),
                       ("Dublin/London, R=130 ms", ["bn_420_2000", "bn_130_420"]),
                       ("oracle, R=0 (impossible)", ["bn_420_2000", "bn_130_420", "bn_0_130"])]:
        keep = np.ones(len(df), bool)
        for c in cols:
            keep &= ~(df[c] > thr)
        k = df[keep]
        print(f"    {name:26s} m30 {100 * wavg(k, 'm30'):6.2f}c  m5 {100 * wavg(k, 'm5'):6.2f}c  "
              f"vol kept {k['size'].sum() / df['size'].sum():.2f}  (all trades m30 {100 * base:.2f}c)")


def main():
    d = sys.argv[1]
    pos = [a for a in sys.argv[2:] if not a.startswith("--")]
    assets = pos[0].split(",") if pos else ["btc", "eth", "sol"]
    thr = float(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--thr=")), 1.0))
    df, src = build(d, assets)
    print(f"# {d}  binance source: {src}  thr {thr} bp")
    for a in assets:
        print(f"\n#### {a}")
        report(df[df.asset == a], thr)
    print("\n#### pooled")
    report(df, thr)
    out = os.path.join(d, "flow_toxicity.csv")
    try:
        df.to_csv(out, index=False)
    except OSError:
        pass


if __name__ == "__main__":
    main()
