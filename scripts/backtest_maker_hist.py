"""Historical (72h) backtest of a two-bid fair-value maker against the observed taker flow.

No historical book exists, so the fill rule is the conservative "trade-through" rule from pmbot.maker_sim:
our bid b on a token is filled only by taker flow that executed at a price strictly worse than b for the
taker's counterparty (taker SELL token at p < b, or taker BUY complement at q > 1-b).  Each market/side
can absorb at most `cap` shares per quote level change, approximated by a per-market/side cap.

Fair value: TWAP model on Binance 1s closes observed at (match_time - latency), match_time = data-api ts - 2.2s.
Also tested: fair value = last trade-implied P(Up) (a "follow the market" quoter) for comparison.

usage: python scripts/backtest_maker_hist.py HIST_DIR
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.features import enrich_trades, load  # noqa: E402
from pmbot.fees import fee_per_share  # noqa: E402

pd.set_option("display.width", 220)
TICK = 0.01


def run(t, fv_col, h, stop_s, size=10, cap=100):
    x = t[(t.rel_m >= 2) & (t.rel_m < stop_s) & t[fv_col].notna()].copy()
    fv = x[fv_col].values
    bU = np.floor((fv - h) / TICK + 1e-9) * TICK
    bD = np.floor((1 - fv - h) / TICK + 1e-9) * TICK
    up = x.is_up_tok.values
    sell = (x.side == "SELL").values
    p = x.price.values
    # effective price at which the taker flow would have hit a bid on Up / Down
    eff_u = np.where(up & sell, p, np.where(~up & ~sell, 1 - p, np.nan))
    eff_d = np.where(~up & sell, p, np.where(up & ~sell, 1 - p, np.nan))
    hit_u = eff_u < bU - 1e-9
    hit_d = eff_d < bD - 1e-9
    q = np.minimum(x["size"].values, size)
    won_up = (x.winner == "Up").values.astype(float)
    rows = pd.DataFrame({
        "cid": np.concatenate([x.condition_id.values[hit_u], x.condition_id.values[hit_d]]),
        "asset": np.concatenate([x.asset.values[hit_u], x.asset.values[hit_d]]),
        "tok": ["U"] * hit_u.sum() + ["D"] * hit_d.sum(),
        "b": np.concatenate([bU[hit_u], bD[hit_d]]),
        "q": np.concatenate([q[hit_u], q[hit_d]]),
        "won": np.concatenate([won_up[hit_u], 1 - won_up[hit_d]]),
        "rel": np.concatenate([x.rel_m.values[hit_u], x.rel_m.values[hit_d]]),
    })
    # per-market/side capacity cap
    rows["cum"] = rows.groupby(["cid", "tok"]).q.cumsum()
    rows = rows[rows.cum <= cap]
    rows["pnl"] = (rows.won - rows.b) * rows.q
    rows["rebate"] = 0.2 * fee_per_share(rows.b) * rows.q
    rows["notional"] = rows.b * rows.q
    return rows


if __name__ == "__main__":
    mk, tr, kl = load(sys.argv[1])
    out = []
    for lat_s in (1, 3):
        # match time = ts - 2.2 ; information time = match - latency  -> lag_s rounded up
        lag = int(np.ceil(2.2 + lat_s))
        t = enrich_trades(mk, tr, kl, lag_s=lag)
        t["rel_m"] = t.rel - 2.2
        # 'market' fair value: previous trade-implied P(Up) in the same market (strictly earlier fill)
        t = t.sort_values(["condition_id", "timestamp"])
        t["mkt_fv"] = t.groupby("condition_id").p_up_trade.shift(1)
        for fv_col in ("model_p_up", "mkt_fv"):
            for h in (0.01, 0.02, 0.03, 0.05):
                for stop in (240, 290):
                    r = run(t, fv_col, h, stop)
                    pm = r.groupby("cid").apply(lambda z: (z.pnl + z.rebate).sum())
                    out.append({"lat_s": lat_s, "fv": fv_col, "h": h, "stop": stop, "fills": len(r),
                                "notional": r.notional.sum(), "pnl": r.pnl.sum(), "rebate": r.rebate.sum(),
                                "total": (r.pnl + r.rebate).sum(),
                                "roi": (r.pnl + r.rebate).sum() / max(r.notional.sum(), 1),
                                "t_stat": pm.mean() / pm.std() * np.sqrt(len(pm)) if len(pm) > 2 else np.nan,
                                **{f"roi_{a}": ((r[r.asset == a].pnl + r[r.asset == a].rebate).sum() /
                                                max(r[r.asset == a].notional.sum(), 1)) for a in ("btc", "eth", "sol", "xrp")}})
    df = pd.DataFrame(out)
    print(df.round(4).to_string())
