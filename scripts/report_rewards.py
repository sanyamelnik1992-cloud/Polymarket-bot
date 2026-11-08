"""Summary of the liquidity-rewards farmer journal.

usage: python scripts/report_rewards.py [JOURNAL_ROOT_OR_RUN_DIR=data/journal-rewards]
Rewards are an ESTIMATE (our share of each pool against the live books); fills are simulated on real trades.
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


def main(path):
    rew = defaultdict(float)
    fills = []
    last_status, last_select, first_t, last_t = None, None, None, None
    samples = defaultdict(lambda: [0, 0])
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
        elif k == "rstatus":
            last_status = e
        elif k == "select":
            last_select = e
    if first_t is None:
        print("no events")
        return
    hours = (last_t - first_t) / 3600
    print(f"journal {path}: {hours:.1f} h of events")
    if last_status:
        s = last_status
        print(f"now: markets {s['markets']}, quoting {s['quoting']}, bids ${s['bids_usd']}, pool rate at current shares "
              f"${s['rate_now']}/day")
        print(f"rewards (estimate): today ${s['rewards_today']}, total ${s['rewards_total']}")
        print(f"inventory: value ${s['inv_value']} cost ${s['inv_cost']} -> mark-to-market ${s['mtm_pnl']} "
              f"(today {s['mtm_pnl_today']})")
        print(f"TOTAL (rewards estimate + mark-to-market): ${s['total_pnl']}; fills total {s['fills_total']}")
        if s.get("top"):
            print("\ntop markets by estimated reward:")
            for m in s["top"]:
                print(f"  ${m['reward']:7.2f}  share {m['share']:.2f}  pool ${m['rate']:.0f}/d  "
                      f"{'empty ' if m['empty'] else ''}{m['slug']}")
    print("\nrewards by day (from samples):")
    for d in sorted(rew):
        print(f"  {d}: ${rew[d]:.2f}  samples {samples[d][0]}, avg markets scoring {samples[d][1] / max(samples[d][0], 1):.1f}")
    if last_select:
        print(f"\nlast selection: {last_select['picked']} markets of {last_select['priced']} priced, capital "
              f"${last_select['capital']}, est ${last_select['est_reward_day']}/day, empty books {last_select['empty']}")
    print(f"\nfills: {len(fills)}")
    for f in fills[-15:]:
        print(f"  {datetime.fromtimestamp(f['t'], timezone.utc):%m-%d %H:%M} {f['key'][:55]:55s} "
              f"{'YES' if f['side'] == 'Y' else 'NO '} {f['size']:.0f}@{f['price']:.3f} last_trade={f.get('last_trade')}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/journal-rewards")
