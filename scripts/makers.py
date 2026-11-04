"""Maker-side wallet economics (needs trades_all_<asset>.parquet from collect_maker_side.py).

usage: python scripts/makers.py HIST_DIR ASSET
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.fees import fee_per_share  # noqa: E402

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 30)

if __name__ == "__main__":
    hist, asset = sys.argv[1], sys.argv[2]
    mk = pd.read_parquet(f"{hist}/markets.parquet")
    al = pd.read_parquet(f"{hist}/trades_all_{asset}.parquet")
    tk = pd.read_parquet(f"{hist}/trades.parquet")
    tk = tk[tk.condition_id.isin(al.condition_id.unique())]
    key = ["transaction_hash", "proxy_wallet", "token_id", "side", "size", "price"]
    al["k"] = al[key].astype(str).agg("|".join, axis=1)
    tk["k"] = tk[key].astype(str).agg("|".join, axis=1)
    mrows = al[~al.k.isin(set(tk.k))].copy()
    print(f"all rows {len(al):,}  taker rows {len(tk):,}  maker rows {len(mrows):,}")
    m = mk.set_index("condition_id")
    mrows = mrows.join(m[["window_start", "winner", "token_up"]], on="condition_id")
    mrows["won"] = np.where(mrows.token_id == mrows.token_up, mrows.winner == "Up", mrows.winner == "Down").astype(float)
    sign = np.where(mrows.side == "BUY", 1.0, -1.0)
    mrows["pnl"] = sign * (mrows.won - mrows.price) * mrows["size"]
    mrows["notional"] = mrows.price * mrows["size"]
    mrows["fee_eq"] = fee_per_share(mrows.price) * mrows["size"]
    mrows["rel"] = mrows.timestamp - mrows.window_start
    # rebate pool per market = 20% of taker fees; split by fee-equivalent share
    tk["fee"] = fee_per_share(tk.price) * tk["size"]
    pool = 0.2 * tk.groupby("condition_id").fee.sum()
    share = mrows.fee_eq / mrows.groupby("condition_id").fee_eq.transform("sum")
    mrows["rebate"] = share * mrows.condition_id.map(pool)
    g = mrows.groupby("proxy_wallet")
    w = pd.DataFrame({
        "name": g.name.first().str[:18], "fills": g.size(), "markets": g.condition_id.nunique(),
        "notional": g.notional.sum(), "pnl": g.pnl.sum(), "rebate": g.rebate.sum(),
    })
    w["total"] = w.pnl + w.rebate
    w["roi"] = w.total / w.notional
    w["share_of_maker_vol"] = w.notional / w.notional.sum()
    # per-market consistency for the big makers
    pm = mrows.groupby(["proxy_wallet", "condition_id"]).apply(lambda x: (x.pnl + x.rebate).sum())
    w["win_markets"] = pm.groupby(level=0).apply(lambda x: (x > 0).mean())
    w["t_stat"] = pm.groupby(level=0).apply(lambda x: x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() > 0 else np.nan)
    print(f"maker wallets {len(w):,}; maker notional ${w.notional.sum():,.0f}; maker pnl ${w.pnl.sum():,.0f}; "
          f"rebates ${w.rebate.sum():,.0f}")
    top = w.sort_values("notional", ascending=False)
    print(f"top-5 makers' share of maker volume: {top.share_of_maker_vol.head(5).sum():.1%}, top-20: {top.share_of_maker_vol.head(20).sum():.1%}")
    print("\nTop 20 makers by volume (24h):")
    print(top.head(20).round(4).to_string())
    print("\nTop 10 makers by total pnl:")
    print(w.sort_values("total", ascending=False).head(10).round(4).to_string())
    print("\nMaker pnl (pre-rebate) by time in window, all makers:")
    mrows["tb"] = pd.cut(mrows.rel, [-1e9, 0, 60, 120, 180, 240, 270, 290, 300, 1e9])
    print(mrows.groupby("tb", observed=True).agg(notional=("notional", "sum"), pnl=("pnl", "sum"), rebate=("rebate", "sum")).round(0))
    w.to_parquet(f"{hist}/makers_{asset}.parquet")
