"""Join trades with outcomes and Binance-derived fair value."""
import numpy as np
import pandas as pd

from .fees import fee_per_share
from .model import prob_up

WINDOW = 300
L = 60


def load(hist_dir, assets=("btc", "eth", "sol", "xrp")):
    mk = pd.read_parquet(f"{hist_dir}/markets.parquet")
    tr = pd.read_parquet(f"{hist_dir}/trades.parquet")
    kl = {}
    for a in assets:
        try:
            kl[a] = pd.read_parquet(f"{hist_dir}/klines_{a}.parquet")
        except FileNotFoundError:
            pass
    return mk, tr, kl


def binance_resolution(mk, kl):
    """Reconstruct open/close 60s TWAPs from Binance 1s closes and compare with the oracle outcome."""
    rows = []
    for a, k in kl.items():
        c = k["c"]
        # rolling 60s mean ending at second s (inclusive): mean(c[s-59..s])
        roll = c.rolling(L, min_periods=L).mean()
        sub = mk[mk.asset == a]
        T = sub.window_start.values
        W = sub["window"].values if "window" in sub else WINDOW
        o = roll.reindex(T).values
        cl = roll.reindex(T + W).values
        rows.append(pd.DataFrame({"slug": sub.slug.values, "bn_open": o, "bn_close": cl}))
    r = pd.concat(rows)
    out = mk.merge(r, on="slug", how="left")
    out["bn_winner"] = np.where(out.bn_close >= out.bn_open, "Up", "Down")
    out["oracle_move"] = (out.final_price / out.price_to_beat - 1) * 1e4  # bp
    out["bn_move"] = (out.bn_close / out.bn_open - 1) * 1e4
    return out


def enrich_trades(mk, tr, kl, lag_s=4, vol_halflife=900, df_t=4):
    """Per-trade features: time in window, token won?, taker PnL, model probability of the traded token.

    lag_s: seconds subtracted from the trade timestamp before reading spot (guards against look-ahead:
           data-api timestamps are block times, i.e. after the match).
    """
    m = mk.set_index("condition_id")
    if "window" not in m:
        m = m.assign(window=WINDOW)
    t = tr.merge(m[["asset", "window_start", "window", "winner", "token_up", "price_to_beat"]], left_on="condition_id",
                 right_index=True, how="inner").copy()
    t["rel"] = t["timestamp"] - t["window_start"]
    t["is_up_tok"] = t["token_id"] == t["token_up"]
    t["won"] = np.where(t["is_up_tok"], t["winner"] == "Up", t["winner"] == "Down").astype(float)
    t["fee_ps"] = fee_per_share(t["price"])
    sign = np.where(t["side"] == "BUY", 1.0, -1.0)
    # taker pnl per share (BUY: won - p - fee, SELL: p - won - fee)
    t["taker_pnl_ps"] = sign * (t["won"] - t["price"]) - t["fee_ps"]
    t["maker_pnl_ps"] = -sign * (t["won"] - t["price"])  # makers pay no fee (rebates excluded)
    t["notional"] = t["size"] * t["price"]
    # equivalent "probability of Up" implied by the trade
    t["p_up_trade"] = np.where(t["is_up_tok"], t["price"], 1 - t["price"])
    # taker direction in Up terms: +1 = taker wants Up exposure
    t["dir_up"] = np.where(t["is_up_tok"], sign, -sign)

    parts = []
    for a, k in kl.items():
        sub = t[t.asset == a].copy()
        if sub.empty:
            continue
        c = k["c"]
        lr = np.log(c).diff()
        sig = np.sqrt((lr ** 2).ewm(halflife=vol_halflife, min_periods=60).mean())
        roll = c.rolling(L, min_periods=L).mean()
        cs = c.cumsum()
        ts_obs = sub["timestamp"].values - lag_s
        spot = c.reindex(ts_obs).values
        sigma = sig.reindex(ts_obs).values
        T = sub["window_start"].values
        W = sub["window"].values
        open_tw = roll.reindex(T).values
        rel_obs = ts_obs - T
        # partial sum of closes in the closing TWAP window [T+W-59, ts_obs]
        tw0 = T + W - L
        n_part = np.clip(ts_obs - tw0, 0, L)
        hi = cs.reindex(ts_obs).values
        lo = cs.reindex(tw0).values
        psum = np.where(n_part > 0, hi - lo, 0.0)
        pu = prob_up(rel_obs, spot, open_tw, sigma, psum, n_part, df=df_t, window=W)
        sub["spot"] = spot
        sub["sigma"] = sigma
        sub["bn_open"] = open_tw
        sub["model_p_up"] = pu
        parts.append(sub)
    t = pd.concat(parts)
    t["model_p_tok"] = np.where(t["is_up_tok"], t["model_p_up"], 1 - t["model_p_up"])
    # model edge for the taker (per share, after fee)
    t["model_edge_ps"] = np.where(t["side"] == "BUY", t["model_p_tok"] - t["price"], t["price"] - t["model_p_tok"]) - t["fee_ps"]
    return t
