"""Replay the production TWAP-lock decision (pmbot.bot.lock.decide + config/lock_tables.npz) on a recorder
extraction: late top of book (PREFIX_tob.csv, from the late-window extractor), Chainlink and Binance as received.
Entry = FAK matched against the book exec_ms later (2 levels). Outcome = Gamma winner from a markets parquet.

  python scripts/replay_lock.py PREFIX MARKETS.parquet [config/lock.toml] [shares]
"""
import bisect
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.bot.lock import decide, load_lock_config  # noqa: E402
from pmbot.config import BINANCE_SYMBOL, CHAINLINK_SYMBOL  # noqa: E402
from pmbot.fees import fee_per_share  # noqa: E402
from pmbot.lockmodel import ClSigma, Tables, strike_from_prints  # noqa: E402


def jl(path):
    with open(path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                pass


def main(prefix, markets, cfg_path="config/lock.toml", shares=10.0, exec_ms=300, step_ms=100):
    cfg = load_lock_config(cfg_path)
    c = cfg.lock
    tables = Tables(os.path.join(os.path.dirname(__file__), "..", c.tables))
    mk = pd.read_parquet(markets).dropna(subset=["winner"])
    win = {(r.asset, int(r.window_start)): r.winner for r in mk.itertuples()}
    cl = pd.DataFrame(jl(prefix + "_chainlink.jsonl"))
    bn = pd.DataFrame(jl(prefix + "_binance.jsonl"))
    bn["mid"] = (bn.bid + bn.ask) / 2
    tob = pd.read_csv(prefix + "_tob.csv")
    tob = tob[~tob.k.str.startswith("GAP-")]
    rows = []
    for asset in c.assets:
        a_cl = cl[cl.sym == CHAINLINK_SYMBOL[asset]].sort_values("rx")
        a_bn = bn[bn.sym == BINANCE_SYMBOL[asset]].sort_values("rx")
        b_rx, b_mid = a_bn.rx.values.tolist(), a_bn.mid.values.tolist()

        def bn_at(t):
            i = bisect.bisect_right(b_rx, t) - 1
            return b_mid[i] if i >= 0 else None

        prints = list(zip(a_cl.rx.values.tolist(), (a_cl.ts.values // 1000).tolist(), a_cl.ts.values.tolist(),
                          a_cl.v.values.tolist()))
        cl_by_sec, sig = {}, ClSigma()
        pi = 0
        last = None
        ta = tob[tob.k.str.startswith(asset + "-")]
        books = {k: (g.rx.values, g[["ask", "ask_sz", "ask2", "ask2_sz"]].values) for k, g in ta.groupby("k")}

        def ask_at(k, t):
            if k not in books:
                return None
            rx, v = books[k]
            i = np.searchsorted(rx, t, side="right") - 1
            if i < 0 or not np.isfinite(v[i][0]):
                return None
            return v[i]

        Ts = sorted({int(k.split("-")[1]) for k in books})
        for T in Ts:
            if (asset, T) not in win:
                continue
            t = (T + c.rel_lo) * 1000
            entered = False
            next_try = 0
            while t <= (T + c.rel_hi) * 1000 and not entered:
                while pi < len(prints) and prints[pi][0] <= t:
                    rx, sec, ts, v = prints[pi]
                    cl_by_sec.setdefault(sec, v)
                    if last is None or ts >= last[1]:
                        last = (v, ts, rx)
                    if sig.last_sec is None:
                        sig.on_print(sec, v)
                    else:
                        for s in range(sig.last_sec + 1, sec + 1):
                            sig.on_print(s, cl_by_sec.get(s, sig.last_v))
                    pi += 1
                if t < next_try:
                    t += step_ms
                    continue
                K = strike_from_prints(cl_by_sec, T)
                au, ad = ask_at(f"{asset}-{T}-U", t), ask_at(f"{asset}-{T}-D", t)
                s_, ctx = decide(c, tables, asset, T, K, t, cl_by_sec, last,
                                 sig.sigma() if sig.n >= c.min_sigma_prints else None,
                                 bn_at(t), bn_at(last[1] + 999) if last else None, bn_at(t - 1000), bn_at(t - 3000),
                                 (au[0], au[1]) if au is not None else None, (ad[0], ad[1]) if ad is not None else None)
                if s_:
                    tok = f"{asset}-{T}-{s_['side']}"
                    v = ask_at(tok, t + exec_ms)
                    got = cost = fee = 0.0
                    if v is not None:
                        for px, sz in ((v[0], v[1]), (v[2], v[3])):
                            if np.isfinite(px) and px <= s_["limit"] + 1e-9 and got < shares:
                                q = min(shares - got, sz)
                                got += q
                                cost += q * px
                                fee += q * fee_per_share(px)
                    if got > 0:
                        won = (s_["side"] == "U") == (win[(asset, T)] == "Up")
                        rows.append({"asset": asset, "T": T, **ctx, "px": cost / got, "sh": got, "won": won,
                                     "pnl": (got if won else 0.0) - cost - fee})
                        entered = True
                    else:
                        next_try = t + c.retry_ms
                t += step_ms
    r = pd.DataFrame(rows)
    if r.empty:
        print("no entries")
        return r
    print(f"entries {len(r)} lost {int((~r.won).sum())} win% {100 * r.won.mean():.2f} px {r.px.mean():.3f} "
          f"pnl ${r.pnl.sum():+.2f} EV/sh {100 * r.pnl.sum() / r.sh.sum():+.2f}c")
    print(r.groupby("asset").agg(n=("won", "size"), lost=("won", lambda x: int((~x).sum())), pnl=("pnl", "sum")).round(2))
    return r


if __name__ == "__main__":
    a = sys.argv[1:]
    out = main(a[0], a[1], a[2] if len(a) > 2 else "config/lock.toml", float(a[3]) if len(a) > 3 else 10.0)
    if len(out):
        out.to_csv(a[0] + "_lock_replay.csv", index=False)
