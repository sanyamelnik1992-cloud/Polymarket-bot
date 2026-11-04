"""Scan active Polymarket events: 24h volume by category/tag, fee schedules, liquidity rewards.

usage: python scripts/sector_scan.py OUT_DIR
"""
import json
import os
import sys
from collections import Counter, defaultdict

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.config import GAMMA  # noqa: E402
from pmbot.http import get_json  # noqa: E402

pd.set_option("display.width", 220)
pd.set_option("display.max_rows", 200)


def fetch_active(max_events=2500):
    out, offset = [], 0
    while offset < max_events:   # Gamma rejects offset > 2000 (HTTP 422)
        try:
            d = get_json(f"{GAMMA}/events", {"closed": "false", "active": "true", "limit": 500, "offset": offset,
                                             "order": "volume24hr", "ascending": "false"}, timeout=90)
        except Exception:  # noqa: BLE001
            break
        if not d:
            break
        out += d
        if (d[-1].get("volume24hr") or 0) < 100:
            break
        offset += 500
    return out


def main(out):
    os.makedirs(out, exist_ok=True)
    evs = fetch_active()
    json.dump(evs, open(f"{out}/events_active.json", "w"))
    rows = []
    for e in evs:
        tags = [t.get("slug") for t in (e.get("tags") or [])]
        ms = e.get("markets") or []
        fee = Counter((m.get("feeSchedule") or {}).get("rate") for m in ms)
        rows.append({"slug": e["slug"], "title": e.get("title", "")[:70], "vol24": e.get("volume24hr") or 0,
                     "liq": float(e.get("liquidity") or 0), "n_markets": len(ms), "tags": tags,
                     "series": e.get("seriesSlug") or "", "fee_rate": fee.most_common(1)[0][0] if fee else None,
                     "neg_risk": e.get("negRisk"), "end": e.get("endDate")})
    df = pd.DataFrame(rows)
    df.to_parquet(f"{out}/events_active.parquet")
    print(f"active events fetched: {len(df)}, total 24h volume ${df.vol24.sum():,.0f}")
    # category = first matching top-level tag
    top = ["crypto", "sports", "politics", "geopolitics", "finance", "economy", "weather", "culture", "tech",
           "mentions", "elections", "esports", "pop-culture", "science", "business", "world"]
    def cat(tags):
        for t in top:
            if t in tags:
                return t
        return tags[0] if tags else "?"
    df["cat"] = df.tags.apply(cat)
    g = df.groupby("cat").agg(events=("slug", "size"), vol24=("vol24", "sum"), liq=("liq", "sum"),
                              fee=("fee_rate", lambda x: x.mode().iloc[0] if x.notna().any() else None))
    print("\nby category:")
    print((g.sort_values("vol24", ascending=False)).round(0).to_string())
    print("\ntop 40 events by 24h volume:")
    print(df.sort_values("vol24", ascending=False).head(40)[["vol24", "cat", "series", "n_markets", "fee_rate", "title"]]
          .round(0).to_string())
    # recurring series
    s = df[df.series != ""].groupby("series").agg(events=("slug", "size"), vol24=("vol24", "sum"),
                                                  fee=("fee_rate", "first")).sort_values("vol24", ascending=False)
    print("\ntop recurring series by 24h volume:")
    print(s.head(40).round(0).to_string())
    tagvol = defaultdict(float)
    for _, r in df.iterrows():
        for t in r.tags:
            tagvol[t] += r.vol24
    print("\ntop tags:", sorted(((k, round(v)) for k, v in tagvol.items()), key=lambda x: -x[1])[:40])


if __name__ == "__main__":
    main(sys.argv[1])
