"""Summaries of run_variants.py outputs: raw, and with the live limits (max_open via shares x limit like LockBot,
daily stop -$5 per UTC day counted at window end; a stopped day takes no more entries)."""
import os
import sys

import pandas as pd

OUT = os.path.join(os.path.abspath(os.environ.get("REPLAY_DATA", "data")), "out")
periods = sys.argv[1].split(",")
variants = sys.argv[2].split(",") if len(sys.argv) > 2 else ["L", "th098", "L3top", "L3", "L4"]


def load(v):
    fs = [pd.read_csv(os.path.join(OUT, f"{v}__{p}.csv")).assign(period=p)
          for p in periods if os.path.exists(os.path.join(OUT, f"{v}__{p}.csv"))]
    fs = [f for f in fs if len(f)]
    return pd.concat(fs, ignore_index=True) if fs else pd.DataFrame()


def limited(r, max_open=10.0, stop=5.0):
    if r.empty:
        return r
    r = r.sort_values(["T", "rel"]).copy()
    r["day"] = pd.to_datetime(r["T"] + 300, unit="s").dt.date
    keep, day_pnl, stopped = [], {}, set()
    for T, g in r.groupby("T", sort=True):
        d = pd.to_datetime(T + 300, unit="s").date()
        if d in stopped:
            continue
        open_ = 0.0
        for i, e in g.iterrows():
            lim = e["limit"]
            if open_ + 5 * lim > max_open + 1e-9:
                continue
            open_ += 5 * lim
            keep.append(i)
        pnl = r.loc[[i for i in keep if r.loc[i, "T"] == T], "pnl"].sum()
        day_pnl[d] = day_pnl.get(d, 0.0) + pnl
        if day_pnl[d] <= -stop:
            stopped.add(d)
    out = r.loc[keep].copy()
    out.attrs["stopped"] = sorted(stopped)
    return out


def line(name, r):
    if r.empty:
        return f"{name:8s} 0"
    n, lost = len(r), int((~r.won).sum())
    days = r.groupby(pd.to_datetime(r["T"] + 300, unit="s").dt.date).pnl.sum()
    return (f"{name:8s} n={n:4d} lost={lost:3d} ({100 * lost / n:4.1f}%) pnl={r.pnl.sum():+7.2f} "
            f"avg_px={r.px.mean():.3f} EV/sh={100 * r.pnl.sum() / r.sh.sum():+5.2f}c "
            f"worst_day={days.min():+6.2f} days={' '.join(f'{d.day}:{v:+.1f}' for d, v in days.items())}")


for v in variants:
    r = load(v)
    print(line(v + " raw", r))
    for mo in (10.0, 5.0):
        lr = limited(r, mo)
        print(line(f"  mo{int(mo)}", lr), " stopped:", [str(d) for d in lr.attrs.get("stopped", [])])
    if not r.empty:
        lr = limited(r, 10.0)
        bands = pd.cut(lr.px, [0, 0.5, 0.8, 0.89, 0.91, 0.93, 0.95, 0.99])
        t = lr.groupby(bands, observed=True).agg(n=("won", "size"), lost=("won", lambda x: int((~x).sum())),
                                                  pnl=("pnl", "sum")).round(2)
        print("   by price:", "; ".join(f"{k}: {a.n}/{a.lost}L/{a.pnl:+.2f}" for k, a in t.iterrows()))
        print("   by period:", "; ".join(f"{p}: {len(g)}/{int((~g.won).sum())}L/{g.pnl.sum():+.2f}"
                                       for p, g in lr.groupby("period")))
