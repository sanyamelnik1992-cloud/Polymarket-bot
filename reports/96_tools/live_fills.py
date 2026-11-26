"""reports/96: the live maker's real fills from public data, each against the model at that second.

  python live_fills.py SETTLES.csv UNIT DATE BN1S.npz          (settles.csv of the server-reports branch; DATE = YYYY-MM-DD;
                                                               BN1S from reports/95_tools/bn_fetch.py, the day before included)

1. the unit's settled windows of DATE from settles.csv (key, Up and Down shares held);
2. every trade row of those windows (data-api v2 with taker_only=false: taker AND maker rows) and the winner (Gamma);
3. our wallet = the one wallet whose BUY rows match the Up / Down shares of EVERY window (kept in memory, never printed or written);
4. each of our fills: time, second of the window, side, shares, price, the model's fair value of that side (Binance 1 s close of the
   second before, strike = mean of [T-62, T-3], sigma = RMS of 10 s returns over 1 h), edge = fair - price, the Binance move of the 3 s
   before toward the side (bp), the fair value's expected 1 s move phi(z) / sqrt(tau0 + 19.5), the outcome; and the counterparty
   rows of the same transaction (side, size, price): who took our bid.
Public data only."""
import csv
import json
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from scipy.special import ndtr


def get(u, tries=5):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "research-script"}), timeout=30) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            time.sleep(1 + i)
    return {}


def window(start):
    slug = f"btc-updown-5m-{start}"
    ev = get(f"https://gamma-api.polymarket.com/events?slug={slug}")
    m = ev[0]["markets"][0]
    outs = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
    pr = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices")
    win = (outs[0] if float(pr[0]) > 0.5 else outs[1]) if m.get("closed") and pr else None
    rows, cur = [], None
    for _ in range(80):
        j = get(f"https://data-api.polymarket.com/v2/trades?condition={m['conditionId']}&limit=500&taker_only=false"
                + (f"&cursor={cur}" if cur else ""))
        d = j.get("data") or []
        rows += d
        pg = j.get("pagination") or {}
        cur = pg.get("next_cursor")
        if not d or not pg.get("has_more") or not cur:
            break
    return start, {"win": win, "rows": rows}


def main():
    settles, unit, date, bn = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    held = {}
    for r in csv.DictReader(open(settles)):
        if r["unit"] == unit and r["time_utc"].startswith(date):
            held[int(r["key"].rsplit("-", 1)[1])] = (float(r["up"]), float(r["down"]), float(r["pnl"]))
    with ThreadPoolExecutor(10) as ex:
        W = dict(ex.map(window, sorted(held)))
    me = None
    for st, w in W.items():
        agg = defaultdict(lambda: [0.0, 0.0])
        for r in w["rows"]:
            if r["side"] == "BUY":
                agg[r["proxy_wallet"].lower()][0 if r["outcome"] == "Up" else 1] += float(r["size"])
        hit = {a for a, (u, d) in agg.items() if abs(u - held[st][0]) < 0.01 and abs(d - held[st][1]) < 0.01}
        me = hit if me is None else me & hit
    if not me or len(me) != 1:
        sys.exit(f"the wallet is not unique: {len(me or [])} candidates")
    me = me.pop()
    B = np.load(bn)
    PX, T0 = B["BTCUSDT"].astype(float), int(B["t0"])
    LP = np.log(PX)
    sq = np.full(len(PX), np.nan)
    sq[10:] = (LP[10:] - LP[:-10]) ** 2
    SIG = np.full(len(PX), np.nan)
    for c in range(10):
        idx = np.arange(c, len(PX), 10)
        v = np.where(np.isnan(sq[idx]), 0.0, sq[idx])
        cs = np.cumsum(v)
        roll = cs.copy()
        roll[360:] = cs[360:] - cs[:-360]
        SIG[idx[360:]] = np.sqrt(roll[360:] / 3600.0)
    print(" time      sec side   sh   price  fair   edge   bn3s  1s-move result | took our bid")
    tot = defaultdict(float)
    for st in sorted(W):
        w = W[st]
        byhash = defaultdict(list)
        for r in w["rows"]:
            byhash[r["transaction_hash"]].append(r)
        mine = sorted((r for r in w["rows"] if r["proxy_wallet"].lower() == me), key=lambda r: int(r["timestamp"]))
        K = PX[st - 62 - T0: st - 2 - T0].mean()
        for r in mine:
            ts, up = int(r["timestamp"]), r["outcome"] == "Up"
            i = ts - T0 - 1
            tau0 = (st + 238) - ts
            z = np.log(PX[i] / K) / (SIG[i] * np.sqrt(max(tau0, 0) + 19.5))
            fair = ndtr(z) if up else 1 - ndtr(z)
            mv3 = (1 if up else -1) * 1e4 * (LP[i] - LP[i - 3])
            move1 = np.exp(-z * z / 2) / np.sqrt(2 * np.pi) / np.sqrt(max(tau0, 0) + 19.5)
            sz, px = float(r["size"]), float(r["price"])
            won = w["win"] == r["outcome"]
            tot["shares"] += sz
            tot["pnl"] += sz * ((1.0 if won else 0.0) - px)
            tot["edge"] += sz * (fair - px)
            others = [x for x in byhash[r["transaction_hash"]] if x["proxy_wallet"].lower() != me]
            took = "; ".join(f"{x['side']} {x['outcome']} {float(x['size']):.1f}@{float(x['price']):.3f}" for x in others[:2])
            print(f"{time.strftime('%H:%M:%S', time.gmtime(ts))} {ts - st:4d} {r['outcome']:5s} {sz:4.1f}  {px:.2f}  {fair:.3f} "
                  f"{100 * (fair - px):+5.1f}c {mv3:+5.2f}  {100 * move1:4.1f}c   {'won ' if won else 'lost'} | {took}")
    print(f"windows {len(W)}, shares {tot['shares']:.0f}, by outcome {100 * tot['pnl'] / tot['shares']:+.2f} c a share, "
          f"edge against the model at the fill {100 * tot['edge'] / tot['shares']:+.2f} c a share")


if __name__ == "__main__":
    main()
