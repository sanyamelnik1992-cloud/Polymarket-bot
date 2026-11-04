"""Analyse recorder output (live books + Chainlink RTDS + Binance):
  1. feed latency and Chainlink-vs-Binance lead/lag
  2. book quality, complete-set sums
  3. lead-lag Binance -> Polymarket mid (100ms grid)
  4. fair value variants vs the book mid as outcome predictors (log loss on the grid)
  5. sub-second decay of taker edge on live fills (ms match timestamps)

usage: python scripts/analyze_live.py LIVE_DIR [LIVE_DIR ...]
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.fees import fee_per_share  # noqa: E402
from pmbot.gamma import fetch_event, parse_event  # noqa: E402
from pmbot.live_data import load_markets, load_prices, rebuild_books  # noqa: E402
from pmbot.live_grid import GRID_MS, add_fair_value, build_grid, chainlink_fair  # noqa: E402

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)


def section(title):
    print("\n" + "=" * 100 + f"\n{title}\n" + "=" * 100)


def logloss(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def main(dirs):
    allg, alltr, info = [], [], []
    lat_rows = []
    for d in dirs:
        mk = load_markets(d)
        cl, bn = load_prices(d)
        tob, ltr = rebuild_books(d)
        tmax, tmin = tob.rx.max() / 1000, tob.rx.min() / 1000
        mk = mk[(mk.window_start + 360 < tmax) & (mk.window_start - 120 > tmin)]
        cl["lat_ms"] = cl.rx - cl.ts
        lat_rows.append(cl[["sym", "lat_ms"]])
        for _, m in mk.iterrows():
            e = fetch_event(m.slug)
            r = parse_event(e) if e else None
            if not r or r["winner"] is None:
                continue
            g = build_grid(m.asset, m.window_start, tob, bn, cl)
            if g is None:
                continue
            g = add_fair_value(g, bn, m.asset, m.window_start)
            g["p_cl"] = chainlink_fair(g, cl, bn, m.asset, m.window_start, r["price_to_beat"], g.sigma.values)
            y = 1.0 if r["winner"] == "Up" else 0.0
            g["y"] = y
            g["asset"], g["slug"] = m.asset, m.slug
            allg.append(g)
            trs = ltr[(ltr.asset == m.asset) & (ltr.window_start == m.window_start)].sort_values("ts").reset_index(drop=True)
            trs["won"] = np.where(trs.outcome == "U", y, 1 - y)
            # Binance mid and Chainlink-model at (match time - L) for several L
            gi = g[["t", "bn", "p_up", "p_cl", "mid_U", "rel"]].sort_values("t")
            for L in (0, 100, 250, 500, 1000, 2000, 4000):
                q = pd.merge_asof(trs.assign(tq=trs.ts - L).sort_values("tq"), gi, left_on="tq", right_on="t",
                                  direction="backward").sort_values("ts")
                for col in ("p_up", "p_cl"):
                    trs[f"{col}_{L}"] = q[col].values if len(q) else np.nan
            alltr.append(trs.assign(slug=m.slug))
            info.append({"slug": m.slug, "asset": m.asset, "winner": r["winner"]})
    G = pd.concat(allg)
    T = pd.concat(alltr)
    I = pd.DataFrame(info)
    section(f"LIVE: {len(I)} resolved windows {I.asset.value_counts().to_dict()}, grid rows {len(G):,}, live fills {len(T):,}")

    section("1. Feed latency (Chainlink via RTDS: oracle timestamp -> local receive, ms)")
    print(pd.concat(lat_rows).groupby("sym").lat_ms.describe(percentiles=[.5, .9, .99]).round(0))

    section("2. Book quality (in-window medians per window, then median across windows)")
    w = G[(G.rel >= 0) & (G.rel < 300)]
    q = w.groupby(["asset", "slug"]).apply(lambda x: pd.Series({
        "spread_U": (x.ask_U - x.bid_U).median(), "ask_sz_U": x.ask_sz_U.median(), "bid_sz_U": x.bid_sz_U.median(),
        "sum_asks": (x.ask_U + x.ask_D).median(), "min_sum_asks": (x.ask_U + x.ask_D).min(),
        "sum_bids": (x.bid_U + x.bid_D).median(), "max_sum_bids": (x.bid_U + x.bid_D).max()}))
    print(q.groupby("asset").median().round(4))

    section("3. Lead-lag: corr(Binance 500ms log-return, Polymarket Up-mid change over the 500ms ending `lag` later)")
    for a in sorted(G.asset.unique()):
        res = {}
        for lag in (0, 1, 2, 3, 5, 10, 20):
            xs, ys = [], []
            for _, g in G[(G.asset == a) & (G.rel >= 0) & (G.rel < 240)].groupby("slug"):
                rb = np.log(g.bn).diff(5).values
                mid = g.mid_U.values
                dm = np.full(len(mid), np.nan)
                if lag < 5:
                    pass
                dm[:len(mid) - lag] = (pd.Series(mid).shift(-lag) - pd.Series(mid).shift(5 - lag)).values[:len(mid) - lag]
                xs.append(rb)
                ys.append(dm)
            x, yv = np.concatenate(xs), np.concatenate(ys)
            ok = np.isfinite(x) & np.isfinite(yv)
            res[lag * GRID_MS] = np.corrcoef(x[ok], yv[ok])[0, 1] if ok.sum() > 100 else np.nan
        print(f"{a}: " + ", ".join(f"{k}ms:{v:.3f}" for k, v in res.items()))

    section("4. Who predicts the outcome best on the 100ms grid? (log loss; lower = better)")
    w = G[(G.rel >= 2) & (G.rel < 299) & G.mid_U.between(0.005, 0.995)].dropna(subset=["p_up", "p_cl"])
    w["tb"] = pd.cut(w.rel, [0, 60, 120, 180, 240, 270, 290, 300])
    rows = []
    for (a, tb), x in w.groupby(["asset", "tb"], observed=True):
        rows.append({"asset": a, "tb": tb, "n_windows": x.slug.nunique(), "book_mid": logloss(x.mid_U, x.y),
                     "binance_model": logloss(x.p_up, x.y), "chainlink_nowcast": logloss(x.p_cl, x.y)})
    print(pd.DataFrame(rows).round(4).to_string())
    print("\nall:", {k: round(logloss(w[c], w.y), 4) for k, c in [("book_mid", "mid_U"), ("binance", "p_up"), ("chainlink", "p_cl")]})

    section("5. Sub-second decay: taker fills whose side the model favoured by > 3c after fee, info L ms before match")
    T = T[(T.ts / 1000 - T.window_start).between(2, 298)].copy()
    T["fee"] = fee_per_share(T.price)
    sign = np.where(T.side == "BUY", 1.0, -1.0)
    T["pnl_ps"] = sign * (T.won - T.price) - T.fee
    for col in ("p_up", "p_cl"):
        print(f"-- fair value = {col}")
        for L in (0, 100, 250, 500, 1000, 2000, 4000):
            pu = T[f"{col}_{L}"]
            ptok = np.where(T.outcome == "U", pu, 1 - pu)
            edge = sign * (ptok - T.price) - T.fee
            s = T[edge > 0.03]
            if s.empty:
                continue
            pm = s.groupby("slug").apply(lambda z: (z.pnl_ps * z["size"].clip(upper=100)).sum())
            tstat = pm.mean() / pm.std() * np.sqrt(len(pm)) if len(pm) > 2 and pm.std() > 0 else np.nan
            print(f"   L={L:5d}ms fills={len(s):6d} windows={len(pm):3d} pnl/share={np.average(s.pnl_ps, weights=s['size']):+.4f} "
                  f"roi={(s.pnl_ps * s['size']).sum() / (s.price * s['size']).sum():+.4f} t={tstat:+.2f}")
            if col == "p_cl" and L in (0, 250, 1000):
                for a, sa in s.groupby("asset"):
                    pa = sa.groupby("slug").apply(lambda z: (z.pnl_ps * z["size"].clip(upper=100)).sum())
                    ta = pa.mean() / pa.std() * np.sqrt(len(pa)) if len(pa) > 2 and pa.std() > 0 else np.nan
                    print(f"        {a}: fills={len(sa):5d} windows={len(pa):3d} "
                          f"roi={(sa.pnl_ps * sa['size']).sum() / (sa.price * sa['size']).sum():+.4f} "
                          f"pnl=${(sa.pnl_ps * sa['size']).sum():+.0f} t={ta:+.2f}")


if __name__ == "__main__":
    main(sys.argv[1:])
