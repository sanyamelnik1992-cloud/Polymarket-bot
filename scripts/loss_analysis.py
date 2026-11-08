"""Why do we lose? Post-mortem of 5m windows from the server export (settles.csv in the server-reports branch).

usage: python scripts/loss_analysis.py SETTLES_CSV [--units=pmbot-5m-b,pmbot-5m-d] [--worst=15]

Works on one row per settled window: which side(s) we filled, the average price we paid, the winner, the P&L.
For windows where only ONE side filled, the average price is the market's implied probability of that side, so
  edge = win - price   (per share, before the maker rebate)
is the adverse-selection measure: a positive edge means our fills won more often than their price implied.
A z-score against the implied probability says whether a gap is more than chance.
"""
import sys

import numpy as np
import pandas as pd

pd.set_option("display.width", 200)


def load(path, units=None):
    d = pd.read_csv(path, parse_dates=["time_utc"])
    if units:
        d = d[d.unit.isin(units)]
    d["asset"] = d.key.str.split("-").str[0]
    d["hour"] = d.time_utc.dt.hour
    d["shares"] = d.up + d.down
    d["p_avg"] = d.cost / d.shares
    d["both"] = (d.up > 0) & (d.down > 0)
    return d


def zscore(wins, n, p):
    var = n * p * (1 - p)
    return (wins - n * p) / np.sqrt(var) if var > 0 else np.nan


def one_sided_table(one, by):
    g = one.groupby(by, observed=True)
    out = pd.DataFrame({"n": g.size(), "implied_p": g.p_avg.mean(), "win_rate": g.won.mean(),
                        "wins": g.won.sum()})
    out["edge_c"] = 100 * (out.win_rate - out.implied_p)
    out["z"] = [zscore(w, n, p) for w, n, p in zip(out.wins, out.n, out.implied_p)]
    return out.drop(columns="wins").round(3)


def main(path, units=None, worst=15):
    d = load(path, units)
    print(f"windows {len(d)}, total P&L {d.pnl.sum():+.2f}$, losing windows {(d.pnl < 0).sum()} "
          f"(sum {d.pnl[d.pnl < 0].sum():+.1f}$), winning windows sum {d.pnl[d.pnl > 0].sum():+.1f}$")
    print("\n1. by window type (where the losses come from):")
    cls = np.where(d.both, "both sides filled", "one side filled")
    print(d.assign(cls=cls).groupby("cls").pnl.agg(["size", "sum", "mean"]).round(2).to_string())

    one = d[~d.both].copy()
    one["side"] = np.where(one.up > 0, "U", "D")
    one["won"] = (((one.side == "U") & (one.winner == "Up")) | ((one.side == "D") & (one.winner == "Down"))).astype(float)
    one["edge"] = one.won - one.p_avg
    print(f"\n2. one-sided windows: {len(one)}, implied p {one.p_avg.mean():.3f}, actual win {one.won.mean():.3f}, "
          f"edge {100 * one.edge.mean():+.2f}c per share (z {zscore(one.won.sum(), len(one), one.p_avg.mean()):+.2f})")
    print("\n2a. by bot:")
    print(one_sided_table(one, "unit").to_string())
    print("\n2b. by fill price (the cheap side is where we lose):")
    bands = pd.cut(one.p_avg, [0, 0.3, 0.4, 0.5, 0.6, 0.7, 1], labels=["<.3", ".3-.4", ".4-.5", ".5-.6", ".6-.7", ">.7"])
    print(one_sided_table(one.assign(band=bands), "band").to_string())
    print("\n2c. by asset:")
    print(one_sided_table(one, "asset").to_string())
    print("\n2d. by side:")
    print(one_sided_table(one, "side").to_string())
    print("\n2e. by UTC time block:")
    blocks = pd.cut(one.hour, [-1, 5, 11, 17, 23], labels=["00-05", "06-11", "12-17", "18-23"])
    print(one_sided_table(one.assign(block=blocks), "block").to_string())

    print("\n3. cost of a window vs its loss (a losing window costs most of its budget):")
    cb = pd.cut(d.cost, [0, 5, 10, 15, 100], labels=["<5", "5-10", "10-15", ">15"])
    print(d.assign(cost_b=cb).groupby("cost_b", observed=True).pnl.agg(["size", "sum", "mean"]).round(2).to_string())

    print(f"\n4. the {worst} worst windows:")
    cols = ["time_utc", "unit", "asset", "winner", "up", "down", "cost", "p_avg", "pnl"]
    print(d.nsmallest(worst, "pnl")[cols].round(2).to_string(index=False))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = {a.split("=", 1)[0][2:]: a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--") and "=" in a}
    units = opts["units"].split(",") if "units" in opts else None
    main(args[0], units, int(opts.get("worst", 15)))
