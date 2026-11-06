"""Liquidity-rewards farmer: one-day backtest on the real trade tape (shadow test, no orders).

Plan fixed in advance from the day-1 scan (scripts/rewards_scan.py CSV): pick markets by estimated net yield
until the capital budget is used (skip markets ending within 2 days and live sports / esports). In each market
we quote `size` shares on both sides `dist` cents around the day-1 displayed price (YES bid + NO bid).
Then we walk every public trade of the next day (Data API, taker side):
  * a taker SELL of YES (or BUY of NO) printing BELOW our bid fills our bid; a taker BUY of YES (or SELL of NO)
    printing ABOVE our ask fills our ask -- conservative trade-through, equal price = no fill;
  * after a fill the market pauses `cooldown_min`, then re-quotes around that trade price; after `max_fills`
    fills the market is dropped for the day.
Inventory is marked to the day-2 displayed price (scan CSV of day 2). Rewards = pool rate * our share *
quoting uptime, share computed against the competitors' score D measured on BOTH days (worse of the two).
Caveats: our presence would attract extra flow in empty books (not in the tape); rewards are an estimate.

usage: python scripts/rewards_backtest.py DAY1_CSV DAY1_MARKETS_JSON DAY2_CSV T0_UNIX T1_UNIX
       [--budget=2000] [--size=0 (=min size)] [--dist=1] [--cooldown=15] [--max-fills=3] [--competitive]
"""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from collect_history import collect_trades  # noqa: E402

LIVE = re.compile(r"\bvs\.?\b|o/u|spread:|handicap|game \d|map \d|winner|match", re.I)


def opt(name, default):
    return float(next((a.split("=", 1)[1] for a in sys.argv if a.startswith(f"--{name}=")), default))


def main():
    d1, mj, d2, t0, t1 = sys.argv[1:6]
    t0, t1 = int(t0), int(t1)
    budget, size_over, dist = opt("budget", 2000), opt("size", 0), opt("dist", 1)
    cool, max_fills = opt("cooldown", 15) * 60, int(opt("max-fills", 3))
    a = pd.read_csv(d1).drop_duplicates("slug")
    b = pd.read_csv(d2).drop_duplicates("slug").set_index("slug")
    mk = {m.get("market_slug"): m for m in json.load(open(mj))}
    a = a[(a.days_left > 2) & ~a.question.str.contains(LIVE) & a.slug.isin(mk)]
    if "--competitive" in sys.argv:                                   # only books that already had other makers
        a = a[a.comp_Q > 0]
    # re-price day-1 quotes at the chosen size (scores scale linearly with size)
    a["size2"] = np.maximum(a["size"], size_over)
    k = a.size2 / a["size"]
    a["our_Q2"], a["cap2"] = a.our_Q * k, a.capital * k
    comp2 = a.slug.map(b.comp_Q)
    a["comp_worst"] = np.fmax(a.comp_Q, comp2.fillna(a.comp_Q))
    a["rew_est"] = a.rate * a.our_Q2 / (a.comp_worst + a.our_Q2)
    a["fair2"] = a.slug.map(b.fair)
    a = a.assign(y=a.rew_est / a.cap2).sort_values("y", ascending=False)   # best estimated yield first
    pick = a[a.cap2.cumsum() <= budget].copy()
    print(f"picked {len(pick)} markets, capital ${pick.cap2.sum():.0f}, day-1 est. rewards ${pick.rew_est.sum():.0f}/day")
    conds = [mk[s]["condition_id"] for s in pick.slug]
    with ThreadPoolExecutor(8) as ex:
        tapes = dict(zip(pick.slug, ex.map(collect_trades, conds)))
    rows = []
    for _, r in pick.iterrows():
        m = mk[r.slug]
        yes_tok = m["tokens"][0]["token_id"]
        tr = [x for x in tapes[r.slug] if t0 <= int(x["timestamp"]) < t1]
        tr.sort(key=lambda x: int(x["timestamp"]))
        f, q = float(r.fair), float(r.size2)
        paused_until, paused_s, fills, inv, cash = t0, 0.0, 0, 0.0, 0.0
        log = []
        for x in tr:
            ts = int(x["timestamp"])
            if fills >= max_fills:
                break
            if ts < paused_until:
                continue
            p = float(x["price"])
            is_yes = x.get("token_id") == yes_tok or x.get("outcome_index") == 0
            p_yes = p if is_yes else 1 - p
            buys_yes = (x["side"] == "BUY") == bool(is_yes)
            bid, ask = round(f - dist / 100, 3), round(f + dist / 100, 3)
            n = min(q, float(x["size"]))
            if not buys_yes and p_yes < bid - 1e-9:
                inv += n
                cash -= n * bid
            elif buys_yes and p_yes > ask + 1e-9:
                inv -= n
                cash += n * ask
            else:
                continue
            fills += 1
            log.append((ts, "buyYES" if not buys_yes else "sellYES", round(p_yes, 3), n))
            pause = (t1 - ts) if fills >= max_fills else min(cool, t1 - ts)
            paused_until = ts + pause
            paused_s += max(0, pause)
            f = min(max(p_yes, 0.02), 0.98)                        # re-centre on the trade
        mark = r.fair2 if np.isfinite(r.fair2) else f
        fill_pnl = cash + inv * mark
        up = max(0.0, 1 - paused_s / (t1 - t0))
        rew = r.rew_est * up
        rows.append({"question": r.question[:70], "rate": r.rate, "size": q, "cap": r.cap2, "fair1": r.fair,
                     "fair2": mark, "trades": len(tr), "fills": fills, "fill_pnl": fill_pnl, "uptime": up,
                     "rew": rew, "net": rew + fill_pnl, "comp1": r.comp_Q, "comp2": b.comp_Q.get(r.slug, np.nan)})
    o = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(o.sort_values("net").round(2).to_string(index=False))
    print(f"\nTOTAL over {(t1 - t0) / 3600:.1f} h: markets {len(o)}, capital ${o.cap.sum():.0f}, trades seen {o.trades.sum()}, "
          f"fills {o.fills.sum()}, fill PnL ${o.fill_pnl.sum():.2f}, rewards (est.) ${o.rew.sum():.2f}, "
          f"net ${o.net.sum():.2f} ({o.net.sum() / max(o.cap.sum(), 1):.1%} of capital)")
    print(f"markets with net < 0: {(o.net < 0).sum()}, worst ${o.net.min():.2f}; competitors' score day1->day2 median ratio "
          f"{(o.comp2 / o.comp1.replace(0, np.nan)).median():.2f}, empty books on day 1 that got competitors by day 2: "
          f"{((o.comp1 == 0) & (o.comp2 > 0)).sum()} of {(o.comp1 == 0).sum()}")


if __name__ == "__main__":
    main()
