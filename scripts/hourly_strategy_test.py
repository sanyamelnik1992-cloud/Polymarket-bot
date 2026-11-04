"""Robustness of the hourly 'late-window big-disagreement taker' rule: by day, by threshold, capacity.

usage: python scripts/hourly_strategy_test.py HOURLY_DIR
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from analyze_hourly import enrich  # noqa: E402

pd.set_option("display.width", 200)


def clustered(s):
    pm = s.groupby("condition_id").apply(lambda z: (z.pnl_ps * z["size"].clip(upper=100)).sum())
    return pm.mean() / pm.std() * np.sqrt(len(pm)) if len(pm) > 2 and pm.std() > 0 else np.nan


if __name__ == "__main__":
    d = sys.argv[1]
    mk = pd.read_parquet(f"{d}/markets.parquet")
    tr = pd.read_parquet(f"{d}/trades.parquet")
    kl = {a: pd.read_parquet(f"{d}/klines_{a}.parquet") for a in mk.asset.unique()}
    days = (mk.window_start.max() - mk.window_start.min() + 3600) / 86400
    for lag in (4, 8):
        t = enrich(mk, tr, kl, lag_s=lag)
        t["ttc"] = 3600 - t.rel
        t = t[(t.rel >= 0) & (t.ttc > 5)]
        print(f"\n######## lag {lag}s")
        rows = []
        for ttc_max in (300, 900, 1800):
            for thr in (0.05, 0.08, 0.10, 0.15, 0.20):
                s = t[(t.edge > thr) & (t.ttc <= ttc_max)]
                if s.empty:
                    continue
                rows.append({"ttc<=": ttc_max, "edge>": thr, "fills": len(s), "markets": s.condition_id.nunique(),
                             "roi": (s.pnl_ps * s["size"]).sum() / s.notional.sum(),
                             "pnl_capped100_per_day": (s.pnl_ps * s["size"].clip(upper=100)).sum() / days,
                             "notional_capped_per_day": (s.price * s["size"].clip(upper=100)).sum() / days,
                             "t": clustered(s), "win_rate": (s.won == np.where(s.side == "BUY", 1, 0)).mean()})
        print(pd.DataFrame(rows).round(3).to_string())
        s = t[(t.edge > 0.10) & (t.ttc <= 900)].copy()
        s["day"] = pd.to_datetime(s.window_start, unit="s").dt.date
        print("\nby day (edge>10c, last 15 min):")
        print(s.groupby(["day", "asset"]).apply(lambda z: pd.Series({
            "markets": z.condition_id.nunique(), "pnl_capped": (z.pnl_ps * z["size"].clip(upper=100)).sum(),
            "roi": (z.pnl_ps * z["size"]).sum() / z.notional.sum()})).round(3).unstack("asset").to_string())
        print("price distribution of these fills:", s.price.describe(percentiles=[.1, .5, .9]).round(3).to_dict())
