"""Summarise a bot run from its journal (paper or live).

usage: python scripts/report_journal.py JOURNAL_RUN_DIR
"""
import json
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

pd.set_option("display.width", 200)


def main(d):
    ev = [json.loads(line) for line in open(os.path.join(d, "events.jsonl")) if line.strip()]
    kinds = Counter(e["type"] for e in ev)
    start = next((e for e in ev if e["type"] == "start"), None)
    t0, t1 = ev[0]["t"], ev[-1]["t"]
    print(f"run: {d}\nmode: {start and start.get('mode')}  duration: {(t1 - t0) / 60:.1f} min  events: {dict(kinds)}")
    st = pd.DataFrame([e for e in ev if e["type"] == "settle"])
    fills = pd.DataFrame([e for e in ev if e["type"] == "fill"])
    quotes = pd.DataFrame([e for e in ev if e["type"] == "quote"])
    filt = pd.DataFrame([e for e in ev if e["type"] == "filters"])
    if not filt.empty:
        filt["asset"] = filt.key.str.split("-").str[0]
        r = Counter(x for rs in filt.reasons for x in rs)
        print("\nfilter reasons (state changes):", dict(r.most_common()))
    if not quotes.empty:
        print(f"quotes placed: {len(quotes)}")
    if fills.empty:
        print("no fills")
        return
    fills["asset"] = fills.key.str.split("-").str[0]
    fills["notional"] = fills.price * fills["size"]
    fills["rel"] = fills.ts / 1000 - fills.key.str.split("-").str[1].astype(int)
    print(f"\nfills: {len(fills)}  shares: {fills['size'].sum():.0f}  notional: ${fills.notional.sum():.2f}")
    print(fills.groupby("asset").agg(fills=("size", "size"), shares=("size", "sum"), notional=("notional", "sum"),
                                     avg_price=("price", "mean"), med_rel=("rel", "median")).round(3))
    if st.empty:
        print("no settled windows yet")
        return
    st["asset"] = st.key.str.split("-").str[0]
    st["pnl_ex_rebate"] = st.pnl - st.rebate
    g = st.groupby("asset")
    summ = pd.DataFrame({
        "windows": g.size(), "cost": g.cost.sum(), "pnl": g.pnl.sum(), "pnl_ex_rebate": g.pnl_ex_rebate.sum(),
        "rebate": g.rebate.sum(), "win_rate": g.pnl.apply(lambda x: (x > 0).mean()),
        "worst": g.pnl.min(), "best": g.pnl.max()})
    summ["roi"] = summ.pnl / summ.cost
    print("\nsettled windows:")
    print(summ.round(3).to_string())
    tot = st.pnl.sum()
    tstat = st.pnl.mean() / st.pnl.std() * np.sqrt(len(st)) if len(st) > 2 and st.pnl.std() > 0 else float("nan")
    print(f"\nTOTAL pnl ${tot:+.2f} (ex-rebate ${st.pnl_ex_rebate.sum():+.2f}) on cost ${st.cost.sum():.2f} "
          f"-> ROI {tot / st.cost.sum():+.2%}; windows {len(st)}, t-stat {tstat:+.2f}")
    st = st.sort_values("t")
    eq = st.pnl.cumsum()
    print(f"max drawdown ${(eq - eq.cummax()).min():.2f}; hedged windows (both sides filled): "
          f"{((st.up > 0) & (st.down > 0)).sum()}")


if __name__ == "__main__":
    main(sys.argv[1])
