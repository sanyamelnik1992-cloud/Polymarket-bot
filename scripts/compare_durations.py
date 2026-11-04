"""Side-by-side economics of 5m vs 15m Up/Down markets.

usage: python scripts/compare_durations.py HIST_5M HIST_15M
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.features import binance_resolution, enrich_trades, load  # noqa: E402

pd.set_option("display.width", 220)


def summarize(hist, label, assets=("btc", "eth")):
    mk, tr, kl = load(hist, assets)
    mk = mk[mk.asset.isin(assets)]
    if "window" not in mk:
        mk = mk.assign(window=300)
    W = int(mk.window.iloc[0])
    hours = (mk.window_start.max() - mk.window_start.min() + W) / 3600
    r = binance_resolution(mk, kl)
    t = enrich_trades(mk, tr, kl, lag_s=4)
    t["pnl"] = t.taker_pnl_ps * t["size"]
    t["fee"] = t.fee_ps * t["size"]
    out = []
    for a in assets:
        x = t[t.asset == a]
        inw = x[(x.rel >= 5) & (x.rel < W - 3)]
        res = {}
        for lag_thr in (0.03, 0.1):
            s = inw[inw.model_edge_ps > lag_thr]
            pm = s.groupby("condition_id").apply(lambda z: (z.taker_pnl_ps * z["size"].clip(upper=100)).sum())
            res[f"edge>{lag_thr}_roi"] = s.pnl.sum() / max(s.notional.sum(), 1)
            res[f"edge>{lag_thr}_t"] = pm.mean() / pm.std() * np.sqrt(len(pm)) if len(pm) > 2 else np.nan
        out.append({
            "dur": label, "asset": a, "markets": (mk.asset == a).sum(),
            "taker_notional_per_day": x.notional.sum() / hours * 24,
            "taker_pnl_per_day": x.pnl.sum() / hours * 24,
            "fees_per_day": x.fee.sum() / hours * 24,
            "fee_rate": x.fee.sum() / x.notional.sum(),
            "taker_roi": x.pnl.sum() / x.notional.sum(),
            "maker_prerebate_per_day": -(x.pnl + x.fee).sum() / hours * 24,
            "rebate_pool_per_day": 0.2 * x.fee.sum() / hours * 24,
            "bn_agree": (r[r.asset == a].winner == r[r.asset == a].bn_winner).mean(),
            **res,
        })
    return pd.DataFrame(out)


if __name__ == "__main__":
    df = pd.concat([summarize(sys.argv[1], "5m"), summarize(sys.argv[2], "15m")])
    print(df.set_index(["dur", "asset"]).T.round(4).to_string())
