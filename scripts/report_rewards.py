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
    shared = defaultdict(float)              # rewards where others held >= half the pool (samples since 15 Nov)
    has_shared = set()
    samples = defaultdict(lambda: [0, 0])
    fills, settles, hedges, days, merges, gone = [], [], [], [], [], []
    live = []                            # events of the live test (pmbot/rewards/live.py), real money
    per_market = {}                      # run start -> the last per-market table of that run
    starts = []

    def run_of(e):
        return starts[-1] if starts else 0
    last_status, last_select, first_t, last_t = None, None, None, None
    for e in events(path):
        first_t = first_t or e["t"]
        last_t = e["t"]
        k = e["type"]
        if k == "start":
            starts.append(e["t"])
        if k == "rsample":
            rew[day(e["t"])] += e.get("inc", 0)
            off[day(e["t"])] += e.get("official", 0)
            alone[day(e["t"])] += e.get("alone", 0)
            if "shared" in e:
                shared[day(e["t"])] += e["shared"]
                has_shared.add(day(e["t"]))
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
        elif k == "rmarkets":
            per_market[run_of(e)] = e.get("markets") or []
        elif k == "rmk":                 # a market the bot stopped following: its totals, once
            gone.append(e)
        elif k == "rmerge":
            merges.append(e)
        if k.startswith("rlive") or k in ("order_placed", "order_rejected", "order_cancelled", "fill", "kill", "balance"):
            live.append(e)
        elif k == "rstatus":
            last_status = e
        elif k == "select":
            last_select = e
    if first_t is None:
        print("no events")
        return
    print(f"journal {path}: {(last_t - first_t) / 3600:.1f} h of events, {fmt(first_t)} -> {fmt(last_t)} UTC")
    if any(e["type"].startswith("rlive") for e in live):
        live_section(live)
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
        if "honest_pnl" in s:
            print(f"HONEST (rewards only where others held >= half the pool, counted since 15 Nov, + all trading): "
                  f"${s['honest_pnl']} (today ${s.get('honest_today')}; such rewards: today ${s.get('shared_today')}, "
                  f"total ${s.get('shared_total')})")
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
        hon = (f", honest net ${shared[d] + c['trading_pnl_day']:+.2f}" if c and d in has_shared else "")
        net += hon
        sh = f", others >= half ${shared[d]:.2f}" if d in has_shared else ""
        print(f"  {d}: book ${rew[d]:.2f}, official ${off[d]:.2f}, alone ${alone[d]:.2f}{sh} | hedges {hd[d][0]} "
              f"(${hd[d][1]:.2f}) | {tr}{net} | samples {samples[d][0]}, avg markets scoring "
              f"{samples[d][1] / max(samples[d][0], 1):.1f}")
    if last_select:
        print(f"\nlast selection: {last_select['picked']} markets of {last_select['priced']} priced, capital "
              f"${last_select['capital']}, est ${last_select['est_reward_day']}/day, empty books {last_select['empty']}")
    print(f"\nfills: {len(fills)} (material {sum(1 for f in fills if f.get('material'))})")
    for f in fills[-15:]:
        print(f"  {fmt(f['t'])} {f['key'][:55]:55s} {'YES' if f['side'] == 'Y' else 'NO '} "
              f"{f['size']:g}@{f['price']:.3f} last_trade={f.get('last_trade')}{' (alone in the zone)' if f.get('alone') else ''}")
    rows = [m for tab in per_market.values() for m in tab] + gone
    if rows:
        def line(name, sel):
            if not sel:
                return
            rew = sum(m["reward"] for m in sel)
            trade = sum(m["hedge_pnl"] + m["open_pnl"] for m in sel)
            print(f"  {name:38s} {len(sel):3d} markets  rewards ${rew:8.2f}  trading ${trade:+8.2f}  net ${rew + trade:+8.2f}  "
                  f"fills {sum(m['fills'] for m in sel):3d}  quoted {sum(m['quoted_min'] for m in sel) / 60:6.1f} market-hours")
        print("\nby market (rewards = estimate by the book; trading = hedges + open positions at the mark):")
        line("all", rows)
        line("mostly alone in the zone", [m for m in rows if m["alone"] > 0.5 * m["reward"] > 0])
        line("with other makers in the zone", [m for m in rows if not m["alone"] > 0.5 * m["reward"]])
        line("never filled", [m for m in rows if m["fills"] == 0])
        line("filled", [m for m in rows if m["fills"] > 0])
        def mean_share(m):
            hrs = m.get("quoted_min", 0) / 60
            return m["reward"] / (m["rate"] * hrs / 24) if hrs > 0 and m.get("rate") else None
        print("\nRECOUNT by our mean share of the pool in each market (reward / (pool x quoted time); trading = hedges + "
              "open marks of the market, inherited positions included):")
        for name, sel in (("others held >= half (share <= 50 %)", [m for m in rows if (mean_share(m) or 0) <= 0.5
                                                                    and not m["alone"] > 0.5 * m["reward"] > 0]),
                          ("mostly ours (share > 50 %, others present)", [m for m in rows if (mean_share(m) or 0) > 0.5
                                                                         and not m["alone"] > 0.5 * m["reward"] > 0]),
                          ("alone most of the time", [m for m in rows if m["alone"] > 0.5 * m["reward"] > 0])):
            hrs = sum(m["quoted_min"] for m in sel) / 60
            rw = sum(m["reward"] for m in sel)
            tr = sum(m["hedge_pnl"] + m["open_pnl"] for m in sel)
            print(f"  {name:44s} {len(sel):4d} markets {hrs:7.1f} h  rewards ${rw:8.2f}  trading ${tr:+8.2f}  "
                  f"net ${rw + tr:+8.2f}" + (f" = ${(rw + tr) / hrs:+.2f}/h" if hrs else "")
                  + f"  fills {sum(m['fills'] for m in sel)}")
        feat = [m for m in rows if "vol" in m]                # journals since 14 Nov carry what a market looked like
        if feat:
            print("\nby kind of market at selection (journals since 14 Nov; $/h = per quoted market-hour):")

            def group(name, key, edges):
                print(f"  {name}:")
                for lo, hi in zip(edges, edges[1:]):
                    sel = [m for m in feat if m.get(key) is not None and lo <= m[key] < hi]
                    hrs = sum(m["quoted_min"] for m in sel) / 60
                    if not sel or hrs <= 0:
                        continue
                    rew = sum(m["reward"] for m in sel)
                    al = sum(m["alone"] for m in sel)
                    tr = sum(m["hedge_pnl"] + m["open_pnl"] for m in sel)
                    nf = sum(m["fills"] for m in sel)
                    label = "0" if hi < 1e-6 else f"{0 if lo < 1e-6 else lo:g}..{hi:g}" if hi < 1e4 else f"{lo:g}+"
                    print(f"    {label}".ljust(18) + f"{len(sel):4d} markets {hrs:7.1f} h  rewards ${rew:8.2f} (alone "
                          f"${al:8.2f})  trading ${tr:+8.2f}  fills {nf:3d} = {nf / hrs:.2f}/h  "
                          f"not alone ${(rew - al) / hrs:+.2f}/h  trading ${tr / hrs:+.2f}/h")
            group("competitiveness (others' score per $1 of pool)", "comp", [0, 1e-9, 0.1, 0.3, 1, 1e9])
            group("24 h volume, $", "vol", [0, 1, 50, 200, 500, 1e9])
            group("days to the end", "days", [0, 4, 8, 20, 60, 400, 1e5])
            group("price of YES", "price", [0, 0.2, 0.4, 0.6, 0.8, 1.01])
            group("pool, $ a day", "rate", [0, 20, 40, 80, 1e9])
            group("rewards min size, shares", "min_size", [0, 21, 51, 1e9])
        worst = sorted(rows, key=lambda m: m["hedge_pnl"] + m["open_pnl"])[:8]
        print("  worst by trading:")
        for m in worst:
            if m["hedge_pnl"] + m["open_pnl"] < 0:
                print(f"    ${m['hedge_pnl'] + m['open_pnl']:+7.2f}  rewards ${m['reward']:6.2f}  fills {m['fills']}  pool ${m['rate']:.0f}/d  {m['q'][:60]}")
    if hedges:
        n = sum(h.get("size", 0) for h in hedges)
        cost = sum(h.get("cost_per_share", 0) * h.get("size", 0) for h in hedges)
        print(f"\nhedges: {len(hedges)}, {n:g} shares, cost ${cost:.2f} = {100 * cost / max(n, 1e-9):.2f}c a share "
              f"(the largest reward earners pay 1.3c + fee: reports/68)")
        for h in hedges[-10:]:
            print(f"  {fmt(h['t'])} {h['key'][:55]:55s} bought {'NO ' if h['buy'] == 'N' else 'YES'} "
                  f"{h['size']:g}@{h['price']:.3f} held@{h['held']:.3f} cost/share {h['cost_per_share']:+.4f}")
    if merges:
        def mline(name, sel):
            if sel:
                n = sum(m.get("pairs", 0) for m in sel)
                pnl = sum(m.get("pnl", 0) for m in sel)
                print(f"  {name:34s} {len(sel):3d} for {n:7.1f} pairs  ${pnl:+8.2f} = {100 * pnl / max(n, 1e-9):+.1f}c a pair")
        print("\npairs closed by our own bids (no taker fee), by the time since the fill:")
        aged = [m for m in merges if m.get("age_s") is not None]
        mline("all", merges)
        mline("within 30 min", [m for m in aged if m["age_s"] < 1800])
        mline("30 min - 3 h", [m for m in aged if 1800 <= m["age_s"] < 10800])
        mline("after 3 h (the bid follows the price)", [m for m in aged if m["age_s"] >= 10800])
    print(f"\nsettlements: {len(settles)}, realised ${sum(e.get('pnl', 0) for e in settles):.2f}")
    for e in settles[-10:]:
        print(f"  {fmt(e['t'])} {e['key'][:55]:55s} yes {e['yes']:g} no {e['no']:g} cost {e['cost']:.2f} "
              f"payoff {e['payoff']:.2f} pnl {e['pnl']:+.2f}")


