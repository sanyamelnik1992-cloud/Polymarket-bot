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


def lock_orders(ev):
    """Taker orders of a lock bot: how many found their ask, how long the exchange took, why the others failed.
    (Until 9 Nov a rejected FAK was an `order_error` without details: 26 of 42 live orders, reports/52.)"""
    ent = [e for e in ev if e["type"] == "lock_entry" and not e.get("maker")]
    miss = [e for e in ev if e["type"] == "lock_miss"]
    err = [e for e in ev if e["type"] in ("order_error", "order_failed", "order_pending")]
    rej = [e for e in ev if e["type"] == "order_rejected"]
    if not (ent or miss or err or rej):
        return
    clean = [e for e in miss if not e.get("error")]
    print(f"\nlock orders: sent {len(ent) + len(miss)}  filled {len(ent)}  ask gone {len(clean)}  "
          f"errors {len(miss) - len(clean)}")
    ms = sorted(e["ms"] for e in ent + clean if e.get("ms") is not None)
    if ms:
        print(f"  signal -> exchange answer, ms: median {ms[len(ms) // 2]:.0f}  p90 {ms[min(len(ms) - 1, int(0.9 * len(ms)))]:.0f}  "
              f"max {ms[-1]:.0f}")
    call = sorted(e["latency_ms"] for e in ev if e["type"] in ("order_filled", "order_rejected") and e.get("latency_ms") is not None)
    if call:                       # the exchange call alone: the rest of "signal -> answer" is spent inside our process
        print(f"  exchange call alone, ms: median {call[len(call) // 2]:.0f}  p90 {call[min(len(call) - 1, int(0.9 * len(call)))]:.0f}  "
              f"max {call[-1]:.0f}")
    q = sorted(e["queue_ms"] for e in ent + clean if e.get("queue_ms") is not None)
    if q:                          # decision -> the order task running (our own event loop)
        print(f"  decision -> order sent, ms: median {q[len(q) // 2]:.0f}  p90 {q[min(len(q) - 1, int(0.9 * len(q)))]:.0f}  max {q[-1]:.0f}")
    gcm = [e["stats"].get("gc_max_ms") for e in ev if e["type"] == "status" and isinstance(e.get("stats"), dict)]
    gcm = sorted(x for x in gcm if x is not None)
    if gcm:                        # the longest garbage-collector pause of each 30 s status
        print(f"  GC pause per 30 s, ms: median {gcm[len(gcm) // 2]:.0f}  p90 {gcm[min(len(gcm) - 1, int(0.9 * len(gcm)))]:.0f}  "
              f"max {gcm[-1]:.0f}")
    age = sorted(e["sig_age_ms"] for e in ent + clean if e.get("sig_age_ms") is not None)
    if age:                        # fair bots: how old the Binance tick was when the bot decided
        print(f"  signal age at the decision, ms: median {age[len(age) // 2]:.0f}  p90 {age[min(len(age) - 1, int(0.9 * len(age)))]:.0f}")
    warm = sorted(e["ms"] for e in ev if e["type"] == "prewarm" and e.get("ms") is not None)
    if warm:
        print(f"  market prewarm (2 lookups + signing), ms: median {warm[len(warm) // 2]:.0f}  max {warm[-1]:.0f}  n {len(warm)}")
    if ent:
        slip = [100 * (e["price"] - e["ask"]) for e in ent if e.get("ask") is not None and e.get("price") is not None]
        if slip:
            print(f"  fill price minus the ask we saw, cents: mean {sum(slip) / len(slip):+.2f}  worst {max(slip):+.1f}")
    if clean:
        left = [300 - e["rel"] for e in clean if e.get("rel") is not None]
        if left:
            print(f"  misses came {min(left):.0f}-{max(left):.0f} s before the end; model p {min(e.get('p', 0) for e in clean):.3f}-"
                  f"{max(e.get('p', 0) for e in clean):.3f}")
    codes = Counter(e.get("code", "?") for e in rej)
    if codes:
        print(f"  exchange rejections by code: {dict(codes)}")
    for e in err[-5:]:
        txt = e.get("error") or e.get("status") or ""
        print(f"  {e['type']} {e.get('key', '')}: {e.get('err', '')} {str(txt)[:160]}")


def fair_breakdown(ev):
    """Fair-value bots (reports/83 section 14): orders and entries by edge, ask size and signal age, with the outcome of every
    entry from its window's settle - which signals our speed still gets, and whether those earn."""
    ent = [e for e in ev if e["type"] == "lock_entry" and e.get("fair")]
    miss = [e for e in ev if e["type"] == "lock_miss" and e.get("fair") and not e.get("error")]
    if not ent and not miss:
        return
    win = {e["key"]: e.get("winner") for e in ev if e["type"] == "settle"}

    def bucket(x, edges):
        if x is None:
            return "?"
        for lo, hi in zip(edges, edges[1:]):
            if lo <= x < hi:
                return f"{lo:g}-{hi:g}"
        return f">={edges[-1]:g}"

    for name, field_, edges in (("edge, c", "edge", (0.15, 0.20, 0.25, 0.35)), ("ask size, shares", "ask_sz", (0, 10, 50, 200)),
                                ("signal age, ms", "sig_age_ms", (0, 100, 300, 1000))):
        rows = {}
        for e in ent + miss:
            x = e.get(field_)
            r = rows.setdefault(bucket(x * 100 if (field_ == "edge" and x is not None) else x,
                                       tuple(v * 100 for v in edges) if field_ == "edge" else edges),
                                {"orders": 0, "filled": 0, "settled": 0, "won": 0, "exp": 0.0, "pnl": 0.0})
            r["orders"] += 1
            if e["type"] == "lock_entry":
                r["filled"] += 1
                w = win.get(e["key"])
                if w and e.get("price") is not None:
                    won = (e["side"] == "U") == (w == "Up")
                    sh, px = float(e.get("shares") or 0), float(e["price"])
                    r["settled"] += 1
                    r["won"] += won
                    r["exp"] += px
                    r["pnl"] += sh * ((1.0 if won else 0.0) - px - 0.07 * px * (1 - px))
        print(f"\nfair entries by {name}: orders / filled / settled / won (expected at the paid prices) / pnl")
        for k in sorted(rows, key=lambda k: (k == "?", float(k.split("-")[0].lstrip(">=")) if k != "?" else 0)):
            r = rows[k]
            print(f"  {k:>10s}: {r['orders']:4d} / {r['filled']:4d} ({100 * r['filled'] / max(r['orders'], 1):3.0f} %) / {r['settled']:4d} / "
                  f"{r['won']:4d} ({r['exp']:6.1f}) / {r['pnl']:+8.2f}")


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
    lock_orders(ev)
    fair_breakdown(ev)
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
