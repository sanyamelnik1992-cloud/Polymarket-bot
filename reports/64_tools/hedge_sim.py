"""Insurance on a reversal ("страховка"): after a live-rule entry on side S, if the model turns to the other side O with p_O >= H
and O can be bought at <= cap, buy the same number of shares of O (FAK, the book 300 ms later, retry 1 s). Holding both sides
pays exactly `shares` at the end: the window ends at shares * (1 - entry - hedge price) - fees whatever happens.

Grids: reports/64_tools/grid_pmin.py with GRID_P_MIN=0.5 (every 100 ms of the closing minute, the favoured side's book), env
REPLAY_GRID. usage: python reports/64_tools/hedge_sim.py [periods,comma,separated]"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "52_tools"))
from sim import L3, base_ok, limits, load, p_sigma, run, tier_limit  # noqa: E402
from pmbot.fees import fee_per_share  # noqa: E402

PER = sys.argv[1].split(",") if len(sys.argv) > 1 else ["r1107", "r1108", "r1109", "d1109", "d1110", "d1110b", "d1110c",
                                                        "d1111", "d1111b"]


def load_live(periods, tiers=L3[:3]):
    g = load(periods)
    order = {p: i for i, p in enumerate(periods)}
    first = g.assign(o=g.period.map(order)).groupby(["asset", "T"]).o.transform("min")
    g = g[g.period.map(order) == first].reset_index(drop=True)          # periods overlap by a few windows
    s = np.maximum(g.sigma_bp.values, 0.5)
    raw = g.p.values >= 0.99                                             # floor_free_p 0.99: the unfloored model decides
    g["p_raw"] = g.p
    g["p"] = np.where(raw, g.p, p_sigma(g, s))
    g["p_cl"] = np.where(raw, g.p_cl, p_sigma(g, s, "cl"))
    su = np.where(raw, g.sigma_bp, s)
    ok = base_ok(g) & (g.p >= min(t[0] for t in tiers)) & (g.sh3_10 <= np.maximum(2.5, 3 * su * np.sqrt(3)))
    return g, ok, tier_limit(g.p.values, tiers)


def fill(row, cap, shares):
    got = cost = fee = 0.0
    for px, sz in ((row.x_ask, row.x_sz), (row.x_ask2, row.x_sz2)):
        if np.isfinite(px) and px <= cap + 1e-9 and got < shares - 1e-9:
            q = min(shares - got, sz)
            got, cost, fee = got + q, cost + q * px, fee + q * fee_per_share(px)
    return got, cost, fee


def hedge(e, g, H, cap_fn, retry_ms=1000, min_rel=0.0, slip=None):
    """e: entries (run + limits). Returns e with hedge columns and pnl_h (window P&L with the insurance).
    slip: the order's cap is also at most the ask seen at the decision + slip (the live FAK is a dollar budget: a cap far above
    the ask would buy more shares than the position, a bet instead of insurance)."""
    idx = {k: w.sort_values("t") for k, w in g.groupby(["asset", "T"], sort=False)}
    out = []
    for r in e.itertuples():
        w = idx[(r.asset, r.T)]
        w = w[(w.t >= r.t + 300) & (w.side != r.side) & (w.p >= H) & (w.rel >= min_rel)]
        hs = hc = hf = 0.0
        ht = np.nan
        nt = 0
        for x in w.itertuples():
            if x.t < nt:
                continue
            cap = cap_fn(x.p) if slip is None else min(cap_fn(x.p), x.ask + slip)
            if slip is not None and x.ask > cap_fn(x.p) + 1e-9:
                continue                                    # the bot sends nothing while the seen ask is above the cap
            got, cost, fee = fill(x, cap, r.sh)
            if got > 0:
                hs, hc, hf, ht = got, cost, fee, x.rel
                break
            nt = x.t + retry_ms
        won_o = not r.won
        pnl_h = r.pnl + (hs if won_o else 0.0) - hc - hf
        out.append({"h_sh": hs, "h_px": hc / hs if hs else np.nan, "h_rel": ht, "pnl_h": pnl_h})
    return pd.concat([e.reset_index(drop=True), pd.DataFrame(out)], axis=1)


def summ(name, x, days):
    hed = x[x.h_sh > 0]
    lost0 = x[~x.won]
    big0, big1 = int((x.pnl < -3).sum()), int((x.pnl_h < -3).sum())
    per = x.groupby("period").pnl_h.sum()
    return (f"{name:34s} entries {len(x):3d}  hedged {len(hed):3d} (of them winners {int(hed.won.sum()):2d})  "
            f"pnl {x.pnl.sum():+7.2f} -> {x.pnl_h.sum():+7.2f}  losses<-$3: {big0} -> {big1}  worst {x.pnl.min():+.2f} -> {x.pnl_h.min():+.2f}  "
            f"losers saved {(lost0.pnl_h - lost0.pnl).sum():+6.2f}  winners cost {(x[x.won].pnl_h - x[x.won].pnl).sum():+6.2f}  "
            f"periods<0 {int((per < 0).sum())}/{len(per)}")


if __name__ == "__main__":
    g, ok, lim = load_live(PER)
    e = limits(run(g, ok, lim).sort_values("t"), 5, 1e9).reset_index(drop=True)
    days = sum((g[g.period == p]["T"].max() - g[g.period == p]["T"].min() + 300) / 86400 for p in PER)
    print(f"periods {PER}: {days:.2f} days, live rule (3 tiers, one entry at a time): {len(e)} entries, {int((~e.won).sum())} lost, "
          f"pnl {e.pnl.sum():+.2f}")
    print("losing entries: when the other side first reached p (model) and its ask then:")
    for r in e[~e.won].itertuples():
        w = g[(g.asset == r.asset) & (g["T"] == r.T) & (g.t >= r.t + 300) & (g.side != r.side)].sort_values("t")
        marks = []
        for H in (0.5, 0.7, 0.9):
            y = w[w.p >= H].head(1)
            marks.append(f"p>={H}: s{y.rel.iloc[0]:.1f} ask {y.x_ask.iloc[0]:.2f}" if len(y) else f"p>={H}: never")
        print(f"   {r.period} {r.asset} T={r.T} {r.side} entry s{r.rel:.1f} @ {r.px:.3f}: " + "; ".join(marks))
    variants = []
    for H in (0.5, 0.6, 0.65, 0.7, 0.8, 0.9):
        variants.append((f"H {H}, price <= model", H, lambda p: p - 0.0))
        variants.append((f"H {H}, price <= model - 10c", H, lambda p: p - 0.10))
        variants.append((f"H {H}, price <= model - 20c", H, lambda p: p - 0.20))
    print("insurance variants (buy the other side, same shares; the order cap = min(that, seen ask + 2c), as in the bot):")
    for name, H, cap in variants:
        x = hedge(e, g, H, cap, slip=0.02)
        print("  " + summ(name, x, days))
    x = hedge(e, g, 0.65, lambda p: p - 0.20, slip=0.02)
    print("the rule in the bot (H 0.65, model - 20c, seen ask + 2c), per period:")
    print(x.groupby("period")[["pnl", "pnl_h"]].sum().round(2).T.to_string())
