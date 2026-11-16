"""Data sources of the temperature markets (all public, no key):
Gamma (events and buckets), CLOB (books), aviationweather.gov (live METAR, station info), Open-Meteo (forecasts)."""
import datetime
import json
import math
import re
import time

import requests

from ..config import CLOB, GAMMA
from .rules import date_slug, event_date, parse_bucket

S = requests.Session()
S.headers["User-Agent"] = "pmbot-weather/0.1"
AWC = "https://aviationweather.gov/api/data"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
MODELS = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless")


def get(url, params=None, timeout=60, retries=4):
    for k in range(retries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 + 3 * k)
                continue
            return r
        except requests.RequestException:
            time.sleep(2 + 3 * k)
    return None


def discover_cities(pages=30):
    """City slugs of the open "highest temperature in CITY on DATE" events (public search)."""
    cities = set()
    for page in range(1, pages + 1):
        r = get(f"{GAMMA}/public-search", {"q": "highest temperature", "limit_per_type": 50, "events_status": "active",
                                           "page": page})
        evs = (r.json().get("events") or []) if r is not None and r.status_code == 200 else []
        new = 0
        for e in evs:
            m = re.match(r"^highest-temperature-in-(.+)-on-[a-z]+-\d{1,2}-\d{4}$", e.get("slug", ""))
            if m and m.group(1) not in cities:
                cities.add(m.group(1))
                new += 1
        if not evs or not new:
            break
    return sorted(cities)


def parse_event(e):
    """One Gamma event -> dict(slug, city, date, station, unit, end, rules_ok, markets=[...]) or None."""
    slug = e.get("slug") or ""
    m = re.match(r"^highest-temperature-in-(.+)-on-([a-z]+-\d{1,2}-\d{4})$", slug)
    if not m or not e.get("markets"):
        return None
    desc = e.get("description") or ""
    src = e["markets"][0].get("resolutionSource") or ""
    st = re.search(r"ids=([A-Z0-9]{4})", src)
    markets = []
    for x in e["markets"]:
        label = x.get("groupItemTitle") or ""
        try:
            lo, hi = parse_bucket(label)
        except (ValueError, IndexError):
            continue
        toks = json.loads(x.get("clobTokenIds") or "[]")
        op = json.loads(x.get("outcomePrices") or "[]")
        markets.append(dict(label=label, lo=None if lo == -math.inf else lo, hi=None if hi == math.inf else hi,
                            cid=x.get("conditionId"), yes=toks[0] if toks else None, no=toks[1] if len(toks) > 1 else None,
                            closed=bool(x.get("closed")), won=(op[0] in ("1", 1)) if op and x.get("closed") else None))
    markets.sort(key=lambda b: -math.inf if b["lo"] is None else b["lo"])
    return dict(slug=slug, city=m.group(1), date=str(event_date(slug)), station=st.group(1) if st else None,
                unit="F" if any("°F" in b["label"] for b in markets) else "C", end=e.get("endDate"),
                closed=bool(e.get("closed")), rules_ok="routine (METAR) observation" in desc and "nearest whole degree" in desc,
                markets=markets)


def events_for(cities, dates, batch=20):
    """Events of these cities on these dates (Gamma, by slug, 20 a request)."""
    slugs = [f"highest-temperature-in-{c}-on-{date_slug(d)}" for c in cities for d in dates]
    out = []
    for i in range(0, len(slugs), batch):
        params = [("slug", s) for s in slugs[i:i + batch]] + [("limit", 50)]
        r = get(f"{GAMMA}/events", params)
        if r is None or r.status_code != 200:
            continue
        for e in r.json():
            ev = parse_event(e)
            if ev:
                out.append(ev)
    return out


def metar(stations, hours=3):
    """Raw METAR / SPECI lines of the last hours, by station (aviationweather.gov, the markets' resolution source)."""
    out = {s: [] for s in stations}
    for i in range(0, len(stations), 100):
        r = get(f"{AWC}/metar", {"ids": ",".join(stations[i:i + 100]), "format": "raw", "hours": hours})
        if r is None or r.status_code != 200:
            continue
        for line in r.text.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            st = parts[1] if parts[0] in ("METAR", "SPECI") else parts[0]
            if st in out:
                out[st].append(line.strip())
    return out


def station_info(stations):
    r = get(f"{AWC}/stationinfo", {"ids": ",".join(stations), "format": "json"})
    if r is None or r.status_code != 200:
        return {}
    return {x["icaoId"]: dict(lat=x["lat"], lon=x["lon"], elev=x.get("elev"), site=x.get("site"), country=x.get("country"))
            for x in r.json()}


def forecast(lat, lon, models=MODELS, days=3):
    """Hourly 2 m temperature of several models (Open-Meteo), local time zone of the point."""
    r = get(OPEN_METEO, {"latitude": lat, "longitude": lon, "hourly": "temperature_2m", "models": ",".join(models),
                         "timezone": "auto", "forecast_days": days, "past_days": 1})
    if r is None or r.status_code != 200:
        return None
    return r.json()


def books(token_ids, batch=200, depth_c=5.0):
    """Top of the book per token: best bid / ask, their sizes, and the size within depth_c cents of each."""
    out = {}
    for i in range(0, len(token_ids), batch):
        try:
            r = S.post(f"{CLOB}/books", json=[{"token_id": t} for t in token_ids[i:i + batch]], timeout=60)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        for b in r.json():
            bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids") or []), reverse=True)
            asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks") or [])
            bb, ba = (bids[0] if bids else (None, 0.0)), (asks[0] if asks else (None, 0.0))
            out[b["asset_id"]] = dict(bid=bb[0], bid_sz=bb[1], ask=ba[0], ask_sz=ba[1],
                                      bid_depth=round(sum(s for p, s in bids if bb[0] is not None and p >= bb[0] - depth_c / 100), 2),
                                      ask_depth=round(sum(s for p, s in asks if ba[0] is not None and p <= ba[0] + depth_c / 100), 2))
    return out


def utc_dates_around(now=None, back=1, ahead=2):
    d = datetime.datetime.fromtimestamp(now or time.time(), datetime.timezone.utc).date()
    return [d + datetime.timedelta(days=k) for k in range(-back, ahead + 1)]
