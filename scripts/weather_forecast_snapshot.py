"""Forward test for weather markets: snapshot ensemble-forecast bucket probabilities next to market prices.

For every active 'Highest temperature in <city> on <date>' event (next days): ECMWF (51) + GFS (31) ensemble
daily-max members from Open-Meteo at the METAR station -> bucket probabilities (members smeared with a
Gaussian kernel for station/model error) -> saved with the market's best bid/ask. Evaluate later with
scripts/weather_forecast_eval.py once the events resolve.

usage: python scripts/weather_forecast_snapshot.py OUT_DIR [kernel_sd_c=1.0]
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import numpy as np
import requests
from scipy import stats

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.config import GAMMA  # noqa: E402
from pmbot.http import get_json  # noqa: E402
from pmbot.weather import STATION_TZ  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from collect_weather import bucket_bounds  # noqa: E402

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}


def active_weather():
    out, offset = [], 0
    while True:
        d = get_json(f"{GAMMA}/events", {"tag_slug": "highest-temperature", "closed": "false", "limit": 100,
                                         "offset": offset}, timeout=90)
        if not d:
            return out
        out += d
        offset += 100
        if len(d) < 100:
            return out


def station_coords(ids):
    r = requests.get("https://aviationweather.gov/api/data/stationinfo", params={"ids": ",".join(ids), "format": "json"},
                     timeout=30)
    return {s["icaoId"]: (s["lat"], s["lon"]) for s in r.json()}


def ensemble_max(lat, lon, tz, date):
    params = {"latitude": lat, "longitude": lon, "daily": "temperature_2m_max", "models": "ecmwf_ifs025,gfs025",
              "timezone": tz, "start_date": date, "end_date": date}
    for i in range(5):
        try:
            r = requests.get("https://ensemble-api.open-meteo.com/v1/ensemble", params=params, timeout=30)
        except requests.RequestException:
            time.sleep(3 * (i + 1))
            continue
        if r.status_code == 200:
            d = r.json().get("daily") or {}
            return [v[0] for k, v in d.items() if k.startswith("temperature_2m_max") and v and v[0] is not None]
        time.sleep(3 * (i + 1))
    return []


def bucket_probs(members_c, buckets, unit, kernel_sd_c):
    """P(qualifying whole-degree value in [lo, hi]) with members ~ N(member, kernel_sd)."""
    m = np.asarray(members_c, dtype=float)
    out = []
    for lo, hi in buckets:
        # whole-degree value v is reported when the true value is in [v-0.5, v+0.5)
        if unit == "F":
            lo_c = (lo - 0.5 - 32) * 5 / 9 if lo > -900 else -1e9
            hi_c = (hi + 0.5 - 32) * 5 / 9 if hi < 900 else 1e9
        else:
            lo_c = lo - 0.5 if lo > -900 else -1e9
            hi_c = hi + 0.5 if hi < 900 else 1e9
        p = stats.norm.cdf((hi_c - m) / kernel_sd_c) - stats.norm.cdf((lo_c - m) / kernel_sd_c)
        out.append(float(p.mean()))
    s = sum(out)
    return [x / s for x in out] if s > 0 else out


if __name__ == "__main__":
    out = sys.argv[1]
    ksd = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    os.makedirs(out, exist_ok=True)
    evs = active_weather()
    rows = []
    stations = set()
    for e in evs:
        m0 = (e.get("markets") or [{}])[0]
        st = re.search(r"ids=([A-Z0-9]{4})", m0.get("resolutionSource") or "")
        if st and st.group(1) in STATION_TZ:
            stations.add(st.group(1))
    coords = station_coords(sorted(stations))
    now = datetime.now(timezone.utc).isoformat()
    for e in evs:
        m0 = (e.get("markets") or [{}])[0]
        st = re.search(r"ids=([A-Z0-9]{4})", m0.get("resolutionSource") or "")
        dm = re.search(r"on-([a-z]+)-(\d+)-(\d{4})$", e["slug"])
        if not st or not dm or st.group(1) not in coords:
            continue
        st = st.group(1)
        date = f"{dm.group(3)}-{MONTHS[dm.group(1)]:02d}-{int(dm.group(2)):02d}"
        legs = []
        for m in e["markets"]:
            b = bucket_bounds(m.get("groupItemTitle") or "")
            if b is None or m.get("closed"):
                continue
            legs.append({"bucket": m["groupItemTitle"], "lo": b[0], "hi": b[1], "unit": b[2],
                         "bid": m.get("bestBid"), "ask": m.get("bestAsk"), "last": m.get("lastTradePrice"),
                         "condition_id": m["conditionId"]})
        if len(legs) < 3:
            continue
        mem = ensemble_max(*coords[st], STATION_TZ[st], date)
        if len(mem) < 10:
            continue
        probs = bucket_probs(mem, [(lg["lo"], lg["hi"]) for lg in legs], legs[0]["unit"], ksd)
        for lg, p in zip(legs, probs):
            lg["p_model"] = p
        rows.append({"snapshot_utc": now, "event": e["slug"], "station": st, "date": date, "members": len(mem),
                     "ens_mean_c": float(np.mean(mem)), "ens_sd_c": float(np.std(mem)), "legs": legs})
        time.sleep(1.0)
    fn = os.path.join(out, f"snapshot_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M')}.json")
    json.dump(rows, open(fn, "w"))
    print(f"saved {len(rows)} events -> {fn}")
    # quick view: biggest model-vs-market gaps
    gaps = []
    for r in rows:
        for lg in r["legs"]:
            mid = None
            if lg["bid"] is not None and lg["ask"] is not None:
                mid = (float(lg["bid"]) + float(lg["ask"])) / 2
            if mid is not None:
                gaps.append((lg["p_model"] - mid, r["event"], lg["bucket"], round(mid, 3), round(lg["p_model"], 3)))
    gaps.sort(key=lambda x: -abs(x[0]))
    for g in gaps[:15]:
        print(f"  {g[0]:+.3f}  {g[1][:55]:55s} {g[2]:14s} mid={g[3]} model={g[4]}")