def live_section(live):
    """The live test (REAL MONEY): what the exchange itself reported next to the paper estimate, minute by minute."""
    print("\nLIVE TEST (real money; exch = the exchange's own numbers, paper = our estimate):")
    n_placed = sum(e["type"] == "order_placed" for e in live)
    n_rej = sum(e["type"] == "order_rejected" for e in live)
    print(f"  orders placed {n_placed}, rejected {n_rej}, fills {sum(e['type'] == 'fill' for e in live)}")
    shown = 0
    for e in live:
        k = e["type"]
        if k == "balance":
            print(f"  {fmt(e['t'])} balance ${e.get('pusd')}")
        elif k == "kill":
            print(f"  {fmt(e['t'])} STOPPED: {e.get('reason')}")
        elif k == "rlive_check":
            print(f"  {fmt(e['t'])} read-only check: percentages {'ok' if e.get('pct_ok') else 'FAILED'}, markets with a share "
                  f"{e.get('markets_with_pct')}, earnings yesterday {e.get('earnings_yesterday')}")
        elif k == "rlive_market":
            print(f"  {fmt(e['t'])} market: {e.get('question')} | pool ${e.get('rate')}/d comp {e.get('comp')} "
                  f"{'alone in the zone' if e.get('empty') else 'others in the zone'} | kind {e.get('test_kind')}, "
                  f"{e.get('size')} shares a side, ${e.get('capital')} | cid {e.get('cid')}")
        elif k == "rlive_no_market":
            print(f"  {fmt(e['t'])} no market fits (kind {e.get('test_kind')}, looked at {e.get('looked_at')})")
        elif k in ("rlive_refused", "rlive_error", "order_rejected"):
            print(f"  {fmt(e['t'])} {k}: {({x: e[x] for x in e if x not in ('t', 'type', 'mode', 'token')})}")
        elif k in ("fill", "rlive_fill"):
            print(f"  {fmt(e['t'])} {k.upper()}: {e.get('side')} {e.get('size')} @ {e.get('price')}")
        elif k == "rlive":
            shown += 1
            if shown <= 5 or shown % 10 == 0:              # the first minutes, then every tenth sample
                print(f"  {fmt(e['t'])} exch share {e.get('exch_pct', 'n/a')}%  paper share {100 * e.get('paper_share', 0):.1f}%  "
                      f"scoring {e.get('scoring', 'n/a')}  bids {e.get('bids')}  ref {e.get('ref')}  paper ${e.get('paper_reward')}"
                      + (f"  exch accrued ${e['exch_earned']}" if "exch_earned" in e else ""))
        elif k == "rlive_done":
            print(f"  {fmt(e['t'])} DONE ({e.get('why')}): quoted {e.get('quoted_min')} min, samples {e.get('samples')}; exch share "
                  f"mean {e.get('exch_pct_mean')}% last {e.get('exch_pct_last')}%; paper share mean {e.get('paper_share_mean')}; "
                  f"max {e.get('exch_pct_max')}%, halved after {e.get('squeezed_after_min')} min; scoring in "
                  f"{e.get('scoring_all')} of samples; THE EXCHANGE ACCRUED ${e.get('exch_earned')} against paper "
                  f"${e.get('paper_reward')} (by time at the exchange's share ${e.get('by_time_exch')}; pool "
                  f"${e.get('whole_pool')}/day); fills {e.get('fills')}; position "
                  f"{e.get('position')}; orders placed {e.get('orders_placed')}; left open {e.get('orders_left_open')}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/journal-rewards")
