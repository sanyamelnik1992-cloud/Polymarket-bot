"""100ms grids built from recorder output + TWAP-model fair value."""
import numpy as np
import pandas as pd

from .config import BINANCE_SYMBOL, CHAINLINK_SYMBOL
from .model import prob_up

GRID_MS = 100


def build_grid(asset, ws, tob, bn, cl, pre=15, window=300):
    t0, t1 = (ws - pre) * 1000, (ws + window) * 1000
    grid = pd.DataFrame({"t": np.arange(t0, t1, GRID_MS)})
    for oc in ("U", "D"):
        k = f"{asset}-{ws}-{oc}"
        s = tob[tob.k == k][["rx", "bid", "ask", "bid_sz", "ask_sz"]].sort_values("rx")
        if s.empty:
            return None
        s = s.rename(columns={c: f"{c}_{oc}" for c in ["bid", "ask", "bid_sz", "ask_sz"]})
        grid = pd.merge_asof(grid, s, left_on="t", right_on="rx", direction="backward").drop(columns="rx")
    b = bn[bn.sym == BINANCE_SYMBOL[asset]][["rx", "mid"]].sort_values("rx").rename(columns={"mid": "bn"})
    grid = pd.merge_asof(grid, b, left_on="t", right_on="rx", direction="backward").drop(columns="rx")
    c = cl[cl.sym == CHAINLINK_SYMBOL[asset]][["rx", "v"]].sort_values("rx").rename(columns={"v": "cl"})
    grid = pd.merge_asof(grid, c, left_on="t", right_on="rx", direction="backward").drop(columns="rx")
    grid["rel"] = grid.t / 1000 - ws
    return grid


def add_fair_value(grid, bn, asset, ws, window=300, halflife=900, df_t=4):
    """P(Up) on every grid row from Binance mid (1s sampled), open TWAP from Binance [ws-59, ws]."""
    b = bn[bn.sym == BINANCE_SYMBOL[asset]]
    b1 = b.groupby(b.rx // 1000).mid.last()
    b1 = b1.reindex(np.arange(ws - 1800, ws + window + 1)).ffill()
    lr = np.log(b1).diff()
    sig = np.sqrt((lr ** 2).ewm(halflife=halflife, min_periods=30).mean())
    open_tw = b1.loc[ws - 59:ws].mean()
    cs = b1.cumsum()
    sec = np.floor(grid.t.values / 1000).astype(int)
    sigma = sig.reindex(sec - 1).values
    tw0 = ws + window - 60
    npart = np.clip(sec - tw0, 0, 60)
    psum = np.where(npart > 0, cs.reindex(sec).values - cs.reindex(np.full(len(sec), tw0)).values, 0.0)
    g = grid.copy()
    g["sigma"] = sigma
    g["p_up"] = prob_up(g.rel.values, g.bn.values, open_tw, sigma, psum, npart, df=df_t, window=window)
    g["mid_U"] = (g.bid_U + g.ask_U) / 2
    return g


def chainlink_fair(g, cl, bn, asset, ws, o_cl, sigma, window=300):
    """P(Up) from the Chainlink spot (as received) nowcast with the Binance return since that print."""
    c = cl[cl.sym == CHAINLINK_SYMBOL[asset]].sort_values("rx")
    b = bn[bn.sym == BINANCE_SYMBOL[asset]].sort_values("rx")
    gg = pd.merge_asof(g[["t"]], c[["rx", "v", "ts"]].rename(columns={"v": "clv"}), left_on="t", right_on="rx",
                       direction="backward").drop(columns="rx")
    # Binance mid at the Chainlink print time and now -> nowcast
    bb = b[["rx", "mid"]].rename(columns={"mid": "bn_then"})
    gg = pd.merge_asof(gg.sort_values("ts"), bb, left_on="ts", right_on="rx", direction="backward").drop(columns="rx")
    gg = gg.sort_values("t").reset_index(drop=True)
    now_spot = gg.clv * (g.bn.values / gg.bn_then)
    # partial closing-window sum from Chainlink seconds already printed
    s = c.drop_duplicates("ts")
    s1 = pd.Series(s.v.values, index=(s.ts // 1000).values)
    s1 = s1[~s1.index.duplicated()].sort_index()
    s1 = s1.reindex(np.arange(ws - 120, ws + window + 1)).ffill()
    cs = s1.cumsum()
    known_sec = np.floor(gg.ts.fillna(0).values / 1000).astype(int)
    tw0 = ws + window - 60
    npart = np.clip(known_sec - tw0, 0, 60)
    psum = np.where(npart > 0, cs.reindex(np.minimum(known_sec, ws + window)).values - cs.reindex(np.full(len(g), tw0)).values, 0.0)
    rel_known = known_sec - ws
    # treat seconds between the last Chainlink print and now as unknown: use rel_known for the TWAP algebra
    return prob_up(np.maximum(rel_known, g.rel.values - 2), now_spot.values, o_cl, sigma, psum, npart, df=4, window=window)
