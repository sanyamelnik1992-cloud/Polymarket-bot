"""Summary of the liquidity-rewards farmer journal.

usage: python scripts/report_rewards.py [JOURNAL_ROOT_OR_RUN_DIR=data/journal-rewards]
Rewards are an ESTIMATE (our share of each pool against the live books); "official" is the same accrual with the share
from Polymarket's competitiveness number; "alone" is the part earned while nobody else scored in the zone (the least
certain part: reports/68); "stress" is the estimate if one more identical maker joins. Fills are simulated on real
trades, hedges on the live book; settlements pay positions out at Polymarket's resolution.
"""
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone


def events(path):
    runs = [path] if os.path.exists(os.path.join(path, "events.jsonl")) else sorted(glob.glob(os.path.join(path, "*")))
    for d in runs:
        f = os.path.join(d, "events.jsonl")
        if not os.path.exists(f):
            continue
        for line in open(f):
            try:
                yield json.loads(line)
            except ValueError:
                continue


def day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def fmt(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M")


def main(path):
    rew = defaultdict(float)
    off = defaultdict(float)
    alone = defaultdict(float)
    samples = defaultdict(lambda: [0, 0])
    fills, settles, hedges, days = [], [], [], []
    last_status, last_select, first_t, last_t = None, None, None, None
    for e in events(path):
        first_t = first_t or e["t"]
        last_t = e["t"]
        k = e["type"]
        if k == "rsample":
            rew[day(e["t"])] += e.get("inc", 0)
            off[day(e["t"])] += e.get("official", 0)
            alone[day(e["t"])] += e.get("alone", 0)
            samples[day(e["t"])][0] += 1
            samples[day(e["t"])][1] += e.get("scoring", 0)
        elif k == "rfill":
            fills.append(e)
        elif k == "rsettle":
            settles.append(e)
        elif k == "rhedge":
            hedges.append(e)
        elif k == "rday":
            days.append(e)
        elif k == "rstatus":
            last_status = e
        elif k == "select":
            last_select = e
    if first_t is None:
        print("no events")
        return
    print(f"journal {path}: {(last_t - first_t) / 3600:.1f} h of events, {fmt(first_t)} -> {fmt(last_t)} UTC")
    if last_status:
        s = last_status
        print(f"now: markets {s['markets']}, quoting {s['quoting']}, bids ${s['bids_usd']}, feed "
              f"{'ok' if s.get('feed') else 'SILENT'}, pool rate at current shares ${s['rate_now']}/day")
        print(f"rewards (estimate by the book): today ${s['rewards_today']}, total ${s['rewards_total']}; "
              f"by Polymarket's competitiveness: total ${s.get('official_total', 'n/a')}; "
              f"earned alone in a zone: total ${s.get('alone_total', 'n/a')}; "
              f"stress (one more identical maker): total ${s.get('stress_total', 'n/a')}")
        print(f"hedges: {s.get('hedges_total', 0)} for {s.get('hedge_shares_total', 0)} shares, lost "
              f"${s.get('hedge_loss_total', 0)}; inventory not hedged yet: ${s.get('unhedged_usd', 0)}")
        open_pnl = s.get("open_pnl", s.get("mtm_pnl"))              # journals before the trading-PnL change
        trading = s.get("trading_pnl", open_pnl)
        print(f"positions: value ${s['inv_value']} cost ${s['inv_cost']} -> open ${open_pnl}; "
              f"realised ${s.get('realized', 0)}; trading ${trading} (today {s.get('trading_pnl_today', s.get('mtm_pnl_today'))})")
        print(f"TOTAL (rewards estimate + trading): ${s['total_pnl']}; CAUTIOUS (lower estimate, without the part "
              f"earned alone, + trading): ${s.get('cautious_pnl', 'n/a')}; fills total {s['fills_total']}")
        if s.get("top"):
            print("\ntop markets by estimated reward:")
            for m in s["top"]:
                print(f"  ${m['reward']:7.2f}  share {m['share']:.2f}  pool ${m['rate']:.0f}/d  "
                      f"{'empty ' if m['empty'] else ''}{m['slug']}")
    print("\nby day (rewards from samples; trading = hedges, settlements and marks of the day, from the day close):")
    close = {e["day"]: e for e in days}
    hd = defaultdict(lambda: [0, 0.0])
    for h in hedges:
        hd[day(h["t"])][0] += 1
        hd[day(h["t"])][1] += h.get("cost_per_share", 0) * h.get("size", 0)
    for d in sorted(rew):
        c = close.get(d)
        tr = f"trading ${c['trading_pnl_day']:+.2f}" if c else "trading: day not closed"
        low = min(rew[d], off[d]) - alone[d]
        net = f", cautious net ${max(low, 0) + c['trading_pnl_day']:+.2f}" if c else ""
        print(f"  {d}: book ${rew[d]:.2f}, official ${off[d]:.2f}, alone ${alone[d]:.2f} | hedges {hd[d][0]} "
              f"(${hd[d][1]:.2f}) | {tr}{net} | samples {samples[d][0]}, avg markets scoring "
              f"{samples[d][1] / max(samples[d][0], 1):.1f}")
    if last_select:
        print(f"\nlast selection: {last_select['picked']} markets of {last_select['priced']} priced, capital "
              f"${last_select['capital']}, est ${last_select['est_reward_day']}/day, empty books {last_select['empty']}")
    print(f"\nfills: {len(fills)} (material {sum(1 for f in fills if f.get('material'))})")
    for f in fills[-15:]:
        print(f"  {fmt(f['t'])} {f['key'][:55]:55s} {'YES' if f['side'] == 'Y' else 'NO '} "
              f"{f['size']:g}@{f['price']:.3f} last_trade={f.get('last_trade')}{' (alone in the zone)' if f.get('alone') else ''}")
    if hedges:
        n = sum(h.get("size", 0) for h in hedges)
        cost = sum(h.get("cost_per_share", 0) * h.get("size", 0) for h in hedges)
        print(f"\nhedges: {len(hedges)}, {n:g} shares, cost ${cost:.2f} = {100 * cost / max(n, 1e-9):.2f}c a share "
              f"(the largest reward earners pay 1.3c + fee: reports/68)")
        for h in hedges[-10:]:
            print(f"  {fmt(h['t'])} {h['key'][:55]:55s} bought {'NO ' if h['buy'] == 'N' else 'YES'} "
                  f"{h['size']:g}@{h['price']:.3f} held@{h['held']:.3f} cost/share {h['cost_per_share']:+.4f}")
    print(f"\nsettlements: {len(settles)}, realised ${sum(e.get('pnl', 0) for e in settles):.2f}")
    for e in settles[-10:]:
        print(f"  {fmt(e['t'])} {e['key'][:55]:55s} yes {e['yes']:g} no {e['no']:g} cost {e['cost']:.2f} "
              f"payoff {e['payoff']:.2f} pnl {e['pnl']:+.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/journal-rewards")
