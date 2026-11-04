"""Model-free monotonicity arbitrage between a 15m market and the 5m market ending at the same second.

If O15 <= O5: {C >= O5} is a subset of {C >= O15}  ->  P15_up >= P5_up must hold.
Violation v = P5_up - P15_up (> 0).  Trade: buy 15m Up + buy 5m Down: cost = P15 + (1-P5) = 1 - v,
payoff >= 1 in every state (=2 when O15 <= C < O5).  Mirror case for O15 > O5.
Uses taker fill prices (both legs at observed fills within `max_gap` s), fees on both legs.

usage: python scripts/nested_arb.py HIST_5M HIST_15M
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.fees import fee_per_share  # noqa: E402

pd.set_option("display.width", 220)


def fills(hist, window):
    mk = pd.read_parquet(f"{hist}/markets.parquet")
    tr = pd.read_parquet(f"{hist}/trades.parquet")
    mk = mk[mk.asset.isin(["btc", "eth"])]
    t = tr.merge(mk[["condition_id", "asset", "window_start", "token_up", "price_to_beat", "winner"]], on="condition_id")
    t["tm"] = t.timestamp - 2.2
    t["end"] = t.window_start + window
    t["is_up"] = t.token_id == t.token_up
    # price at which one could BUY Up / BUY Down right now, from taker BUY fills only (= resting asks)
    t["ask_up"] = np.where(t.is_up & (t.side == "BUY"), t.price, np.nan)
    t["ask_dn"] = np.where(~t.is_up & (t.side == "BUY"), t.price, np.nan)
    return t


if __name__ == "__main__":
    t5 = fills(sys.argv[1], 300)
    t15 = fills(sys.argv[2], 900)
    t15 = t15[(t15.timestamp - t15.window_start) >= 600]
    out = []
    for (a, end), g15 in t15.groupby(["asset", "end"]):
        g5 = t5[(t5.asset == a) & (t5.end == end)]
        if g5.empty:
            continue
        o15, o5 = g15.price_to_beat.iloc[0], g5.price_to_beat.iloc[0]
        win_up = g5.winner.iloc[0] == "Up"
        win15_up = g15.winner.iloc[0] == "Up"
        # second-by-second latest asks
        secs = np.arange(end - 300, end)
        def last(series_t, series_v):
            s = pd.Series(series_v.values, index=series_t.values).dropna().sort_index()
            s = s[~s.index.duplicated(keep="last")]
            return s.reindex(secs, method="ffill", tolerance=2)
        if o15 <= o5:
            leg15 = last(g15.tm.round().astype(int), g15.ask_up)   # buy 15m Up
            leg5 = last(g5.tm.round().astype(int), g5.ask_dn)      # buy 5m Down
            payoff = int(win15_up) + int(not win_up)
        else:
            leg15 = last(g15.tm.round().astype(int), g15.ask_dn)   # buy 15m Down
            leg5 = last(g5.tm.round().astype(int), g5.ask_up)      # buy 5m Up
            payoff = int(not win15_up) + int(win_up)
        cost = leg15 + leg5 + fee_per_share(leg15) + fee_per_share(leg5)
        edge = 1 - cost
        ok = edge.dropna()
        if ok.empty:
            continue
        best = ok.idxmax()
        out.append({"asset": a, "end": end, "gap_bp": (o5 / o15 - 1) * 1e4, "secs_obs": len(ok),
                    "secs_edge>1c": (ok > 0.01).sum(), "secs_edge>3c": (ok > 0.03).sum(),
                    "max_edge": ok.max(), "rel_best": best - (end - 300), "cost_best": cost[best], "payoff": payoff})
    r = pd.DataFrame(out)
    print(f"pairs analysed: {len(r)}")
    print(r.groupby("asset")[["secs_obs", "secs_edge>1c", "secs_edge>3c", "max_edge"]].describe().T.round(3).to_string())
    hit = r[r["secs_edge>1c"] > 0]
    print(f"\npairs with a >1c fee-adjusted locked edge at some second: {len(hit)} / {len(r)}")
    print(f"realised if we bought 1 share of each leg at the best second: mean pnl/set = {(hit.payoff - hit.cost_best).mean():+.4f}, "
          f"min = {(hit.payoff - hit.cost_best).min():+.4f}")
    print(hit.sort_values("max_edge", ascending=False).head(15).round(4).to_string())
    r.to_parquet(os.path.join(sys.argv[2], "nested_arb.parquet"))
