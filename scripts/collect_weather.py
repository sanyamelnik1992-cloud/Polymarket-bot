"""Collect closed 'Highest temperature in <city> on <date>' events, their trades, and METAR history (IEM).

usage: python scripts/collect_weather.py OUT_DIR DAYS [max_events]
Writes events.parquet (one row per bucket market), trades.parquet, metar.parquet
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.config import DATA, GAMMA  # noqa: E402
from pmbot.http import get_json  # noqa: E402

IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def closed_events(days, max_events):
    out, offset = [], 0
    since = datetime.now(timezone.utc) - timedelta(days=days)
    while len(out) < max_events:
        d = get_json(f"{GAMMA}/events", {"tag_slug": "highest-temperature", "closed": "true", "limit": 100,
                                         "offset": offset, "order": "endDate", "ascending": "false"}, timeout=90)
        if not d:
            break
        for e in d:
            end = datetime.fromisoformat(e["endDate"].replace("Z", "+00:00"))
            if end >= since:
                out.append(e)
        if datetime.fromisoformat(d[-1]["endDate"].replace("Z", "+00:00")) < since:
            break
        offset += 100
    return out[:max_events]


def bucket_bounds(title):
    """'16°C or below' -> (-inf, 16); '17°C' -> (17, 17); '26°C or higher' -> (26, inf); also °F."""
    m = re.search(r"(-?\d+)(?:-(-?\d+))?\s*°\s*([CF])", title)
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    unit = m.group(3)
    t = title.lower()
    if "below" in t or "lower" in t:
        return (-999, hi, unit)
    if "higher" in t or "above" in t:
        return (lo, 999, unit)
    return (lo, hi, unit)


def flatten(events):
    rows = []
    for e in events:
        city = re.sub(r"^highest-temperature-in-|-on-.*$", "", e["slug"])
        date = re.search(r"on-([a-z]+-\d+-\d{4})$", e["slug"])
        for m in e.get("markets") or []:
            st = re.search(r"ids=([A-Z0-9]{4})", m.get("resolutionSource") or "")
            try:
                toks = json.loads(m["clobTokenIds"])
                prices = json.loads(m["outcomePrices"])
            except (KeyError, ValueError, TypeError):
                continue
            b = bucket_bounds(m.get("groupItemTitle") or "")
            rows.append({"event": e["slug"], "city": city, "date": date.group(1) if date else None,
                         "station": st.group(1) if st else None, "end": m.get("endDate"),
                         "closed_time": m.get("closedTime"), "condition_id": m["conditionId"],
                         "bucket": m.get("groupItemTitle"), "lo": b[0] if b else None, "hi": b[1] if b else None,
                         "unit": b[2] if b else None, "token_yes": toks[0],
                         "won": float(prices[0]) if prices else None, "volume": float(m.get("volume") or 0),
                         "fee_rate": (m.get("feeSchedule") or {}).get("rate")})
    return pd.DataFrame(rows)


def trades_for(cond):
    out, cur = [], None
    while True:
        p = {"condition": cond, "limit": 1000}
        if cur:
            p["cursor"] = cur
        d = get_json(f"{DATA}/trades", p)
        out += d["data"]
        pg = d.get("pagination") or {}
        if not pg.get("has_more") or not pg.get("next_cursor"):
            return out
        cur = pg["next_cursor"]


def metar(station, start, end):
    params = {"station": station, "data": "metar", "year1": start.year, "month1": start.month, "day1": start.day,
              "year2": end.year, "month2": end.month, "day2": end.day, "tz": "Etc/UTC", "format": "onlycomma",
              "latlon": "no", "elev": "no", "missing": "M", "trace": "T", "direct": "no",
              "report_type": [3, 4]}
    for i in range(4):
        try:
            r = requests.get(IEM, params=params, timeout=60)
            if r.status_code == 200:
                lines = r.text.strip().splitlines()[1:]
                return [ln.split(",", 2) for ln in lines if ln.count(",") >= 2]
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    return []


if __name__ == "__main__":
    out, days = sys.argv[1], float(sys.argv[2])
    max_events = int(sys.argv[3]) if len(sys.argv) > 3 else 2000
    os.makedirs(out, exist_ok=True)
    evs = closed_events(days, max_events)
    mk = flatten(evs)
    mk.to_parquet(f"{out}/events.parquet")
    print(f"events {mk.event.nunique()}, bucket markets {len(mk)}, cities {mk.city.nunique()}", flush=True)
    with ThreadPoolExecutor(12) as ex:
        allt = []
        for i, rows in enumerate(ex.map(trades_for, mk.condition_id)):
            allt += rows
            if i % 500 == 0:
                print(f"  trades {i}/{len(mk)} rows={len(allt)}", flush=True)
    tr = pd.DataFrame(allt)
    keep = [c for c in ["proxy_wallet", "side", "token_id", "condition_id", "size", "price", "timestamp", "outcome"]
            if c in tr.columns]
    tr[keep].to_parquet(f"{out}/trades.parquet")
    print(f"trades {len(tr)}", flush=True)
    start = datetime.now(timezone.utc) - timedelta(days=days + 2)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    rows = []
    stations = sorted(s for s in mk.station.dropna().unique())
    with ThreadPoolExecutor(4) as ex:
        for st, res in zip(stations, ex.map(lambda s: metar(s, start, end), stations)):
            rows += [(st, v, m) for _, v, m in res]
    pd.DataFrame(rows, columns=["station", "valid", "metar"]).to_parquet(f"{out}/metar.parquet")
    print(f"metar rows {len(rows)} for {len(stations)} stations", flush=True)
