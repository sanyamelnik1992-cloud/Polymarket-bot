"""Rule simulator on the state grids (grid.py). Entry = first step of a window where the rule accepts; FAK on the
book 300 ms later (2 levels), a miss retries 1 s later; 5 shares. Grids: REPLAY_GRID (folder with PERIOD.parquet).

  g = load(); gg = g.assign(p=p_sigma(g, np.maximum(g.sigma_bp, 0.5)), p_cl=p_sigma(g, np.maximum(g.sigma_bp, 0.5), "cl"))
  e = run(gg, base_ok(gg) & (gg.p >= 0.90), tier_limit(gg.p.values, L3)); print(summ(limits(e, 5), "L3 + floor 0.5"))
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO)
from pmbot.fees import fee_per_share as fee_ps  # noqa: E402
from pmbot.lockmodel import FLOOR, Tables  # noqa: E402

GRID = os.environ.get("REPLAY_GRID", "grid")
L3 = [[0.99, 0.98], [0.97, 0.95], [0.95, 0.93], [0.90, 0.91]]


def load(periods=None):
    fs = sorted(f for f in glob.glob(os.path.join(GRID, "*.parquet")) if not f.endswith("gamma_close.parquet"))
    out = []
    for f in fs:
        name = os.path.basename(f)[:-8]
        if periods and name not in periods:
            continue
        d = pd.read_parquet(f)
        d["period"] = name
        out.append(d)
    g = pd.concat(out, ignore_index=True)
    gc = os.path.join(GRID, "gamma_close.parquet")          # real outcomes of the close windows (gamma_outcomes.py)
    if os.path.exists(gc):
        for r in pd.read_parquet(gc).dropna(subset=["g_winner"]).itertuples():
            ix = (g.asset == r.asset) & (g["T"] == int(r.window_start))
            if ix.any():
                g.loc[ix, "won"] = (g.loc[ix, "side"] == ("U" if r.g_winner == "Up" else "D")).values
    g["rel"] = g.t / 1000 - g["T"]
    sg = np.where(g.side == "U", 1.0, -1.0)
    g["adv1"] = -sg * g.mv1
    g["adv3"] = -sg * g.mv3
    g["z"] = g.dist_bp / (g.sigma_bp * np.sqrt(np.maximum(g.m, 1)))   # distance in sigmas of the remaining path (rough)
    return g


def base_ok(g, agree=0.90, dmin=0.5, adv1=1.0, adv3=2.0, jump=5.0):
    return (g.p_cl >= agree) & (g.dist_bp >= dmin) & (g.spot_bp >= 0) & (g.adv1 <= adv1) & (g.adv3 <= adv3) & \
        (g.mv1.abs() <= jump)


def tier_limit(p, tiers):
    lim = np.full(len(p), np.nan)
    for pm, am in sorted(tiers):
        lim = np.where(p >= pm - 1e-12, np.fmax(np.nan_to_num(lim, nan=0), am), lim)
    return lim


def run(g, ok, limit, a_min=0.90, shares=5.0, retry_ms=1000):
    """ok: bool Series (state filters), limit: array of max ask per row. Returns entries DataFrame."""
    d = g[ok & (g.ask <= limit + 1e-9) & (g.ask >= a_min - 1e-9)].copy()
    d["limit"] = np.asarray(limit)[d.index]
    rows = []
    for (a, T), w in d.groupby(["asset", "T"], sort=False):
        nt = 0
        for r in w.itertuples():
            if r.t < nt:
                continue
            got = cost = f = 0.0
            for px, sz in ((r.x_ask, r.x_sz), (r.x_ask2, r.x_sz2)):
                if np.isfinite(px) and px <= r.limit + 1e-9 and got < shares:
                    q = min(shares - got, sz)
                    got += q
                    cost += q * px
                    f += q * fee_ps(px)
            if got > 0:
                rows.append({**r._asdict(), "px": cost / got, "sh": got,
                             "pnl": (got if r.won else 0.0) - cost - f})
                break
            nt = r.t + retry_ms
    return pd.DataFrame(rows)


def limits(e, max_open=5.0, day_stop=5.0):
    """Live limits on top: one entry at a time per window end (max_open $), daily stop."""
    e = e.sort_values("t")
    keep, day_pnl, killed = [], {}, set()
    used = {}
    for r in e.itertuples():
        day = pd.Timestamp(r.T + 300, unit="s").strftime("%m-%d")
        if day in killed:
            continue
        # losses known at the window end: count pnl of windows that ended before this entry
        dp = sum(p for (end, p) in day_pnl.get(day, []) if end * 1000 <= r.t)
        if dp <= -day_stop:
            killed.add(day)
            continue
        c = used.get(r.T, 0.0)
        if c + 5 * r.limit > max_open + 1e-9:
            continue
        used[r.T] = c + 5 * r.limit
        day_pnl.setdefault(day, []).append((r.T + 300, r.pnl))
        keep.append(r.Index)
    return e.loc[keep]


def summ(e, name=""):
    if not len(e):
        return f"{name:28s} 0 entries"
    by = e.groupby("period").pnl.sum().round(1).to_dict()
    return (f"{name:28s} n={len(e):4d} lost={int((~e.won).sum()):3d} win={100 * e.won.mean():5.1f}% "
            f"px={e.px.mean():.3f} pnl={e.pnl.sum():+7.2f} c/sh={100 * e.pnl.sum() / e.sh.sum():+5.2f} {by}")


def run_delay(g, ok, limit, a_min=0.90, shares=5.0, retry_ms=1000, exec_ms=1000, persist=False):
    """Like run(), but the FAK meets the book exec_ms later (looked up in the grid rows of the same window and side).
    persist=True: a fill needs the ask <= limit at EVERY step from t to t+exec_ms (nobody faster wanted it)."""
    gl = g.assign(limit=np.asarray(limit))
    cand = gl[ok & (gl.ask <= gl.limit + 1e-9) & (gl.ask >= a_min - 1e-9)]
    rows = []
    allw = {k: w.set_index("t") for k, w in gl.groupby(["asset", "T"], sort=False)}
    for (a, T), w in cand.groupby(["asset", "T"], sort=False):
        full = allw[(a, T)]
        nt = 0
        for r in w.itertuples():
            if r.t < nt:
                continue
            te = r.t + exec_ms
            got = False
            if te in full.index:
                x = full.loc[te]
                if x.side == r.side and np.isfinite(x.ask) and x.ask <= r.limit + 1e-9:
                    got = True
                    if persist:
                        seg = full.loc[r.t:te]
                        got = bool(((seg.side == r.side) & (seg.ask <= r.limit + 1e-9)).all()) and len(seg) >= exec_ms / 100
                    if got:
                        px = x.ask
                        rows.append({**r._asdict(), "px": px, "sh": shares,
                                     "pnl": (shares if r.won else 0.0) - shares * px - shares * fee_ps(px)})
                        break
            nt = r.t + retry_ms
    return pd.DataFrame(rows)


_TAB = None


def p_sigma(g, sig_bp, which="nc"):
    """Model probability of the favoured side recomputed with another sigma (array, bp)."""
    global _TAB
    if _TAB is None:
        _TAB = Tables(os.path.join(REPO, "config", "lock_tables.npz"))
    y = (g["y_" + which].values / (np.asarray(sig_bp) * 1e-4))
    m = g["m_" + which].values.astype(int)
    out = np.full(len(g), np.nan)
    for a in g.asset.unique():
        tab = _TAB.s[a]
        ia = (g.asset == a).values
        for mm in np.unique(m[ia]):
            ix = ia & (m == mm)
            if mm <= 0:
                continue
            out[ix] = np.interp(y[ix], _TAB.y, tab[min(mm, 60) - 1], left=1.0, right=0.0)
    out = np.clip(out, FLOOR, 1 - FLOOR)
    return np.where(g.side.values == "U", out, 1 - out)
