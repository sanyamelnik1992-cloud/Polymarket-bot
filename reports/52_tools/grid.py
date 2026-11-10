"""State grid for rule research: for every window and every 100 ms step of the closing minute, the production model
state (pmbot.bot.lock.decide_state with all filters opened) + the book of the favoured side now and 300 ms later.
One row per step where the model favours a side with p >= P_MIN. Any entry rule can then be evaluated in pandas.

Also stored: the model probability with other sigmas (p60, p300, px15, px2) and the sigma-free thresholds y_nc / y_cl,
so p can be recomputed for any sigma floor (sim.p_sigma).

usage: python reports/52_tools/grid.py DATA_DIR PERIOD MARKETS.parquet OUT_DIR/PERIOD.parquet
(DATA_DIR holds PERIOD_tob.csv, PERIOD_chainlink.jsonl, PERIOD_binance.jsonl: reports/51_tools/extract_day.py + prep_day.py)
"""
import bisect
import json
import math
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO)
from pmbot.bot.lock import LockCfg, decide_state  # noqa: E402
from pmbot.config import BINANCE_SYMBOL, CHAINLINK_SYMBOL  # noqa: E402
from pmbot.lockmodel import M, ClSigma, Tables, lock_state, prob_up, strike_from_prints, vfac  # noqa: E402

P_MIN = 0.80
STEP = 100
EXEC = 300


