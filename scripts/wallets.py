"""Who wins on 5m markets?  Taker-side wallet PnL over the history window.

usage: python scripts/wallets.py HIST_DIR
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.features import enrich_trades, load  # noqa: E402

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 30)

if __name__ == "__main__":
    mk, tr, kl = load(sys.argv[1])
    t = enrich_trades(mk, tr, kl, lag_s=4)
    t["pnl"] = t.taker_pnl_ps * t["size"]
    t["fee"] = t.fee_ps * t["size"]
    t["edge_fill"] = t.model_edge_ps > 0.05
    g = t.groupby("proxy_wallet")
    w = pd.DataFrame({
        "name": g.name.first().str[:18],
        "fills": g.size(),
        "markets": g.condition_id.nunique(),
        "notional": g.notional.sum(),
        "pnl": g.pnl.sum(),
        "fees": g.fee.sum(),
        "roi": g.pnl.sum() / g.notional.sum(),
        "med_rel": g.rel.median(),
        "med_price": g.price.median(),
        "med_size": g["size"].median(),
        "share_edge_fills": g.edge_fill.mean(),
        "assets": g.asset.agg(lambda x: ",".join(sorted(x.unique()))),
    })
    print(f"taker wallets: {len(w):,}; profitable: {(w.pnl > 0).mean():.1%}; "
          f"with >1000 fills: {(w.fills > 1000).sum()}")
    print("\nTop 25 taker wallets by PnL (72h):")
    print(w.sort_values("pnl", ascending=False).head(25).round(3).to_string())
    print("\nBottom 10:")
    print(w.sort_values("pnl").head(10).round(3).to_string())
    print("\nTop 25 by notional:")
    print(w.sort_values("notional", ascending=False).head(25).round(3).to_string())
    # concentration of the 'edge' fills
    e = t[t.edge_fill]
    ge = e.groupby("proxy_wallet").pnl.sum().sort_values(ascending=False)
    print(f"\nEdge fills (model edge>5c): {len(e):,}, pnl ${e.pnl.sum():,.0f}; top-10 wallets take "
          f"{ge.head(10).sum() / ge[ge > 0].sum():.1%} of positive pnl")
    # pnl by activity bucket
    w["act"] = pd.cut(w.fills, [0, 5, 20, 100, 1000, 10000, 1e9])
    print(w.groupby("act", observed=True).agg(wallets=("pnl", "size"), pnl=("pnl", "sum"),
                                               share_prof=("pnl", lambda x: (x > 0).mean()),
                                               median_pnl=("pnl", "median"), fees=("fees", "sum")).round(2))
    w.drop(columns="act").to_parquet(os.path.join(sys.argv[1], "wallets.parquet"))
