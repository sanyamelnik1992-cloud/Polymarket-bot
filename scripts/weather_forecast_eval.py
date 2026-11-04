"""Evaluate weather forecast snapshots against resolved outcomes (forward test of the ensemble model).

Only events whose local observation day had NOT started at snapshot time are used (pure forecast game).
usage: python scripts/weather_forecast_eval.py SNAPSHOT_DIR [kernel_sd_c]
"""
import glob
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.fees import fee_per_share  # noqa: E402
from pmbot.gamma import fetch_event  # noqa: E402
from pmbot.weather import STATION_TZ  # noqa: E402

pd.set_option("display.width", 200)


def resolved_winner(slug):
    e = fetch_event(slug)
    if not e or not all(m.get("closed") for m in e.get("markets", [])):
        return None
    for m in e["markets"]:
        p = json.loads(m.get("outcomePrices") or "[]")
        if p and float(p[0]) == 1.0:
            return m["conditionId"]
    return None


if __name__ == "__main__":
    rows = []
    cache = {}
    for fn in sorted(glob.glob(os.path.join(sys.argv[1], "snapshot_*.json"))):
        for ev in json.load(open(fn)):
            tz = ZoneInfo(STATION_TZ[ev["station"]])
            day_start = datetime.fromisoformat(ev["date"]).replace(tzinfo=tz)
            snap = datetime.fromisoformat(ev["snapshot_utc"])
            if snap >= day_start:
                continue
            if ev["event"] not in cache:
                cache[ev["event"]] = resolved_winner(ev["event"])
            win = cache[ev["event"]]
            if win is None:
                continue
            for lg in ev["legs"]:
                if lg["bid"] is None or lg["ask"] is None:
                    continue
                rows.append({"snap": fn[-18:-5], "event": ev["event"], "lead_h": (day_start - snap).total_seconds() / 3600,
                             "bucket": lg["bucket"], "p_model": lg["p_model"], "bid": float(lg["bid"]),
                             "ask": float(lg["ask"]), "y": float(lg["condition_id"] == win)})
    df = pd.DataFrame(rows)
    if df.empty:
        print("no resolved pre-day snapshots yet")
        sys.exit(0)
    df["mid"] = (df.bid + df.ask) / 2
    eps = 1e-3
    for name, p in (("market mid", df.mid), ("ensemble model", df.p_model),
                    ("50/50 blend", 0.5 * df.mid + 0.5 * df.p_model)):
        pc = p.clip(eps, 1 - eps)
        ll = -(df.y * np.log(pc) + (1 - df.y) * np.log(1 - pc)).mean()
        print(f"{name:15s} logloss={ll:.4f} brier={((pc - df.y) ** 2).mean():.4f}")
    print(f"events {df.event.nunique()}, buckets {len(df)}, median lead {df.lead_h.median():.1f}h")
    for thr in (0.03, 0.05, 0.10):
        b = df[df.p_model - df.ask - fee_per_share(df.ask, 0.05) > thr]
        pnl = (b.y - b.ask - fee_per_share(b.ask, 0.05)).sum()
        print(f"buy YES where model - ask - fee > {thr}: n={len(b)} events={b.event.nunique()} "
              f"pnl/share-sum={pnl:+.2f} roi={pnl / max(b.ask.sum(), 1e-9):+.3f}")
        s = df[(df.bid - df.p_model - fee_per_share(1 - df.bid, 0.05)) > thr]
        pnl = ((1 - s.y) - (1 - s.bid) - fee_per_share(1 - s.bid, 0.05)).sum()
        print(f"buy NO  where bid - model - fee > {thr}: n={len(s)} events={s.event.nunique()} "
              f"pnl/share-sum={pnl:+.2f} roi={pnl / max((1 - s.bid).sum(), 1e-9):+.3f}")
