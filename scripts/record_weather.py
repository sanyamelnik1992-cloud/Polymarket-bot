"""Recorder of the temperature markets (reports/78): no orders, data only.

  python scripts/record_weather.py [--out data/weather] [--minutes N] [--once]

Every 10 minutes: the stations' METAR / SPECI lines (aviationweather.gov, the markets' resolution source) and the top of
the book of every bucket whose local date is today or tomorrow (CLOB). Every hour: forecasts of three models for each
station (Open-Meteo: ECMWF IFS, GFS, ICON). Every 30 minutes: the events of yesterday .. +2 days (Gamma), written when
they change. Files: OUT/YYYY-MM-DD/{events,metar,books,forecasts}.jsonl.gz (UTC date of writing).
This is the data for the forecast-versus-market backtest: what was known and what could be bought, at the time.
"""
import argparse
import datetime
import gzip
import hashlib
import json
import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.weather import sources as src  # noqa: E402
from pmbot.weather.rules import local_dt  # noqa: E402

log = logging.getLogger("weather-rec")
STATIONS_FILE = os.path.join(os.path.dirname(__file__), "..", "config", "weather_stations.json")


class WeatherRecorder:
    def __init__(self, out, fetch=src, clock=time.time, metar_s=600, books_s=600, fc_s=3600, events_s=1800, cities_s=6 * 3600):
        self.out, self.fetch, self.clock = out, fetch, clock
        self.every = dict(metar=metar_s, books=books_s, fc=fc_s, events=events_s, cities=cities_s)
        self.last = {k: -1e18 for k in self.every}
        self.stations = json.load(open(STATIONS_FILE)) if os.path.exists(STATIONS_FILE) else {}
        self.cities, self.events = [], {}
        self.seen_lines, self.event_hash = set(), {}
        self.stop = False
        self.n = dict(metar=0, books=0, forecasts=0, events=0)

    def write(self, kind, rows):
        if not rows:
            return
        day = datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc).strftime("%Y-%m-%d")
        d = os.path.join(self.out, day)
        os.makedirs(d, exist_ok=True)
        with gzip.open(os.path.join(d, f"{kind}.jsonl.gz"), "at") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")
        self.n[kind] = self.n.get(kind, 0) + len(rows)

    def due(self, k):
        if self.clock() - self.last[k] >= self.every[k]:
            self.last[k] = self.clock()
            return True
        return False

    def local_dates(self, ev):
        st = self.stations.get(ev["station"] or "")
        return st and st.get("tz")

    def poll(self):
        now = self.clock()
        if self.due("cities") or not self.cities:
            self.cities = self.fetch.discover_cities() or self.cities
        if self.due("events"):
            evs = self.fetch.events_for(self.cities, src.utc_dates_around(now))
            rows = []
            for e in evs:
                self.events[e["slug"]] = e
                h = hashlib.md5(json.dumps(e, sort_keys=True).encode()).hexdigest()
                if self.event_hash.get(e["slug"]) != h:
                    self.event_hash[e["slug"]] = h
                    rows.append(dict(t=round(now), **e))
            self.write("events", rows)
            new = sorted({e["station"] for e in evs if e["station"] and e["station"] not in self.stations})
            if new:
                for s, info in (self.fetch.station_info(new) or {}).items():
                    self.stations[s] = dict(info, tz=None)
        live = [e for e in self.events.values() if not e["closed"] and e["station"]]
        if self.due("metar"):
            got = self.fetch.metar(sorted({e["station"] for e in live}), hours=3) or {}
            rows = []
            for s, lines in got.items():
                for line in lines:
                    if (s, line) not in self.seen_lines:
                        self.seen_lines.add((s, line))
                        rows.append(dict(t=round(now), station=s, line=line))
            if len(self.seen_lines) > 50000:
                self.seen_lines = set(list(self.seen_lines)[-20000:])
            self.write("metar", rows)
        if self.due("books"):
            want = []
            for e in live:
                tz = (self.stations.get(e["station"]) or {}).get("tz")
                if tz:
                    today = local_dt(now, tz).date()
                    if e["date"] not in (str(today), str(today + datetime.timedelta(days=1))):
                        continue
                want += [(e["slug"], b["label"], b["yes"]) for b in e["markets"] if b["yes"] and not b["closed"]]
            got = self.fetch.books([w[2] for w in want]) or {}
            self.write("books", [dict(t=round(now), slug=s, label=lab, **got[tok]) for s, lab, tok in want if tok in got])
        if self.due("fc"):
            rows = []
            for s in sorted({e["station"] for e in live}):
                st = self.stations.get(s)
                if not st:
                    continue
                j = self.fetch.forecast(st["lat"], st["lon"])
                if not j or "hourly" not in j:
                    continue
                if j.get("timezone") and not st.get("tz"):
                    st["tz"] = j["timezone"]
                h = j["hourly"]
                rows.append(dict(t=round(now), station=s, tz=j.get("timezone"), time=h.get("time"),
                                 temps={k.replace("temperature_2m_", ""): v for k, v in h.items() if k.startswith("temperature_2m")}))
            self.write("forecasts", rows)

    def run(self, minutes=None, once=False):
        t0 = time.time()
        while not self.stop:
            try:
                self.poll()
            except Exception as e:  # noqa: BLE001  (a source can fail; the next pass tries again)
                log.warning("poll failed: %s", e)
            if once or (minutes and time.time() - t0 > minutes * 60):
                break
            for _ in range(60):
                if self.stop:
                    break
                time.sleep(1)
        log.info("written: %s", self.n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/weather")
    ap.add_argument("--minutes", type=float, default=None)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    rec = WeatherRecorder(a.out)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(rec, "stop", True))
    rec.run(a.minutes, a.once)


if __name__ == "__main__":
    main()
