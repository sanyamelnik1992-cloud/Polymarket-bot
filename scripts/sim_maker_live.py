"""Paper-trade the model-based two-bid market maker on recorded live books & trades.

usage: python scripts/sim_maker_live.py LIVE_DIR
"""
import itertools
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.gamma import fetch_event, parse_event  # noqa: E402
from pmbot.live_data import load_markets, load_prices, rebuild_books  # noqa: E402
from pmbot.live_grid import add_fair_value, build_grid, chainlink_fair  # noqa: E402
from pmbot.maker_sim import MakerParams, simulate_window  # noqa: E402

pd.set_option("display.width", 220)


def main(d):
    mk = load_markets(d)
    cl, bn = load_prices(d)
    tob, ltr = rebuild_books(d)
    tmax, tmin = tob.rx.max() / 1000, tob.rx.min() / 1000
    mk = mk[(mk.window_start + 360 < tmax) & (mk.window_start - 120 > tmin)]
    wins = {}
    for _, m in mk.iterrows():
        e = fetch_event(m.slug)
        r = parse_event(e) if e else None
        if not r or r["winner"] is None:
            continue
        g = build_grid(m.asset, m.window_start, tob, bn, cl)
        if g is None:
            continue
        g = add_fair_value(g, bn, m.asset, m.window_start)
        g["p_bn"] = g.p_up
        g["p_cl"] = chainlink_fair(g, cl, bn, m.asset, m.window_start, r["price_to_beat"], g.sigma.values)
        trs = ltr[(ltr.asset == m.asset) & (ltr.window_start == m.window_start)].sort_values("ts")
        wins[m.slug] = (m.asset, g, trs, 1.0 if r["winner"] == "Up" else 0.0)
    print(f"windows simulated: {len(wins)}  ({pd.Series([v[0] for v in wins.values()]).value_counts().to_dict()})")

    grid_params = list(itertools.product(["p_bn", "p_cl"], [0.0, 0.01, 0.02, 0.04], [100, 500], [240], [False, True],
                                         [False, True]))
    rows = []
    for fv, h, lat, stop, opt, join in grid_params:
        prm = MakerParams(half_spread=h, latency_ms=lat, stop_s=stop, optimistic=opt, join_only=join, size=10, max_pos=50)
        for slug, (asset, g, trs, won) in wins.items():
            res, _ = simulate_window(g.assign(p_up=g[fv]), trs, won, prm)
            rows.append({"asset": asset, "slug": slug, "fv": fv, "h": h, "lat": lat, "stop": stop, "opt": opt,
                         "join": join, **res})
    r = pd.DataFrame(rows)
    summ = r.groupby(["fv", "join", "opt", "h", "lat", "stop"]).agg(
        windows=("pnl", "size"), fills=("fills", "sum"), notional=("notional", "sum"), pnl=("pnl", "sum"),
        rebate=("rebate", "sum"), total=("total", "sum"),
        win_windows=("total", lambda x: (x > 0).mean()), worst=("total", "min"))
    summ["roi"] = summ.total / summ.notional.replace(0, np.nan)
    summ["per_window"] = summ.total / summ.windows
    print(summ.round(3).to_string())
    print("\nper asset, conservative fills, 100ms latency:")
    sub = r[(~r.opt) & (r.lat == 100)]
    pa = sub.groupby(["fv", "join", "h", "asset"]).agg(windows=("pnl", "size"), notional=("notional", "sum"),
                                                     total=("total", "sum"))
    pa["roi"] = pa.total / pa.notional.replace(0, np.nan)
    print(pa.round(3).to_string())
    r.to_parquet(os.path.join(d, "maker_sim.parquet"))


if __name__ == "__main__":
    main(sys.argv[1])
