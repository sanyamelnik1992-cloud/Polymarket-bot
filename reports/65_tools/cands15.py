"""15-minute lock candidates: the production state model (pmbot.bot.lock.decide_state with the config's filters, sigma floor,
floor_free_p and shock pause) on the recorder's Chainlink + Binance of 7-11 Nov, every 15m window whose closing minute is inside
a recorder period, at each second s in [240, 296] of the closing 5 minutes (anchor Tc = end - 300, like the bot). Fills: the taker
buys of the model's side on the 15m tape (dl_15m.py) in [s+1, s+2] (what a FAK sent at s would have met), at most `limit`.

usage: python reports/65_tools/cands15.py DATA_DIR TAPES_DIR OUT.json [config/lock15_tier.toml]"""
import bisect
import json
import os
import sys

import numpy as np

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO)
from pmbot.bot.lock import bn_shock_bp, decide_state, load_lock_config, tier_limit  # noqa: E402
from pmbot.config import BINANCE_SYMBOL, CHAINLINK_SYMBOL  # noqa: E402
from pmbot.lockmodel import ClSigma, Tables, strike_from_prints  # noqa: E402

PER = os.environ.get("PERIODS", "r1107,r1108,r1109,d1109,d1110,d1110b,d1110c,d1111,d1111b").split(",")


def jl(path):
    with open(path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                pass


def main(data, tapes, out, cfg_path):
    cfg = load_lock_config(os.path.join(REPO, cfg_path))
    c = cfg.lock
    tables = Tables(os.path.join(REPO, c.tables))
    tape = {}
    for a in c.assets:
        for line in open(os.path.join(tapes, a + ".jsonl")):
            w = json.loads(line)
            if w.get("winner") in ("Up", "Down"):
                tape[(a, w["ws"])] = w
    res, nwin = [], 0
    for per in PER:
        cl_all = list(jl(os.path.join(data, per + "_chainlink.jsonl")))
        bn_all = list(jl(os.path.join(data, per + "_binance.jsonl")))
        for a in c.assets:
            prints = sorted(((e["rx"], e["ts"] // 1000, e["ts"], e["v"]) for e in cl_all if e["sym"] == CHAINLINK_SYMBOL[a]))
            bns = sorted(((e["rx"], (e["bid"] + e["ask"]) / 2) for e in bn_all if e["sym"] == BINANCE_SYMBOL[a]))
            if not prints or not bns:
                continue
            b_rx = [x[0] for x in bns]
            b_mid = [x[1] for x in bns]

            def bn_at(t):
                i = bisect.bisect_right(b_rx, t) - 1
                return b_mid[i] if i >= 0 else None
            t_lo, t_hi = prints[0][0] / 1000, prints[-1][0] / 1000
            cl_by_sec, sig, last, pi = {}, ClSigma(), None, 0
            for ws in range(int(t_lo) - int(t_lo) % 900 + 900, int(t_hi) - 900, 900):
                w = tape.get((a, ws))
                if w is None:
                    continue
                Tc = ws + 600
                ex = {}
                for (t, wal, side, o, size, p, tk, tx) in w["trades"]:
                    if not tk:
                        continue
                    o2, p2 = (o, p) if side == "BUY" else ("Down" if o == "Up" else "Up", 1 - p)
                    ex.setdefault(("U" if o2 == "Up" else "D", t - Tc), []).append((p2, size))
                K = None
                done = False
                nwin += 1
                for s in range(240, 297):
                    t = (Tc + s) * 1000 + 500
                    while pi < len(prints) and prints[pi][0] <= t:
                        rx, sec, ts, v = prints[pi]
                        cl_by_sec.setdefault(sec, v)
                        if last is None or ts >= last[1]:
                            last = (v, ts, rx)
                        if sig.last_sec is None:
                            sig.on_print(sec, v)
                        else:
                            for u in range(sig.last_sec + 1, sec + 1):
                                sig.on_print(u, cl_by_sec.get(u, sig.last_v))
                        pi += 1
                    if K is None:
                        K = strike_from_prints(cl_by_sec, ws)
                    if done or K is None:
                        continue
                    side, p, ctx = decide_state(c, tables, a, Tc, K, t, cl_by_sec, last,
                                                sig.sigma() if sig.n >= c.min_sigma_prints else None, bn_at(t),
                                                bn_at(last[1] + 999) if last else None, bn_at(t - 1000), bn_at(t - 3000),
                                                shock=lambda t=t: bn_shock_bp(bn_at, t, c.shock_s))
                    if side is None:
                        continue
                    lim = tier_limit(c, p, s) if c.tiers else min(c.a_max, p - c.min_edge)
                    if lim is None:
                        continue
                    fills = [f for u in (s + 1, s + 2) for f in ex.get((side, u), []) if c.a_min - 1e-9 <= f[0] <= lim + 1e-9]
                    if sum(q for _, q in fills) < c.shares - 1e-9:
                        continue
                    got = cost = 0.0
                    for pp, q in fills:
                        x = min(q, c.shares - got)
                        got, cost = got + x, cost + x * pp
                        if got >= c.shares - 1e-9:
                            break
                    res.append({"a": a, "ws": ws, "s": s, "side": side, "p": round(p, 5), "limit": lim, "px": round(cost / got, 4),
                                "won": (side == "U") == (w["winner"] == "Up"), "period": per, "sigma": ctx.get("sigma_bp"),
                                "dist": ctx.get("dist_bp")})
                    done = True
                # keep the prints flowing to the next window (the loop above stops at Tc + 296)
            print(per, a, "entries so far", len(res), flush=True)
    json.dump({"windows": nwin, "entries": res}, open(out, "w"))
    print("windows", nwin, "entries", len(res))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "config/lock15_tier.toml")
