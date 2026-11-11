"""The insurance of hedge_sim.py on more entries than the live rule's replay: (a) the real entries of live and the paper bots
(journals 10-11 Nov, entries_10_11nov.json), (b) wider replay rules with more losers (plain L3 with four tiers, L4 = asks from 0.50).
usage: REPLAY_GRID=<grids with GRID_P_MIN=0.5> python reports/64_tools/hedge_eval.py"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from hedge_sim import PER, base_ok, hedge, limits, load, load_live, run, tier_limit, L3  # noqa: E402

CAPS = {"price <= model": lambda p: p, "price <= model - 10c": lambda p: p - 0.10, "price <= model - 20c": lambda p: p - 0.20}
HS = (0.55, 0.6, 0.65, 0.7, 0.75, 0.8)


def show(name, x):
    hed = x[x.h_sh > 0]
    lost = x[~x.won]
    return (f"{name:44s} n {len(x):3d} lost {len(lost):2d}  hedged {len(hed):2d} (winners {int(hed.won.sum())})  pnl {x.pnl.sum():+7.2f} -> "
            f"{x.pnl_h.sum():+7.2f}  < -$3: {int((x.pnl < -3).sum())} -> {int((x.pnl_h < -3).sum())}  saved {(lost.pnl_h - lost.pnl).sum():+6.2f}"
            f"  cost {(x[x.won].pnl_h - x[x.won].pnl).sum():+6.2f}")


def journal_entries(g, path):
    rows = json.load(open(path))
    have = set(map(tuple, g[["asset", "T"]].drop_duplicates().values.tolist()))
    out = []
    for r in rows:
        if r["pnl"] is None or r["unit"] in ("lock-99", "lock-99-live"):
            continue
        a, T = r["key"].split("-")[0], int(r["key"].split("-")[1])
        if (a, T) not in have:
            continue
        out.append({"unit": r["unit"], "asset": a, "T": T, "side": r["side"], "t": int(r["t"] * 1000), "px": r["price"],
                    "sh": 5.0, "won": r["pnl"] > 0, "pnl": r["pnl"] * 5.0 / max(r.get("shares") or 5.0, 1e-9) if False else r["pnl"],
                    "period": "live", "rel": r["rel"], "p": r["p"]})
    return pd.DataFrame(out)


if __name__ == "__main__":
    g, ok, lim = load_live(PER)
    print("== (a) real entries of 10-11 Nov (journals), the insurance replayed on the recorded books after each entry")
    je = journal_entries(g, os.path.join(HERE, "entries_10_11nov.json"))
    for unit, w in je.groupby("unit"):
        print(f"   {unit}: {len(w)} entries in the recorded windows, lost {int((~w.won).sum())}, pnl {w.pnl.sum():+.2f}")
    for H in HS:
        for cn, cap in CAPS.items():
            x = hedge(je, g, H, cap, slip=0.02)
            print("   " + show(f"all bots, H {H}, {cn}", x))
    for unit in ("lock-live",):
        x = hedge(je[je.unit == unit].reset_index(drop=True), g, 0.65, CAPS["price <= model - 20c"], slip=0.02)
        print("   " + show(f"{unit} only, H 0.65, price <= model - 20c", x))
        print(x[(x.h_sh > 0) | (~x.won)][["asset", "T", "side", "rel", "px", "won", "h_px", "h_rel", "pnl", "pnl_h"]].round(3).to_string())
    print("== (b) wider replay rules (more entries, more losers), one entry at a time")
    g0 = load(PER)
    order = {p: i for i, p in enumerate(PER)}
    first = g0.assign(o=g0.period.map(order)).groupby(["asset", "T"]).o.transform("min")
    g0 = g0[g0.period.map(order) == first].reset_index(drop=True)
    for name, a_min in (("plain L3, 4 tiers, no floor/pause", 0.90), ("L4: 4 tiers, asks from 0.50", 0.50)):
        okw = base_ok(g0) & (g0.p >= 0.90)
        e = limits(run(g0, okw, tier_limit(g0.p.values, L3), a_min=a_min).sort_values("t"), 5, 1e9).reset_index(drop=True)
        for H in HS:
            x = hedge(e, g0, H, CAPS["price <= model - 20c"], slip=0.02)
            print("   " + show(f"{name}, H {H}, price <= model - 20c", x))
        x = hedge(e, g0, 0.65, CAPS["price <= model - 20c"], slip=0.02)
        per = x.groupby("period")[["pnl", "pnl_h"]].sum().round(2)
        print("     per period (H 0.65):", "; ".join(f"{p} {a:+.1f}->{b:+.1f}" for p, (a, b) in per.iterrows()))
