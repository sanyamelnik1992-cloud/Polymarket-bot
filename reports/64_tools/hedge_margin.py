"""Insurance with a margin: buy the other side only at <= model p - margin (the model is over-sure right after a reversal:
p 0.80 on average at the purchase, the other side won 52-75 %). Samples: live-rule replay of 9 periods, real entries of 10-11 Nov,
wider replay rules split into 7-9 Nov and 10-11 Nov."""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from hedge_sim import PER, base_ok, hedge, limits, load, load_live, run, tier_limit, L3  # noqa: E402
from hedge_eval import journal_entries  # noqa: E402

OLD = ["r1107", "r1108", "r1109", "d1109", "d1110"]
g, ok, lim = load_live(PER)
samples = {}
e = limits(run(g, ok, lim).sort_values("t"), 5, 1e9).reset_index(drop=True)
samples["live rule replay 7-9 Nov"] = (e[e.period.isin(OLD)].reset_index(drop=True), g)
samples["live rule replay 10-11 Nov"] = (e[~e.period.isin(OLD)].reset_index(drop=True), g)
je = journal_entries(g, os.path.join(HERE, "entries_10_11nov.json"))
samples["real entries 10-11 Nov, live"] = (je[je.unit == "lock-live"].reset_index(drop=True), g)
samples["real entries 10-11 Nov, 3 paper bots"] = (je[je.unit != "lock-live"].reset_index(drop=True), g)
g0 = load(PER)
order = {p: i for i, p in enumerate(PER)}
first = g0.assign(o=g0.period.map(order)).groupby(["asset", "T"]).o.transform("min")
g0 = g0[g0.period.map(order) == first].reset_index(drop=True)
for name, a_min in (("plain L3 4 tiers", 0.90), ("L4", 0.50)):
    okw = base_ok(g0) & (g0.p >= 0.90)
    e = limits(run(g0, okw, tier_limit(g0.p.values, L3), a_min=a_min).sort_values("t"), 5, 1e9).reset_index(drop=True)
    samples[f"{name} replay 7-9 Nov"] = (e[e.period.isin(OLD)].reset_index(drop=True), g0)
    samples[f"{name} replay 10-11 Nov"] = (e[~e.period.isin(OLD)].reset_index(drop=True), g0)
margins = (0.0, 0.10, 0.20)
H = float(os.environ.get("HEDGE_P", "0.65"))
print("insurance from model p >=", H, "; the order cap = min(model - margin, seen ask + 2c)")
print(f"{'sample':40s} {'entries':>7s} {'lost':>4s} {'pnl':>8s} | " + " | ".join(f"m={m:.2f}: gain (hedges/for nothing, big losses)" if i == 0 else f"m={m:.2f}" for i, m in enumerate(margins)))
tot = {m: 0.0 for m in margins}
for name, (e, gg) in samples.items():
    cells = []
    for m in margins:
        x = hedge(e, gg, H, lambda p, m=m: p - m, slip=0.02)
        hed = x[x.h_sh > 0]
        gain = x.pnl_h.sum() - x.pnl.sum()
        tot[m] += gain
        cells.append(f"{gain:+6.2f} ({len(hed)}/{int(hed.won.sum())}, {int((x.pnl < -3).sum())}->{int((x.pnl_h < -3).sum())})")
    print(f"{name:40s} {len(e):7d} {int((~e.won).sum()):4d} {e.pnl.sum():+8.2f} | " + " | ".join(cells))
print("sum of gains over the samples (they overlap: real entries ~ replays of 10-11 Nov):", {m: round(v, 2) for m, v in tot.items()})
