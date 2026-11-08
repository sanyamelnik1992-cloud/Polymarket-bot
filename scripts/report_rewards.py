"""Summary of the liquidity-rewards farmer journal.

usage: python scripts/report_rewards.py [JOURNAL_ROOT_OR_RUN_DIR=data/journal-rewards]
Rewards are an ESTIMATE (our share of each pool against the live books); "stress" is the same estimate if one more
identical maker joins; fills are simulated on real trades; settlements pay positions out at Polymarket's resolution.
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
    samples = defaultdict(lambda: [0, 0])
    fills, settles = [], []
    last_status, last_select, first_t, last_t = None, None, None, None
    for e in events(path):
        first_t = first_t or e["t"]
        last_t = e["t"]
        k = e["type"]
        if k == "rsample":
            rew[day(e["t"])] += e.get("inc", 0)
            samples[day(e["t"])][0] += 1
            samples[day(e["t"])][1] += e.get("scoring", 0)
        elif k == "rfill":
            fills.append(e)
        elif k == "rsettle":
            settles.append(e)
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
        print(f"rewards (estimate): today ${s['rewards_today']}, total ${s['rewards_total']}; "
              f"stress (one more identical maker): total ${s.get('stress_total', 'n/a')}")
        open_pnl = s.get("open_pnl", s.get("mtm_pnl"))              # journals before the trading-PnL change
        trading = s.get("trading_pnl", open_pnl)
        print(f"positions: value ${s['inv_value']} cost ${s['inv_cost']} -> open ${open_pnl}; "
              f"realised ${s.get('realized', 0)}; trading ${trading} (today {s.get('trading_pnl_today', s.get('mtm_pnl_today'))})")
        print(f"TOTAL (rewards estimate + trading): ${s['total_pnl']}; fills total {s['fills_total']}")
        if s.get("top"):
            print("\ntop markets by estimated reward:")
            for m in s["top"]:
                print(f"  ${m['reward']:7.2f}  share {m['share']:.2f}  pool ${m['rate']:.0f}/d  "
                      f"{'empty ' if m['empty'] else ''}{m['slug']}")
    print("\nrewards by day (from samples):")
    for d in sorted(rew):
        print(f"  {d}: ${rew[d]:.2f}  samples {samples[d][0]}, avg markets scoring "
              f"{samples[d][1] / max(samples[d][0], 1):.1f}")
    if last_select:
        print(f"\nlast selection: {last_select['picked']} markets of {last_select['priced']} priced, capital "
              f"${last_select['capital']}, est ${last_select['est_reward_day']}/day, empty books {last_select['empty']}")
    print(f"\nfills: {len(fills)} (material {sum(1 for f in fills if f.get('material'))})")
    for f in fills[-15:]:
        print(f"  {fmt(f['t'])} {f['key'][:55]:55s} {'YES' if f['side'] == 'Y' else 'NO '} "
              f"{f['size']:g}@{f['price']:.3f} last_trade={f.get('last_trade')}")
    print(f"\nsettlements: {len(settles)}, realised ${sum(e.get('pnl', 0) for e in settles):.2f}")
    for e in settles[-10:]:
        print(f"  {fmt(e['t'])} {e['key'][:55]:55s} yes {e['yes']:g} no {e['no']:g} cost {e['cost']:.2f} "
              f"payoff {e['payoff']:.2f} pnl {e['pnl']:+.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/journal-rewards")
