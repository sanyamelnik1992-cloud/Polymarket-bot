"""reports/97: the 15m Ступени rule replayed on public data, with "spike and fade" features of every entry.

  python swing15.py TAPE_DIR BN.npz OUT.json [config/lock15_tier.toml]

TAPE_DIR from reports/95_tools/fetch_tape.py (15m windows: windows.json with the Gamma winners, trades.json with the taker rows),
BN.npz from reports/95_tools/bn_fetch.py (Binance 1 s closes of BTCUSDT ETHUSDT SOLUSDT XRPUSDT, a day before the first window).

The decision is the production code (pmbot.bot.lock.decide_state with the config's tiers, sigma floor, floor_free_p, shock pause and
guards), at each second s in [240, 296] of the closing 5 minutes like the bot. Binance 1 s closes stand in for the Chainlink prints
(no Chainlink history here): the strike is the mean of [T-62, T-3], the print of second x is known at x + 1.2 s, the sigma is the
production ClSigma (EWMA of 1 s squared returns, half-life 900 s). The OUTCOME is the exchange's (Gamma), so the Binance / Chainlink
difference is part of the noise, as it is for the bot. A FAK sent at s meets the asks the tape shows in seconds s+1 and s+2: taker BUYs of
our side at <= the tier's limit, and taker SELLs of the other side at q (a bid there = an ask of ours at 1 - q). One entry a window.

Features of each entry (Binance, the seconds before the decision): lead_bp (spot vs strike, our side +), min_lead_60 / _120 (the lowest
lead of the last 60 / 120 s: < 0 = the price was on the OTHER side of the strike then), swing_60 / _120 (max - min, bp), run_60 / _120
(the move toward our side over 60 / 120 s), dist_bp / sigma_bp / p from the model."""
import json
import os
import sys

import numpy as np

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO)
from pmbot.bot.lock import bn_shock_bp, decide_state, load_lock_config, tier_limit  # noqa: E402
from pmbot.lockmodel import Tables  # noqa: E402

SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
SHARES = 5


def ewma_sigma(px, halflife=900.0):
    """ClSigma on a 1 s series: EWMA of squared log returns (forward-filled seconds = zero returns)."""
    alpha = 1 - 0.5 ** (1 / halflife)
    r = np.zeros(len(px))
    r[1:] = np.log(px[1:] / px[:-1])
    r = np.nan_to_num(r)
    out = np.full(len(px), np.nan)
    num = den = 0.0
    for i in range(1, len(px)):
        num = num * (1 - alpha) + r[i] * r[i]
        den = den * (1 - alpha) + 1.0
        if i >= 120:
            out[i] = (num / den) ** 0.5
    return out


def main(tape, bn, out, cfg_path):
    cfg = load_lock_config(os.path.join(REPO, cfg_path))
    c = cfg.lock
    tables = Tables(os.path.join(REPO, c.tables))
    B = np.load(bn)
    T0 = int(B["t0"])
    W = [w for w in json.load(open(os.path.join(tape, "windows.json"))) if w.get("win") in ("Up", "Down") and w["end"] - w["start"] == 900]
    TR = json.load(open(os.path.join(tape, "trades.json")))
    res, nwin = [], 0
    for a in c.assets:
        if SYM.get(a) not in B.files:
            continue
        px = B[SYM[a]].astype(float)
        ok = np.isfinite(px)
        idx = np.where(ok, np.arange(len(px)), 0)
        np.maximum.accumulate(idx, out=idx)
        px = px[idx]                                    # forward-filled
        sig = ewma_sigma(px)

        def at(sec):
            i = int(sec) - T0
            return float(px[i]) if 0 <= i < len(px) else None

        def bn_at(ms):
            return at(ms // 1000 - 1)                     # the last complete second at time ms (conservative)
        for w in sorted((w for w in W if w["asset"] == a), key=lambda w: w["start"]):
            ws, win = w["start"], w["win"]
            Tc = ws + 600
            if ws - 62 - T0 < 4000 or Tc + 300 - T0 >= len(px):
                continue
            nwin += 1
            K = float(np.mean(px[ws - 62 - T0: ws - 2 - T0]))
            asks = {}
            for ts, sd, o, n, p, _wal in TR.get(w["slug"], []):
                if not (Tc + 240 <= ts <= Tc + 299):
                    continue
                o2, p2 = (o, p) if sd == "B" else ("Down" if o == "Up" else "Up", 1.0 - p)
                asks.setdefault(("U" if o2 == "Up" else "D", ts - Tc), []).append((p2, n))
            for s in range(240, 297):
                t = (Tc + s) * 1000 + 500
                last_sec = Tc + s - 1
                cl_by_sec = {x: at(x) for x in range(Tc + 238, last_sec + 1)}
                cl_last = (at(last_sec), last_sec * 1000, last_sec * 1000 + 1200)
                sg = sig[last_sec - T0]
                side, p, ctx = decide_state(c, tables, a, Tc, K, t, cl_by_sec, cl_last, sg if np.isfinite(sg) else None,
                                            bn_at(t), bn_at(last_sec * 1000 + 999), bn_at(t - 1000), bn_at(t - 3000),
                                            shock=lambda t=t: bn_shock_bp(bn_at, t, c.shock_s))
                if side is None:
                    continue
                lim = tier_limit(c, p, s)
                if lim is None:
                    continue
                fills = [f for u in (s + 1, s + 2) for f in asks.get((side, u), []) if c.a_min - 1e-9 <= f[0] <= lim + 1e-9]
                if sum(q for _, q in fills) < SHARES - 1e-9:
                    continue
                got = cost = 0.0
                for pp, q in fills:
                    x = min(q, SHARES - got)
                    got, cost = got + x, cost + x * pp
                    if got >= SHARES - 1e-9:
                        break
                sgn = 1.0 if side == "U" else -1.0
                i1 = last_sec - T0
                seg60, seg120 = px[i1 - 60: i1 + 1], px[i1 - 120: i1 + 1]
                lead = sgn * (seg120 / K - 1) * 1e4
                res.append({"a": a, "ws": ws, "s": s, "side": side, "p": round(p, 5), "limit": lim, "px": round(cost / got, 4),
                            "won": (side == "U") == (win == "Up"), "day": (ws - T0) // 86400,
                            "sigma_bp": ctx.get("sigma_bp"), "dist_bp": ctx.get("dist_bp"),
                            "lead_bp": round(float(lead[-1]), 3), "min_lead_60": round(float(lead[-61:].min()), 3),
                            "min_lead_120": round(float(lead.min()), 3),
                            "swing_60": round(float((seg60.max() - seg60.min()) / K * 1e4), 3),
                            "swing_120": round(float((seg120.max() - seg120.min()) / K * 1e4), 3),
                            "run_60": round(float(sgn * (seg60[-1] / seg60[0] - 1) * 1e4), 3),
                            "run_120": round(float(sgn * (seg120[-1] / seg120[0] - 1) * 1e4), 3)})
                break
        print(a, "windows so far", nwin, "entries", len(res), flush=True)
    json.dump({"windows": nwin, "entries": res, "config": cfg_path}, open(out, "w"))
    print("windows", nwin, "entries", len(res))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "config/lock15_tier.toml")
