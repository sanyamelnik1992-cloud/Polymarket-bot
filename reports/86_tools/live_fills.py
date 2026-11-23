"""reports/86: the live interceptor's real fills from the exchange's public trades (fetch.py -> trades.json in the cwd).

Our wallet is never written to the repo: it comes from PMBOT_WALLET, or from one known fill given as SLUG SIZE (the
proxy wallet of the taker BUY of that size in that window). For every fill in the fair zone (window second < 238): the
window second, the side, the price, the outcome, and the average price of the same side bought by others in the next
20 s (did the market move against us right after the fill?). Summary: wins against the wins the paid prices implied.

    python3 fetch.py <first window start> <last window start>      # unix seconds, 5m windows of BTC/ETH/SOL/XRP
    python3 live_fills.py [SLUG SIZE]
"""
import collections
import json
import os
import sys

FEE = lambda p: 0.07 * p * (1 - p)                     # noqa: E731  taker fee per share of the crypto Up/Down markets


def main():
    W = json.load(open("trades.json"))
    me = os.environ.get("PMBOT_WALLET", "").lower() or None
    if len(sys.argv) == 3:
        slug, size = sys.argv[1], float(sys.argv[2])
        me = next(t[5] for t in W[slug]["trades"] if t[1] == "BUY" and abs(t[3] - size) < 0.002)
    if not me:
        sys.exit("set PMBOT_WALLET or give SLUG SIZE of one of our fills")
    rows = []
    for slug, w in W.items():
        if not w.get("winner"):
            continue
        for t in w["trades"]:
            if t[5].lower() != me or t[1] != "BUY" or t[0] - w["start"] >= 238:
                continue
            after = [x for x in w["trades"] if x[5].lower() != me and 0 < x[0] - t[0] <= 20 and x[1] == "BUY" and x[2] == t[2]]
            pa = sum(x[4] * x[3] for x in after) / sum(x[3] for x in after) if after else None
            rows.append((t[0], slug.split("-")[0], t[0] - w["start"], t[2], t[3], t[4], t[2] == w["winner"], pa))
    rows.sort()
    B = collections.defaultdict(lambda: [0, 0, 0.0, 0.0, 0, 0, 0.0])
    for ts, a, rel, out, sz, px, won, pa in rows:
        for k in ("all", "window second < 200" if rel < 200 else "window second >= 200"):
            b = B[k]
            b[0] += 1
            b[1] += won
            b[2] += sz * (px + FEE(px))
            b[3] += sz * (1.0 if won else 0.0) - sz * (px + FEE(px))
            b[6] += px
            if pa is not None:
                b[4] += 1
                b[5] += pa < px
    print(f"fills in the fair zone: {len(rows)}")
    for k in ("all", "window second < 200", "window second >= 200"):
        if k in B:
            n, won, cost, pnl, na, down, exp = B[k]
            print(f"  {k:22s}: {n:3d} fills, won {won:3d} (the prices implied {exp:5.1f}), cost ${cost:7.2f}, pnl ${pnl:+7.2f}; "
                  f"others paid less for our side in the next 20 s after {down} of {na}")


if __name__ == "__main__":
    main()