def jl(path):
    with open(path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                pass


def main(data, period, markets, out):
    c = LockCfg(theta=P_MIN, agree=0.0, dmin_bp=-1e9, adv1_bp=1e9, adv3_bp=1e9, jump_bp=1e9, a_min=0.01)
    tables = Tables(os.path.join(REPO, c.tables))
    mk = pd.read_parquet(os.path.join(data, markets)).dropna(subset=["winner"])
    win = {(r.asset, int(r.window_start)): r.winner for r in mk.itertuples()}
    prefix = os.path.join(data, period)
    cl = pd.DataFrame(jl(prefix + "_chainlink.jsonl"))
    bn = pd.DataFrame(jl(prefix + "_binance.jsonl"))
    bn["mid"] = (bn.bid + bn.ask) / 2
    tob = pd.read_csv(prefix + "_tob.csv")
    tob = tob[~tob.k.str.startswith("GAP-")]
    rows = []
    cols = ["bid", "bid_sz", "ask", "ask_sz", "ask2", "ask2_sz"]
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
        sig60, sig300 = ClSigma(halflife=60.0), ClSigma(halflife=300.0)
        pi = 0
        last = None
        ta = tob[tob.k.str.startswith(asset + "-")]
        books = {k: (g.rx.values, g[cols].values.astype(float)) for k, g in ta.groupby("k")}

        def book_at(k, t):
            if k not in books:
                return None
            rx, v = books[k]
            i = np.searchsorted(rx, t, side="right") - 1
            if i < 0:
                return None
            return v[i]

        Ts = sorted({int(k.split("-")[1]) for k in books})
        for T in Ts:
            if (asset, T) not in win:
                continue
            t = (T + 240) * 1000
            K = None
            while t <= (T + 299.5) * 1000:
                while pi < len(prints) and prints[pi][0] <= t:
                    rx, sec, ts, v = prints[pi]
                    cl_by_sec.setdefault(sec, v)
                    if last is None or ts >= last[1]:
                        last = (v, ts, rx)
                    for sg_ in (sig, sig60, sig300):
                        if sg_.last_sec is None:
                            sg_.on_print(sec, v)
                        else:
                            for s in range(sg_.last_sec + 1, sec + 1):
                                sg_.on_print(s, cl_by_sec.get(s, sg_.last_v))
                    pi += 1
                if K is None:
                    K = strike_from_prints(cl_by_sec, T)
                side, p, ctx = decide_state(c, tables, asset, T, K, t, cl_by_sec, last,
                                            sig.sigma() if sig.n >= c.min_sigma_prints else None,
                                            bn_at(t), bn_at(last[1] + 999) if last else None, bn_at(t - 1000),
                                            bn_at(t - 3000))
                if side is not None:
                    tok = f"{asset}-{T}-{side}"
                    b0, b1 = book_at(tok, t), book_at(tok, t + EXEC)
                    if b0 is not None and np.isfinite(b0[2]):
                        r = {"asset": asset, "T": T, "t": t, **{k: v for k, v in ctx.items() if k != "why"},
                             "bid": b0[0], "ask": b0[2], "ask_sz": b0[3],
                             "won": (side == "U") == (win[(asset, T)] == "Up")}
                        st = lock_state(cl_by_sec, last[1] // 1000, last[0], T, K, t / 1000.0, bn_at(t), bn_at(last[1] + 999))
                        s0, s6, s3 = sig.sigma(), sig60.sigma() or 0.0, sig300.sigma() or 0.0
                        for nm, sv in (("p60", max(s0, s6)), ("p300", max(s0, s3)), ("px15", 1.5 * s0), ("px2", 2 * s0)):
                            q = prob_up(tables, asset, st["A_nc"], st["m_nc"], st["S_nc"], K, sv)
                            r[nm] = q if side == "U" else 1 - q
                        r["sig60"], r["sig300"] = s6 * 1e4, s3 * 1e4
                        mn, mc = st["m_nc"], st["m_cl"]
                        # sigma-free thresholds: p_up(sigma) = surv(asset, m, y / sigma)
                        r["m_nc"], r["m_cl"] = mn, mc
                        r["y_nc"] = ((M * K - st["A_nc"]) / (mn * st["S_nc"]) - 1.0) / math.sqrt(vfac(mn)) if mn > 0 else float("nan")
                        r["y_cl"] = ((M * K - st["A"]) / (mc * st["S_cl"]) - 1.0) / math.sqrt(vfac(mc)) if mc > 0 else float("nan")
                        # the same state if the exchange averaged one second later: strike [T-61, T-2], close [T+239, T+298]
                        # (a third of 806 windows of 7-8 Nov match that window exactly, the rest match [T-62, T-3])
                        K1 = strike_from_prints(cl_by_sec, T + 1)
                        if K1 is not None:
                            s1 = lock_state(cl_by_sec, last[1] // 1000, last[0], T + 1, K1, t / 1000.0, bn_at(t), bn_at(last[1] + 999))
                            m1, c1 = s1["m_nc"], s1["m_cl"]
                            r["m1_nc"], r["m1_cl"] = m1, c1
                            r["y1_nc"] = ((M * K1 - s1["A_nc"]) / (m1 * s1["S_nc"]) - 1.0) / math.sqrt(vfac(m1)) if m1 > 0 else float("nan")
                            r["y1_cl"] = ((M * K1 - s1["A"]) / (c1 * s1["S_cl"]) - 1.0) / math.sqrt(vfac(c1)) if c1 > 0 else float("nan")
                            r["dist1"] = (1.0 if side == "U" else -1.0) * ((s1["A_nc"] + m1 * s1["S_nc"]) / M / K1 - 1) * 1e4
                            r["k1_bp"] = (K1 / K - 1) * 1e4
                        # Binance shock: the largest |1 s| and |3 s| move that ENDED in the last 5 / 10 s (bp), sampled every 0.5 s
                        for look in (5, 10):
                            m1 = m3 = 0.0
                            for k in range(0, look * 2 + 1):
                                u = t - k * 500
                                a0, a1, a3 = bn_at(u), bn_at(u - 1000), bn_at(u - 3000)
                                if a0 and a1:
                                    m1 = max(m1, abs(math.log(a0 / a1)) * 1e4)
                                if a0 and a3:
                                    m3 = max(m3, abs(math.log(a0 / a3)) * 1e4)
                            r[f"sh1_{look}"], r[f"sh3_{look}"] = m1, m3
                        if b1 is not None:
                            r.update(x_ask=b1[2], x_sz=b1[3], x_ask2=b1[4], x_sz2=b1[5])
                        rows.append(r)
                t += STEP
        print(period, asset, len(rows), flush=True)
    pd.DataFrame(rows).to_parquet(out)


if __name__ == "__main__":
    main(*sys.argv[1:5])
