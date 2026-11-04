"""Paper market-making simulator on recorded live data.

Strategy (per 5m window):
  * fair value p = P(Up) from the TWAP model on Binance mid (open TWAP from Binance as well)
  * rest a BID on Up at floor(p - h) and a BID on Down at floor((1-p) - h)   (two bids = no inventory needed)
  * quotes are post-only: never at/through the opposite best ask
  * a quote only becomes live `latency_ms` after the data it was computed from (cancel/replace delay)
  * stop quoting `stop_s` seconds into the window (late window = most toxic flow)
  * inventory cap: stop bidding a token once |net Up-Down shares| exceeds max_pos

Fill model (recorded taker trades, side = taker side):
  our Up bid b is hit by  - taker SELL Up at price p_tr  with p_tr <  b   (we were the better bid)
                          - taker BUY  Down at q        with 1-q   <  b   (mint match against our bid)
  and the symmetric rules for our Down bid.  Equality (same price level) = queue: filled only in
  `optimistic` mode.  Filled size = min(remaining quote size, trade size).
Rebates: 20% * fee-equivalent of our fills (crypto feeRate 0.07), i.e. our pro-rata share of the pool.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .fees import fee_per_share

TICK = 0.01


@dataclass
class MakerParams:
    half_spread: float = 0.02
    size: float = 10.0
    latency_ms: int = 200
    stop_s: float = 240.0
    start_s: float = 2.0
    max_pos: float = 50.0
    optimistic: bool = False
    join_only: bool = False      # never improve the market: bid <= current best bid of that token
    rebate_share: float = 0.20


def _floor_tick(x):
    return np.floor(np.asarray(x) / TICK + 1e-9) * TICK


def simulate_window(grid: pd.DataFrame, trades: pd.DataFrame, won_up: float, prm: MakerParams):
    """grid: 100ms rows with t (ms), rel (s), p_up, ask_U, ask_D, bid_U, bid_D.
    trades: rx/ts (ms), outcome ('U'/'D'), price, size, side (taker side)."""
    g = grid.reset_index(drop=True)
    t_grid = g.t.values
    p = g.p_up.values
    bU = _floor_tick(p - prm.half_spread)
    bD = _floor_tick((1 - p) - prm.half_spread)
    # post-only: stay strictly below the opposite best ask
    bU = np.minimum(bU, np.round(g.ask_U.values - TICK, 2))
    bD = np.minimum(bD, np.round(g.ask_D.values - TICK, 2))
    if prm.join_only:
        bU = np.minimum(bU, g.bid_U.values)
        bD = np.minimum(bD, g.bid_D.values)
    active = (g.rel.values >= prm.start_s) & (g.rel.values < prm.stop_s) & np.isfinite(p)
    bU = np.where(active & (bU > 0), bU, np.nan)
    bD = np.where(active & (bD > 0), bD, np.nan)

    pos_u = pos_d = 0.0
    cash = 0.0
    fee_eq = 0.0
    fills = []
    rem = {}  # quote-version -> remaining size (refreshes whenever the quoted price changes)
    lat = prm.latency_ms
    for tr in trades.itertuples(index=False):
        ts = tr.ts
        i = np.searchsorted(t_grid, ts - lat, side="right") - 1
        if i < 0 or i >= len(t_grid):
            continue
        for tok, b in (("U", bU[i]), ("D", bD[i])):
            if not np.isfinite(b):
                continue
            # taker flow that would hit a bid on `tok`
            if tr.outcome == tok and tr.side == "SELL":
                eff = tr.price             # taker sold tok at this price
            elif tr.outcome != tok and tr.side == "BUY":
                eff = round(1 - tr.price, 4)  # taker bought the complement -> mint against our bid
            else:
                continue
            hit = eff < b - 1e-9 or (prm.optimistic and abs(eff - b) < 1e-9)
            if not hit:
                continue
            net = pos_u - pos_d
            if (tok == "U" and net >= prm.max_pos) or (tok == "D" and -net >= prm.max_pos):
                continue
            left = rem.get((tok, round(b, 2)), prm.size)
            q = min(left, tr.size)
            if q <= 0:
                continue
            rem[(tok, round(b, 2))] = left - q
            cash -= q * b
            fee_eq += fee_per_share(b) * q
            if tok == "U":
                pos_u += q
            else:
                pos_d += q
            fills.append((ts, tok, b, q, g.rel.values[i], p[i]))
        # quote refresh: when price level changes, a fresh `size` is available again
        for tok, b in (("U", bU[i]), ("D", bD[i])):
            if np.isfinite(b):
                for k in [k for k in rem if k[0] == tok and k[1] != round(b, 2)]:
                    rem.pop(k)
    payoff = pos_u * won_up + pos_d * (1 - won_up)
    pnl = cash + payoff
    rebate = prm.rebate_share * fee_eq
    return {"pnl": pnl, "rebate": rebate, "total": pnl + rebate, "pos_u": pos_u, "pos_d": pos_d,
            "fills": len(fills), "notional": -cash,
            "matched_sets": min(pos_u, pos_d)}, fills
