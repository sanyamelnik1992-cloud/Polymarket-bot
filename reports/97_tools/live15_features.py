"""reports/97: our REAL 15m entries (the taker buys of our wallet on the public tape) with the same "spike and fade" features as
swing15.py, to see what a filter would have done to the live trades.

  python live15_features.py TAPE_DIR BN.npz WALLET_FILE [--from 2026-11-17]

WALLET_FILE holds our proxy wallet (from PMBOT_WALLET or found from our own fills, reports/96_tools/live_fills.py): it is read, never
printed or written. Entries in the last minute (window second >= 840) at a price <= 0.98 are the Ступени (pmbot-lock-15-live); the early
sniper buys at 0.985-0.99 three to five minutes before the end and is listed apart."""
import calendar
import json
import sys

import numpy as np

SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}


def main(tape, bn, wallet_file, since="2026-11-17"):
    me = open(wallet_file).read().strip().lower()
    y, m, d = map(int, since.split("-"))
    t_from = calendar.timegm((y, m, d, 0, 0, 0))
    B = np.load(bn)
    T0 = int(B["t0"])
    PX = {}
    for a, s in SYM.items():
        if s in B.files:
            px = B[s].astype(float)
            idx = np.where(np.isfinite(px), np.arange(len(px)), 0)
            np.maximum.accumulate(idx, out=idx)
            PX[a] = px[idx]
    W = {w["slug"]: w for w in json.load(open(f"{tape}/windows.json")) if w.get("win") in ("Up", "Down") and w["end"] - w["start"] == 900}
    TR = json.load(open(f"{tape}/trades.json"))
    rows = []
    for slug, trades in TR.items():
        w = W.get(slug)
        if not w or w["start"] < t_from or w["asset"] not in PX:
            continue
        buys = [t for t in trades if t[5].lower() == me and t[1] == "B"]
        if not buys:
            continue
        px, ws = PX[w["asset"]], w["start"]
        K = float(np.mean(px[ws - 62 - T0: ws - 2 - T0]))
        by_side = {}
        for ts, _sd, o, n, p, _w in buys:
            by_side.setdefault((o, ts - ws >= 840 and p <= 0.981), []).append((ts, n, p))
        for (o, step), xs in by_side.items():
            ts0 = min(x[0] for x in xs)
            sh = sum(x[1] for x in xs)
            pr = sum(x[1] * x[2] for x in xs) / sh
            sgn = 1.0 if o == "Up" else -1.0
            i1 = ts0 - 1 - T0
            seg = px[i1 - 120: i1 + 1]
            lead = sgn * (seg / K - 1) * 1e4
            rows.append({"bot": "Ступени" if step else "ранний", "a": w["asset"], "ws": ws, "sec": ts0 - ws, "side": o, "shares": round(sh, 2),
                         "px": round(pr, 4), "won": o == w["win"], "lead_bp": round(float(lead[-1]), 2),
                         "min_lead_60": round(float(lead[-61:].min()), 2), "min_lead_120": round(float(lead.min()), 2),
                         "run_120": round(float(lead[-1] - lead[0]), 2), "swing_120": round(float((seg.max() - seg.min()) / K * 1e4), 2)})
    rows.sort(key=lambda r: (r["bot"], r["ws"]))
    for bot in ("Ступени", "ранний"):
        rs = [r for r in rows if r["bot"] == bot]
        lost = [r for r in rs if not r["won"]]
        print(f"{bot}: {len(rs)} entries, {len(lost)} lost")
        for r in lost:
            print("   LOST", json.dumps(r, ensure_ascii=False))
        for label, f in (("crossed in 60 s", lambda r: r["min_lead_60"] < 0), ("crossed in 120 s", lambda r: r["min_lead_120"] < 0),
                         ("120 s move > lead", lambda r: r["run_120"] > max(r["lead_bp"], 0.1)),
                         ("crossed in 120 s and 120 s move > lead", lambda r: r["min_lead_120"] < 0 and r["run_120"] > max(r["lead_bp"], 0.1))):
            rem = [r for r in rs if f(r)]
            print(f"   filter '{label}': removes {len(rem)} entries, of them lost {sum(not r['won'] for r in rem)}; "
                  f"money of the removed at their own size ${sum((1 - r['px']) * r['shares'] if r['won'] else -r['px'] * r['shares'] for r in rem):+.2f}")
    json.dump(rows, open("live15_rows.json", "w"))


if __name__ == "__main__":
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    since = next((sys.argv[i + 1] for i, x in enumerate(sys.argv) if x == "--from"), "2026-11-17")
    main(args[0], args[1], args[2], since)
